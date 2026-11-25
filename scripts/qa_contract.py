"""校验 PRD 验收点、页面定位和浏览器执行证据之间的关联。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def text(value):
    return isinstance(value, str) and bool(value.strip())


def objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def url_key(value):
    """保留实际路由，包括 SPA 使用的哈希路由。"""
    if not text(value) or re.search(r"[\s\\]", value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in ("http", "https") and url.hostname and url.username is None:
            return (url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80),
                    url.path or "/", url.query, url.fragment)
    except ValueError:
        pass
    return None


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def evidence_file(directory, name):
    """证据必须是任务目录内的非空文件，解析符号链接后仍不得越界。"""
    if text(name):
        try:
            root = directory.resolve()
            path = (root / name).resolve()
            path.relative_to(root)
            if path.is_file() and path.stat().st_size:
                return path
        except (OSError, ValueError, RuntimeError):
            pass
    raise ValueError("证据必须是本次任务目录内的非空文件")


def index_objects(values, label, problems):
    indexed = {}
    if not isinstance(values, list):
        problems.append(f"{label}必须是列表")
        return indexed
    for value in values:
        key = value.get("id") if isinstance(value, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9-]*-\d+", key):
            problems.append(f"{label}编号无效")
        elif key in indexed:
            problems.append(f"{label}编号重复：{key}")
        else:
            indexed[key] = value
    return indexed


def check_plan(requirements, cases):
    """校验到验收点的覆盖，防止用一个关联需求编号代替多条断言。"""
    problems = []
    reqs = index_objects(requirements, "需求", problems)
    planned = index_objects(cases, "用例", problems)
    criteria = {}
    covered = set()
    if not reqs:
        problems.append("未登记 PRD 需求")
    if not planned:
        problems.append("无用例计划")
    for req_id, requirement in reqs.items():
        entries = index_objects(requirement.get("criteria"), f"{req_id} 验收点", problems)
        if not text(requirement.get("source")) or not entries:
            problems.append(f"{req_id} 缺少 PRD 出处或独立验收点")
        for key, criterion in entries.items():
            if key in criteria:
                problems.append(f"验收点编号重复：{key}")
            elif (not text(criterion.get("rule")) or not text(criterion.get("page"))
                  or type(criterion.get("required", True)) is not bool
                  or criterion.get("status", "ready") not in ("ready", "pending")):
                problems.append(f"{key} 缺少验收规则、目标页面或状态无效")
            elif criterion.get("required") is False and not text(criterion.get("scope_note")):
                problems.append(f"{key} 非必测须说明依据")
            else:
                criteria[key] = (req_id, criterion)
                if criterion.get("required", True) and criterion.get("status") == "pending":
                    problems.append(f"必测验收点待确认：{key}")
    for case_id, case in planned.items():
        req_id = case.get("req")
        if not text(req_id) or req_id not in reqs:
            problems.append(f"用例 {case_id} 关联需求不存在")
        steps = case.get("steps")
        if not isinstance(steps, list) or not steps or not all(map(text, steps)) or not text(case.get("page")):
            problems.append(f"用例 {case_id} 缺少步骤或目标页面")
        expectations = case.get("expected")
        if not isinstance(expectations, dict) or not expectations:
            problems.append(f"用例 {case_id} 缺少逐验收点的预期值")
            continue
        for key in expectations:
            if key not in criteria or criteria[key][0] != req_id or criteria[key][1]["page"] != case.get("page"):
                problems.append(f"用例 {case_id} 关联验收点无效：{key}")
            else:
                covered.add(key)
    required = {key for key, (_, criterion) in criteria.items() if criterion.get("required", True)}
    if not required:
        problems.append("没有已登记的必测验收点")
    problems.extend(f"必测验收点无用例：{key}" for key in sorted(required - covered))
    return planned, criteria, problems


def check_page(directory, run, page, observation):
    """将当前浏览器观察和已确认的页面路由、角色及可见特征比对。"""
    origin = url_key(run.get("url"))
    target = url_key(page.get("url"))
    actual = url_key(observation.get("url"))
    if page.get("status") != "confirmed" or not text(page.get("entry")):
        raise ValueError("页面入口未确认")
    if not origin or not target or origin[:3] != target[:3] or actual != target:
        raise ValueError("实际页面路由或来源与已确认页面不符")
    if not text(page.get("role")) or observation.get("role") != page["role"]:
        raise ValueError("实际角色与目标页面要求不符")
    anchors = page.get("anchors")
    if (not isinstance(anchors, list) or not all(map(text, anchors))
            or len(set(anchors)) < 2):
        raise ValueError("页面至少需要两个不同的可见特征")
    file = evidence_file(directory, observation.get("snapshot"))
    snapshot = file.read_text(encoding="utf-8")
    if any(anchor not in snapshot for anchor in anchors):
        raise ValueError("浏览器快照缺少目标页面特征")
    role_marker = page.get("role_marker")
    if not text(role_marker) or role_marker not in snapshot:
        raise ValueError("浏览器快照缺少当前角色特征")
    return hashlib.sha256(file.read_bytes()).hexdigest()


def check_result(directory, run, case, page, criteria, observation):
    """按预期和实测值计算状态，返回证据摘要供门禁复核。"""
    if not isinstance(observation, dict):
        raise ValueError("缺少结构化实测记录")
    if not page:
        raise ValueError("用例的目标页面尚未登记")
    confirmed = {
        "url": page.get("url"), "role": page.get("role"), "snapshot": page.get("snapshot"),
    }
    confirmed_hash = check_page(directory, run, page, confirmed)
    snapshot_hash = check_page(directory, run, page, observation)
    expectations = case.get("expected")
    if not isinstance(expectations, dict) or not expectations:
        raise ValueError("用例没有逐验收点的预期值")
    basis = {}
    for key in expectations:
        requirement_id, criterion = criteria.get(key, (None, {}))
        if (requirement_id != case.get("req") or criterion.get("page") != case.get("page")
                or criterion.get("status") == "pending"):
            raise ValueError(f"{key} 的验收点或目标页面未确认")
        basis[key] = criterion
    assertions = observation.get("assertions")
    if not isinstance(assertions, dict) or set(assertions) != set(expectations):
        raise ValueError("实测断言与计划验收点不一致")
    fingerprints = {"snapshot": snapshot_hash, "confirmed": confirmed_hash,
                    "plan": digest([run.get("run_id"), run.get("version"), case, page, basis]),
                    "observation": digest(observation)}
    passed = True
    for key, expected in expectations.items():
        assertion = assertions[key]
        if not isinstance(assertion, dict) or "actual" not in assertion:
            raise ValueError(f"{key} 缺少实测值")
        file = evidence_file(directory, assertion.get("evidence"))
        fingerprints[key] = hashlib.sha256(file.read_bytes()).hexdigest()
        passed = digest(expected) == digest(assertion["actual"]) and passed
    return "passed" if passed else "failed", fingerprints
