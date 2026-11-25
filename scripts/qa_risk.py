"""核对可选的本轮风险计划、必测场景与已核验的最新结果。"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from qa_browser import LAYERS
from qa_contract import digest, evidence_file, index_objects, text


RISK_REVIEW_FILE = "risk-review.json"
DIMENSIONS = ("functionality", "data", "permission", "integration", "regression",
              "reliability", "performance", "accessibility")
# 沿用当前用例场景词汇，保持本模块可被冻结计划直接调用。
SCENARIOS = ("normal", "boundary", "negative", "role", "regression")
PLACEHOLDERS = {"unknown", "tbd", "n/a", "-", "未确认", "待确认", "未知", "不适用"}


def _confirmed(value):
    return text(value) and value.strip().lower() not in PLACEHOLDERS


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("JSON 含重名字段")
    return value


def _read_json(content):
    value = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("JSON 顶层必须是对象")
    return value


def _same_run(record, run):
    return isinstance(run, dict) and all(
        _confirmed(run.get(key)) and record.get(key) == run[key] for key in ("run_id", "version")
    )


def _decision(read, run, name, subject, status):
    # shortcut: 本地审批凭据只核验结构、范围与本轮关联，需要核验签名时接入可信审批系统。
    try:
        record = _read_json(read(name))
        if (record.get("type") == "risk_decision" and record.get("decision") == status
                and record.get("subject") == subject and _same_run(record, run)
                and all(_confirmed(record.get(key)) for key in ("by", "reason", "reference"))):
            return {"decision": status, "evidence": name,
                    **{key: record[key] for key in ("by", "reason", "reference")}}
    except (OSError, ValueError):
        pass
    raise ValueError(f"{subject} 缺少有效的本轮 {status} 决策凭据")


def _review_dimensions(raw, read, run):
    if not isinstance(raw, dict) or raw.keys() - set(DIMENSIONS):
        raise ValueError("dimensions 必须是由已知风险维度组成的对象")
    dimensions = dict.fromkeys(DIMENSIONS)
    for name in dimensions:
        if name not in raw:
            dimensions[name] = {"status": "unreviewed"}
            continue
        scope = raw[name]
        status = scope.get("status") if isinstance(scope, dict) else None
        if status == "in_scope" and "decision" not in scope:
            dimensions[name] = {"status": status}
        elif status in ("out_of_scope", "not_applicable"):
            dimensions[name] = {"status": status, "decision": _decision(
                read, run, scope.get("decision"), f"dimension:{name}", status,
            )}
        else:
            raise ValueError(f"维度 {name} 须声明 in_scope、out_of_scope 或 not_applicable")
    return dimensions


def _review_layers(raw, read):
    if not isinstance(raw, dict):
        raise ValueError("blocked_layers 必须是对象")
    layers = {name: {"status": "supported"} for name in sorted(LAYERS)}
    for name, entry in raw.items():
        if (name in layers or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
                or not isinstance(entry, dict) or not _confirmed(entry.get("reason"))):
            raise ValueError(f"执行层 {name} 须使用独立标识并说明不可验证的原因")
        layer = {"status": "unsupported", "reason": entry["reason"]}
        if "evidence" in entry:
            read(entry["evidence"])
            layer["evidence"] = entry["evidence"]
        layers[name] = layer
    return layers


def _cases_by_scenario(cases):
    if isinstance(cases, list):
        errors = []
        cases = index_objects(cases, "风险计划关联用例", errors)
        if errors:
            raise ValueError("；".join(errors))
    if not isinstance(cases, dict):
        raise ValueError("风险计划关联用例须为列表或用例编号索引")
    index = defaultdict(list)
    for case_id, case in cases.items():
        if not text(case_id) or not isinstance(case, dict):
            raise ValueError("风险计划关联用例格式无效")
        scenario, layer = case.get("scenario"), case.get("layer", "online_ui")
        expected = case.get("expected", {})
        if ((scenario is not None and not text(scenario)) or not text(layer)
                or not isinstance(expected, dict) or not all(map(text, expected))):
            raise ValueError("风险计划关联用例的场景、执行层或验收点格式无效")
        if scenario in SCENARIOS:
            for criterion in expected:
                index[scenario, layer, criterion].append(case_id)
    return index


def _scenario_status(case_ids, results):
    if not case_ids:
        return "missing"
    if results is None:
        return "planned"
    states = set()
    for case_id in case_ids:
        record = results.get(case_id, {"state": "not_run"})
        state = record.get("state") if isinstance(record, dict) else None
        if state not in ("failed", "unknown", "passed", "blocked", "skipped", "planned", "not_run"):
            raise ValueError("风险计划关联用例的最新结果状态无效")
        states.add("not_run" if state == "planned" else state)
    # 同一场景的全部用例均通过后才算核验完成。
    for state in ("failed", "unknown", "blocked", "skipped", "not_run"):
        if state in states:
            return state
    return "verified"


def _coverage(statuses):
    if not statuses:
        return "unreviewed"
    for status in ("open", "unreviewed", "planned", "accepted"):
        if status in statuses:
            return status
    return "verified"


def _review_risk(risk, dimensions, layers, source, index, results, read, run, reasons):
    risk_id, dimension, severity = risk["id"], risk.get("dimension"), risk.get("severity")
    if not text(dimension) or dimensions.get(dimension, {}).get("status") != "in_scope":
        raise ValueError(f"风险 {risk_id} 须归入已声明的 in_scope 维度")
    if (severity not in ("high", "medium", "low")
            or not all(_confirmed(risk.get(key)) for key in ("invariant", "impact", "source"))
            or risk["source"] not in source):
        raise ValueError(f"风险 {risk_id} 须登记严重度、业务不变量、影响及可在来源文件中定位的原文")
    errors = []
    scenarios = index_objects(risk.get("scenarios", []), f"{risk_id} 必测场景", errors)
    if errors:
        raise ValueError("；".join(errors))
    if severity == "high" and not scenarios:
        reasons.append(f"高风险 {risk_id} 缺少必测场景")
    if (severity == "high" and run.get("assurance_contract") == 1 and run.get("mode") == "full"
            and (len(scenarios) < 2 or not any(item.get("scenario") in ("boundary", "negative", "role")
                                               for item in scenarios.values()))):
        reasons.append(f"高风险 {risk_id} 须安排至少两个场景并包含边界、异常或角色验证")

    reviewed, statuses, seen = {}, [], set()
    for scenario_id, entry in scenarios.items():
        scenario, layer, criterion = (entry.get(key) for key in ("scenario", "layer", "criterion"))
        label = f"{risk_id}/{scenario_id}"
        if (scenario not in SCENARIOS or not text(layer) or layer not in layers or not _confirmed(criterion)):
            raise ValueError(f"必测场景 {label} 须填写有效的 scenario、layer 和 criterion")
        key = (scenario, layer, criterion)
        if key in seen:
            raise ValueError(f"风险 {risk_id} 重复登记场景、执行层和验收点组合：{scenario_id}")
        seen.add(key)

        decision = None
        case_ids = index.get(key, [])
        if layers[layer]["status"] == "unsupported":
            status = "unsupported"
            if "acceptance" in entry:
                decision = _decision(read, run, entry["acceptance"], f"scenario:{label}", "accepted")
        else:
            if "acceptance" in entry:
                raise ValueError(f"必测场景 {label} 所需执行层已支持，须用实际用例核验")
            status = _scenario_status(case_ids, results)
        effective = "accepted" if decision else status
        if effective not in ("verified", "planned", "accepted"):
            if severity == "high" and (status != "unsupported" or results is not None):
                reasons.append(f"高风险 {label} 必测场景未核验（{scenario}/{layer}/{criterion}：{status}）")
            effective = "open"
        statuses.append(effective)
        reviewed[scenario_id] = {"scenario": scenario, "layer": layer, "criterion": criterion,
                                 "status": status, "case_ids": case_ids}
        if decision:
            reviewed[scenario_id]["decision"] = decision

    return {"dimension": dimension, "severity": severity, "invariant": risk["invariant"],
            "impact": risk["impact"], "source": risk["source"], "scenarios": reviewed,
            "status": _coverage(statuses) if scenarios or severity != "high" else "open"}


def review_risks(directory, run, cases, results):
    """返回阻塞原因和可 JSON 序列化的风险摘要，无计划时沿用现有门禁。

    risk-review.json 使用 schema_version=1、run_id、version、source（本轮 UTF-8
    来源文件的相对路径）、dimensions（维度名到对象）和 risks（列表）。维度条目的
    status 为 in_scope、out_of_scope 或 not_applicable，后两者须加 decision 凭据路径。
    可选 blocked_layers 按层名登记 reason 和可选 evidence 路径。
    风险条目包含 id、dimension、severity（high/medium/low）、invariant、impact、
    source（来源原文摘录）和 scenarios（列表，高风险必填）。场景条目包含 id、
    scenario（见 SCENARIOS）、layer、criterion（验收点编号）；不支持的层可加
    acceptance 凭据路径。凭据 JSON 包含 type=risk_decision、run_id、version、
    subject（dimension:维度 或 scenario:风险ID/场景ID）、decision（同维度状态或
    accepted）、by、reason、reference（审批出处）。所有证据路径均相对于任务目录。

    cases 接受计划列表或用例编号索引。results 接受评估器已核验的最新结果索引，
    冻结时传 None，已登记但无法执行的层保留开放风险，支持的层仍须覆盖。plan_sha256
    覆盖计划及其引用文件，与执行无关。
    """
    directory = Path(directory)
    path = directory / RISK_REVIEW_FILE
    summary = {"status": "not_provided", "plan_sha256": None, "source": None,
               "dimensions": {name: {"status": "unreviewed"} for name in DIMENSIONS},
               "layers": {name: {"status": "supported"} for name in sorted(LAYERS)}, "risks": {}}
    try:
        path.lstat()
    except FileNotFoundError:
        return [], summary
    except OSError:
        summary["status"] = "invalid"
        return [f"无法读取 {RISK_REVIEW_FILE}"], summary

    inputs = {}

    def read(name):
        if not text(name) or Path(name).anchor:
            raise ValueError("风险证据须引用本轮任务目录内的相对路径")
        if name not in inputs:
            file = evidence_file(directory, name)
            if file.samefile(path):
                raise ValueError("风险计划不能用作自己的凭据")
            inputs[name] = file.read_bytes()
        return inputs[name]

    try:
        content = evidence_file(directory, RISK_REVIEW_FILE).read_bytes()
        plan = _read_json(content)
        schema = plan.get("schema_version")
        if type(schema) is not int or schema != 1 or not _same_run(plan, run):
            raise ValueError("schema_version 或本轮 run_id、version 不匹配")
        source = read(plan.get("source")).decode("utf-8-sig")
        if not source.strip():
            raise ValueError("source 须引用独立的非空来源文本")
        dimensions = _review_dimensions(plan.get("dimensions"), read, run)
        layers = _review_layers(plan.get("blocked_layers", {}), read)
        reasons = []
        risks = index_objects(plan.get("risks"), "风险", reasons)
        if reasons:
            raise ValueError("；".join(reasons))
        if results is not None and not isinstance(results, dict):
            raise ValueError("风险计划关联结果须为用例编号索引")
        index = _cases_by_scenario(cases)
        reviewed = {}
        by_dimension = defaultdict(list)
        for risk_id, risk in risks.items():
            result = _review_risk(risk, dimensions, layers, source, index, results, read, run, reasons)
            reviewed[risk_id] = result
            by_dimension[result["dimension"]].append(result["status"])

        for name, dimension in dimensions.items():
            if dimension["status"] == "in_scope":
                statuses = by_dimension[name]
                dimension["coverage"] = _coverage(statuses)
                if not statuses:
                    reasons.append(f"范围内维度 {name} 缺少风险与业务不变量")
    except (OSError, ValueError, RecursionError) as error:
        summary["status"] = "invalid"
        return [f"{RISK_REVIEW_FILE} 无法核验：{error}"], summary

    inputs[RISK_REVIEW_FILE] = content
    summary.update(status="reviewed", source=plan["source"], dimensions=dimensions, layers=layers,
                   risks=reviewed, plan_sha256=digest({name: hashlib.sha256(data).hexdigest()
                                                       for name, data in inputs.items()}))
    return reasons, summary
