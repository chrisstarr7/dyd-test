"""把测试范围绑定到需求原文，并在浏览器采样前冻结验收预期。"""
import hashlib
import json
import re

from qa_browser import LAYERS, browser_page, browser_probe, redact_data, redacted_measurement
from qa_contract import check_plan, digest, evidence_file, index_objects, text
from qa_oracle import probes, valid_check
from qa_risk import review_risks


RISKS = {"high", "medium", "low"}
SCENARIOS = {"normal", "boundary", "negative", "role", "regression"}


def fragments(line):
    """按句界建立最小追踪单位，中文枚举和跨行规则由独立评审继续细拆。"""
    return [part.strip() for part in re.split(r"[；;。]", line) if part.strip()] or [line.strip()]


def review_obligations(directory, scope, criteria):
    """按独立复核清单比对可单独失败的业务义务，保留未覆盖项。"""
    source = evidence_file(directory, scope["document"])
    path = directory / "prd-review.json"
    if not path.exists():
        return None, ["缺少独立 PRD 拆分复核清单"]
    review = json.loads(evidence_file(directory, path.name).read_text(encoding="utf-8"))
    if (not isinstance(review, dict) or review.get("revision") != scope["revision"]
            or review.get("source_sha256") != hashlib.sha256(source.read_bytes()).hexdigest()
            or not text(review.get("reviewer")) or not text(review.get("reference"))):
        raise ValueError("PRD 复核清单与本轮原文、版本或复核来源不匹配")
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    clauses = {(line, part): fragment for entry in scope["sections"] if entry["status"] == "in_scope"
               for line in range(entry["from"], entry["to"] + 1) if lines[line - 1].strip()
               for part, fragment in enumerate(fragments(lines[line - 1]), 1)}
    errors = []
    obligations = index_objects(review.get("obligations"), "独立复核义务", errors)
    excerpts = {}
    linked = set()
    gaps = []
    for key, item in obligations.items():
        line, part, excerpt = (item.get(field) for field in ("source_line", "source_part", "excerpt"))
        refs = item.get("criteria")
        position = (line, part)
        if (type(line) is not int or type(part) is not int or position not in clauses or not text(excerpt)
                or excerpt not in clauses[position] or not text(item.get("rule"))
                or not isinstance(refs, list) or not all(text(ref) and ref in criteria for ref in refs)):
            errors.append(f"{key} 的原文位置、行为或验收点引用无效")
            continue
        seen = excerpts.setdefault(position, set())
        if excerpt in seen or set(refs) & linked or len(set(refs)) != len(refs):
            errors.append(f"{key} 重复复核同一摘录或复用其他义务的验收点")
        seen.add(excerpt)
        for ref in refs:
            criterion = criteria[ref][1]
            if (criterion.get("source_line"), criterion.get("source_part")) != position:
                errors.append(f"{key} 的验收点 {ref} 指向其他原文片段")
        linked.update(refs)
        if not refs:
            gaps.append(f"PRD 复核义务 {key} 缺少验收点：{item['rule']}")
    for position, clause in clauses.items():
        parts = re.split(r"并(?:且)?(?=撤销|回收|释放|清除|注销|发送|记录|更新|生成|禁止|拒绝|关闭|保留|返回)"
                         r"|\band\s+(?=revoke\b|delete\b|invalidate\b|restore\b|record\b|reject\b|notify\b)",
                         clause, flags=re.I)
        for segment in parts:
            if not any(excerpt in segment for excerpt in excerpts.get(position, ())):
                gaps.append(f"PRD 第 {position[0]} 行缺少独立义务复核：{segment.strip()}")
    if errors:
        raise ValueError("；".join(errors))
    return review, gaps


def coverage_gaps(scope, risk, cases):
    """独立场景可以先测，范围缺口和未评审风险仍阻止完整 PRD 通过。"""
    gaps = []
    if scope.get("source_complete") is not True:
        gaps.append("完整 PRD 或引用附件未确认")
    for entry in scope["sections"]:
        if entry["status"] in ("excluded", "pending"):
            gaps.append(f"PRD 第 {entry['from']}–{entry['to']} 行未完整验收：{entry['reason']}")
    if risk["status"] == "not_provided":
        return gaps + ["缺少完整测试风险评审"]
    required_dimensions = {"functionality"}
    if any(case.get("operation") in ("create", "update", "delete") for case in cases.values()):
        required_dimensions.add("data")
    if any(case.get("scenario") == "role" or case.get("actor") == "fixture" for case in cases.values()):
        required_dimensions.add("permission")
    for name, item in risk["dimensions"].items():
        if item.get("coverage") != "verified" and (name in required_dimensions or item["status"] != "not_applicable"):
            gaps.append(f"风险维度 {name} 未完整验证")
    return gaps


def check_dependencies(cases):
    """检查用例依赖成环及其业务对象绑定，返回前置优先的顺序。"""
    pending = {}
    for key, case in cases.items():
        deps = case.get("depends_on", [])
        if (not isinstance(deps, list) or not all(text(dep) and dep in cases for dep in deps)
                or len(set(deps)) != len(deps)):
            raise ValueError(f"用例 {key} 的前置用例未登记或重复")
        fixture = case.get("fixture")
        if case.get("actor") not in (None, "fixture"):
            raise ValueError(f"用例 {key} 的主体验证方式无效")
        if case.get("actor") == "fixture" and (not text(fixture) or not deps or case.get("operation") != "read"):
            raise ValueError(f"用例 {key} 的夹具身份验证须关联同一对象的前置用例")
        pending[key] = set(deps)
    ordered = []
    done = set()
    ancestors = {}
    while pending:
        ready = [key for key, deps in pending.items() if deps <= done]
        if not ready:
            raise ValueError("业务链路的前置用例存在循环依赖")
        for key in ready:
            deps = pending[key]
            ancestors[key] = deps.union(*(ancestors[dep] for dep in deps))
            if cases[key].get("actor") == "fixture" and not any(
                cases[dep].get("operation") in {"create", "update"}
                and cases[dep].get("fixture") == cases[key]["fixture"] for dep in ancestors[key]
            ):
                raise ValueError(f"用例 {key} 缺少同一夹具已创建或更新的前置用例")
            ordered.append(key)
            done.add(key)
            del pending[key]
    return ordered


def check_flows(scope, cases):
    """完整模式的每个用例均属于一个已冻结的业务路径或独立规则。"""
    errors = []
    flows = index_objects(scope.get("flows"), "业务流程", errors)
    covered = set()
    for key, flow in flows.items():
        steps = flow.get("cases")
        if (not text(flow.get("source")) or not isinstance(steps, list) or not steps
                or not all(text(step) and step in cases for step in steps) or len(steps) != len(set(steps))):
            errors.append(f"流程 {key} 缺少 PRD 出处或有效的用例顺序")
            continue
        for first, second in zip(steps, steps[1:]):
            if first not in cases[second].get("depends_on", []):
                errors.append(f"流程 {key} 的 {second} 缺少前置 {first}")
        covered.update(steps)
    if covered != set(cases):
        errors.append("完整模式存在未登记业务路径的用例")
    if errors:
        raise ValueError("；".join(errors))


def plan_fingerprint(directory, run, requirements, cases, pages):
    """索引必须处置每个有效原文行，并为所有必测场景安排用例。"""
    planned, criteria, errors = check_plan(requirements, cases)
    if errors:
        raise ValueError("；".join(errors))
    scope = json.loads((directory / "prd-scope.json").read_text(encoding="utf-8"))
    if (not isinstance(scope, dict) or not text(scope.get("revision"))
            or not isinstance(scope.get("sections"), list)):
        raise ValueError("缺少原 PRD 的版本和条款索引")
    source = evidence_file(directory, scope.get("document"))
    assured = run.get("assurance_contract") == 1
    full = assured and run.get("mode") == "full"
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    substantive = {i for i, line in enumerate(lines, 1) if line.strip()}
    seen, mapped = set(), {}
    for entry in scope["sections"]:
        if not isinstance(entry, dict):
            raise ValueError("PRD 条款索引无效")
        first, last = entry.get("from"), entry.get("to")
        state, refs = entry.get("status"), entry.get("requirements", [])
        if (type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(lines)
                or state not in ("in_scope", "excluded", "context", "pending")
                or not isinstance(refs, list) or not all(map(text, refs))):
            raise ValueError("PRD 条款范围、分类或需求引用无效")
        span = set(range(first, last + 1))
        if seen & span:
            raise ValueError("PRD 条款索引重叠")
        seen.update(span)
        if state == "pending" and not assured:
            raise ValueError(f"PRD 第 {first}–{last} 行待澄清")
        if state == "in_scope":
            if not refs:
                raise ValueError(f"PRD 第 {first}–{last} 行缺少需求对应")
            for req in refs:
                if req in mapped:
                    mapped[req].append((first, last))
                else:
                    mapped[req] = [(first, last)]
        elif refs or not text(entry.get("reason")) or (state == "excluded" and not text(entry.get("decision"))):
            raise ValueError("排除须记录范围决定，背景需说明理由")
    if substantive - seen or set(mapped) != {req["id"] for req in requirements}:
        raise ValueError("PRD 有未处置的原文行或未对齐的需求")
    if full:
        required_fragments = {(line, part): fragment for spans in mapped.values() for first, last in spans
                              for line in range(first, last + 1) if lines[line - 1].strip()
                              for part, fragment in enumerate(fragments(lines[line - 1]), 1)}
        traced = set()
        for key, (req_id, criterion) in criteria.items():
            line, part, fragment = (criterion.get(field) for field in ("source_line", "source_part", "source_quote"))
            if (type(line) is not int or type(part) is not int or not text(fragment)
                    or required_fragments.get((line, part)) != fragment
                    or not any(first <= line <= last for first, last in mapped[req_id])):
                raise ValueError(f"{key} 必须引用对应 PRD 行的完整条款片段")
            if not criterion.get("required", True):
                raise ValueError(f"{key} 属于完整测试范围，不能标为非必测")
            traced.add((line, part))
        if required_fragments.keys() - traced:
            missing = "、".join(f"第 {line} 行第 {part} 段「{required_fragments[line, part]}」"
                               for line, part in sorted(required_fragments.keys() - traced))
            raise ValueError(f"PRD 条款缺少验收点：{missing}")
    if assured:
        check_dependencies(planned)
    if full:
        check_flows(scope, planned)

    coverage = {key: set() for key in criteria}
    for page in pages.values():
        browser_page(page)
    for case in planned.values():
        rules = case.get("checks")
        scenario = case.get("scenario")
        layer = case.get("layer", "online_ui")
        if (case["page"] not in pages or scenario not in SCENARIOS or layer not in LAYERS
                or ("result_ready" in case and not text(case["result_ready"]))
                or ("verify_reload" in case and type(case["verify_reload"]) is not bool)
                or ("expected_after" in case and case["expected_after"] not in ("present", "absent"))
                or ("expected_before" in case and case["expected_before"] not in ("present", "absent"))
                or not isinstance(rules, dict) or rules.keys() != case["expected"].keys()
                or not all(valid_check(value, browser_probe, baseline=case.get("operation") in ("create", "update", "delete"))
                           for value in rules.values())):
            raise ValueError(f"用例 {case['id']} 缺少页面、场景、执行层或逐项浏览器采集规则")
        for key in rules:
            if layer != criteria[key][1].get("layer", "online_ui"):
                raise ValueError(f"用例 {case['id']} 和 {key} 的执行层不一致")
            coverage[key].add(scenario)
        if assured and (case.get("operation") in ("create", "update", "delete")
                        or case.get("actor") == "fixture" or scenario == "role"):
            if not pages[case["page"]]["identity"].get("principal"):
                raise ValueError(f"用例 {case['id']} 缺少独立的当前主体标识")
        if assured and case.get("operation") in ("create", "update", "delete"):
            if not text(case.get("fixture")) or case.get("verify_reload") is not True:
                raise ValueError(f"写入用例 {case['id']} 须关联夹具并重新载入持久化回查")
            if case["operation"] == "update" and not any(
                rule.get("scope") == "fixture" for rule in probes(rules, "current").values()
            ):
                raise ValueError(f"修改用例 {case['id']} 至少一个断言须读取当前夹具字段")
            if run.get("evidence_contract") == 1:
                action = case["operation"]
                before = case.get("expected_before", "absent") if action == "create" and scenario == "negative" else (
                    "absent" if action == "create" else "present"
                )
                after = case.get("expected_after", "absent" if action == "delete" else "present")
                effect = case.get("effect", {})
                valid = isinstance(effect, dict) and (
                    (before == after and effect.get("mode") == "unchanged"
                     and isinstance(effect.get("receipt"), dict) and text(effect["receipt"].get("selector"))
                     and text(effect["receipt"].get("text")))
                    or (action == "update" and before == after == "present" and effect.get("mode") == "change"
                        and text(effect.get("field")) and type(effect.get("to")) in (str, int, bool)
                        and redact_data(effect["to"]) == effect["to"] and not redacted_measurement(effect["to"]))
                    or (action != "update" and before != after and not effect)
                )
                if not valid:
                    raise ValueError(f"写入用例 {case['id']} 须冻结字段变化或无变化操作的响应证据")
        elif assured and any(rule.get("scope") == "fixture" for rule in probes(rules, "current").values()):
            raise ValueError(f"只读用例 {case['id']} 须从已定位的页面读取业务字段")
        elif run.get("evidence_contract") == 1 and "effect" in case:
            raise ValueError(f"只读用例 {case['id']} 不能登记写入效果")

    for key, (_, criterion) in criteria.items():
        scenarios = criterion.get("scenarios")
        if not criterion.get("required", True):
            if not text(criterion.get("scope_decision")):
                raise ValueError(f"{key} 非必测须有范围决定出处")
            continue
        if (criterion.get("risk") not in RISKS or not isinstance(scenarios, list) or not scenarios
                or not all(isinstance(item, str) and item in SCENARIOS for item in scenarios)):
            raise ValueError(f"{key} 缺少业务风险或必测场景")
        if set(scenarios) - coverage[key]:
            raise ValueError(f"{key} 缺少必测场景：{', '.join(sorted(set(scenarios) - coverage[key]))}")
        if full and criterion["risk"] == "high" and not set(scenarios).intersection({"boundary", "negative", "role"}):
            raise ValueError(f"{key} 高风险缺少边界、异常或角色场景")
        if criterion["risk"] == "high" and len(set(scenarios)) < 2 and (full or not text(criterion.get("risk_note"))):
            raise ValueError(f"{key} 高风险仅覆盖单一场景，须说明依据")
    frozen = [run.get("run_id"), run.get("mode"), run.get("version"), run.get("url"),
              scope, source.read_text(encoding="utf-8-sig"), requirements, cases, pages]
    issues, risk = review_risks(directory, run, planned, None)
    if issues:
        raise ValueError("；".join(issues))
    if risk["plan_sha256"]:
        frozen.append(risk["plan_sha256"])
    if assured:
        frozen.append(run["assurance_contract"])
    if run.get("evidence_contract") == 1:
        frozen.append(run["evidence_contract"])
        if full:
            review, _ = review_obligations(directory, scope, criteria)
            frozen.append(review)
    return digest(frozen)


def verify_frozen(directory, run, requirements, cases, pages):
    frozen = json.loads((directory / "frozen-plan.json").read_text(encoding="utf-8"))
    actual = plan_fingerprint(directory, run, requirements, cases, pages)
    if not isinstance(frozen, dict) or frozen.get("sha256") != actual:
        raise ValueError("PRD、范围、页面或用例已变更，需重新建立运行")


def freeze(directory, run, requirements, cases, pages):
    fingerprint = plan_fingerprint(directory, run, requirements, cases, pages)
    try:
        with (directory / "frozen-plan.json").open("x", encoding="utf-8") as stream:
            json.dump({"sha256": fingerprint}, stream)
    except FileExistsError:
        verify_frozen(directory, run, requirements, cases, pages)
