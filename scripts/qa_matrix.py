"""校验召回、排序、搜索和生成类非确定功能的受控测试矩阵。"""
from __future__ import annotations

import hashlib
import json
import re

from qa_contract import digest, evidence_file, text, url_key


NONDETERMINISTIC_KINDS = {"recall", "ranking", "search", "generative"}
QUERY_ROLES = {"positive", "negative", "sample"}
PROMPT_LENGTHS = {"short", "medium", "long"}
ENVIRONMENT_FIELDS = ("id", "url", "principal_ref", "tenant", "version", "data_state")
LAYER_LABELS = {
    "decision": "检索决策层",
    "retrieval": "检索层",
    "injection": "上下文注入层",
    "citation": "引用计数层",
    "assertion": "结果断言层",
}


def is_nondeterministic(case):
    return isinstance(case, dict) and case.get("test_kind") in NONDETERMINISTIC_KINDS


def _bounds(value):
    """读取计数期望的上下界；整数表示精确值。"""
    if type(value) is int:
        return value, value
    if isinstance(value, dict) and set(value) <= {"min", "max"}:
        low, high = value.get("min"), value.get("max")
        if low is None and high is None:
            return None, None
        if low is not None and type(low) is not int:
            return None, None
        if high is not None and type(high) is not int:
            return None, None
        if low is not None and high is not None and low > high:
            return None, None
        return low, high
    return None, None


def _positive_expectation(value):
    low, _ = _bounds(value)
    return low is not None and low > 0


def _zero_expectation(value):
    if type(value) is int:
        return value == 0
    _, high = _bounds(value)
    return high is not None and high == 0


def _matches(expected, actual):
    """只比较冻结期望中声明的指标，其他指标作为诊断信息保留。"""
    for key, rule in expected.items():
        if not isinstance(actual, dict) or key not in actual:
            return False, key
        value = actual[key]
        if type(value) is int:
            low, high = _bounds(rule)
            if low is not None and value < low:
                return False, key
            if high is not None and value > high:
                return False, key
        elif digest(rule) != digest(value):
            return False, key
    return True, ""


def validate_plan(case):
    """在冻结前校验矩阵是否具备对照、重复和单因子隔离。"""
    if not isinstance(case, dict):
        return []
    if not case.get("test_kind"):
        if "matrix" in case:
            return [f"用例 {case.get('id', '?')} 登记了矩阵但未声明 test_kind"]
        return []
    case_id = case.get("id", "?")
    kind = case.get("test_kind")
    if kind not in NONDETERMINISTIC_KINDS:
        return [f"用例 {case_id} 的 test_kind 无效：{kind}"]
    matrix = case.get("matrix")
    errors = []
    if not isinstance(matrix, dict):
        return [f"非确定用例 {case_id} 缺少冻结测试矩阵"]
    if not text(matrix.get("evidence")):
        errors.append(f"非确定用例 {case_id} 缺少矩阵证据文件")
    repeats = matrix.get("repeats")
    if type(repeats) is not int or repeats < 2:
        errors.append(f"非确定用例 {case_id} 单次样本不能判定，repeats 至少为 2")
    environment = matrix.get("environment")
    if (not isinstance(environment, dict)
            or any(not text(environment.get(field)) for field in ENVIRONMENT_FIELDS)
            or not url_key(environment.get("url"))):
        errors.append(f"非确定用例 {case_id} 的账号、租户、版本、环境和数据状态未固定")
    queries = matrix.get("queries")
    if not isinstance(queries, list) or not queries:
        return errors + [f"非确定用例 {case_id} 缺少查询矩阵"]
    indexed = {}
    for item in queries:
        if not isinstance(item, dict):
            errors.append(f"非确定用例 {case_id} 的查询项无效")
            continue
        query_id = item.get("id")
        if not isinstance(query_id, str) or not re.fullmatch(r"[A-Z][A-Z0-9-]*-\d+", query_id):
            errors.append(f"非确定用例 {case_id} 的查询编号无效")
            continue
        if query_id in indexed:
            errors.append(f"非确定用例 {case_id} 的查询编号重复：{query_id}")
            continue
        indexed[query_id] = item
        if item.get("role") not in QUERY_ROLES:
            errors.append(f"查询 {query_id} 的 role 无效")
        if not text(item.get("prompt")):
            errors.append(f"查询 {query_id} 缺少原始 prompt")
        factors = item.get("factors")
        if (not isinstance(factors, dict) or not text(factors.get("intent"))
                or factors.get("prompt_length") not in PROMPT_LENGTHS):
            errors.append(f"查询 {query_id} 缺少 intent 或 short/medium/long 长度因子")
        expected = item.get("expect")
        if not isinstance(expected, dict) or not expected:
            errors.append(f"查询 {query_id} 缺少可核验的期望指标")
        citation_min = item.get("citation_min")
        if citation_min is not None and (type(citation_min) is not int or citation_min < 0):
            errors.append(f"查询 {query_id} 的 citation_min 必须是非负整数")
    controls = matrix.get("controls")
    if not isinstance(controls, dict):
        errors.append(f"非确定用例 {case_id} 缺少正负对照")
        controls = {}
    positive = controls.get("positive")
    negative = controls.get("negative")
    for role, values in (("positive", positive), ("negative", negative)):
        if (not isinstance(values, list) or not values
                or not all(text(value) for value in values)
                or len(values) != len(set(values))):
            errors.append(f"非确定用例 {case_id} 缺少有效的 {role} 对照")
            continue
        for query_id in values:
            if query_id not in indexed:
                errors.append(f"{role} 对照未登记：{query_id}")
            elif indexed[query_id].get("role") != role:
                errors.append(f"{role} 对照 {query_id} 的 role 不一致")
    for query_id in positive if isinstance(positive, list) else []:
        query = indexed.get(query_id)
        if query and not any(_positive_expectation(value) for value in query.get("expect", {}).values()):
            errors.append(f"正向对照 {query_id} 没有要求命中的指标")
    for query_id in negative if isinstance(negative, list) else []:
        query = indexed.get(query_id)
        if query and not all(_zero_expectation(value) for value in query.get("expect", {}).values()):
            errors.append(f"负向对照 {query_id} 必须期望零命中")
    intents = {item.get("factors", {}).get("intent") for item in indexed.values()
               if isinstance(item.get("factors"), dict)}
    if len(intents) < 3:
        errors.append(f"非确定用例 {case_id} 至少覆盖 3 种业务意图")
    lengths = {item.get("factors", {}).get("prompt_length") for item in indexed.values()
               if isinstance(item.get("factors"), dict)}
    if not PROMPT_LENGTHS <= lengths:
        errors.append(f"非确定用例 {case_id} 必须覆盖 short/medium/long 查询长度")
    comparisons = matrix.get("comparisons")
    if not isinstance(comparisons, list) or not comparisons:
        errors.append(f"非确定用例 {case_id} 缺少单因子对照")
        comparisons = []
    has_length_pair = False
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            errors.append(f"非确定用例 {case_id} 的对照项无效")
            continue
        base, variant, factor = (comparison.get(field) for field in ("base", "variant", "factor"))
        if base not in indexed or variant not in indexed or base == variant or not text(factor):
            errors.append(f"非确定用例 {case_id} 的对照查询未登记或重复")
            continue
        left = indexed[base].get("factors", {})
        right = indexed[variant].get("factors", {})
        if not isinstance(left, dict) or not isinstance(right, dict) or set(left) != set(right):
            errors.append(f"对照 {base}/{variant} 的因子集合不一致")
            continue
        changed = {key for key in left if digest(left[key]) != digest(right[key])}
        if changed != {factor}:
            errors.append(f"对照 {base}/{variant} 必须只改变 {factor}")
            continue
        if factor == "prompt_length" and {left[factor], right[factor]} == {"short", "long"}:
            has_length_pair = True
    if not has_length_pair:
        errors.append(f"非确定用例 {case_id} 缺少只改变长度的 short/long 对照")
    return errors


def review(directory, run, case):
    """读取矩阵证据并核对重复稳定性；返回可写入执行记录的摘要与哈希。"""
    if not is_nondeterministic(case):
        return None
    before = validate_plan(case)
    if before:
        return {"passed": False, "errors": before, "sha256": "", "summary": {"errors": before}}
    matrix = case["matrix"]
    path = evidence_file(directory, matrix["evidence"])
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = []
    if not isinstance(data, dict):
        raise ValueError("矩阵证据必须是 JSON 对象")
    if data.get("case_id") != case.get("id"):
        errors.append("矩阵证据的 case_id 与用例不一致")
    if digest(data.get("environment")) != digest(matrix.get("environment")):
        errors.append("矩阵执行环境与冻结环境不一致")
    environment = matrix.get("environment", {})
    if url_key(environment.get("url")) != url_key(run.get("url")):
        errors.append("矩阵环境 URL 与运行 URL 不一致")
    if text(run.get("version")) and environment.get("version") != run.get("version"):
        errors.append("矩阵版本与运行版本不一致")
    planned = {item["id"]: item for item in matrix["queries"]}
    observations = data.get("queries")
    if not isinstance(observations, list):
        raise ValueError("矩阵证据缺少 queries 观测列表")
    grouped = {}
    row_paths = set()
    for item in observations:
        if not isinstance(item, dict) or item.get("query_id") not in planned:
            errors.append("矩阵证据包含未登记的查询")
            continue
        grouped.setdefault(item["query_id"], []).append(item)
    repeats = matrix["repeats"]
    items = []
    all_layers = set()
    flaky = False
    for query_id, query in planned.items():
        rows = grouped.get(query_id, [])
        seen = [row.get("repeat") for row in rows]
        if (len(rows) != repeats
                or not all(type(value) is int for value in seen)
                or sorted(seen) != list(range(1, repeats + 1))):
            errors.append(f"查询 {query_id} 未按 {repeats} 次完整重复")
        matched = 0
        actuals = set()
        layers = set()
        for row in sorted(rows, key=lambda value: value.get("repeat", 0)):
            if not text(row.get("observed_at")):
                errors.append(f"查询 {query_id} 第 {row.get('repeat', '?')} 次缺少采样时间")
            row_paths.add(evidence_file(directory, row.get("evidence")))
            actual = row.get("actual")
            if not isinstance(actual, dict):
                errors.append(f"查询 {query_id} 第 {row.get('repeat', '?')} 次缺少 actual")
                continue
            actuals.add(digest(actual))
            ok, key = _matches(query["expect"], actual)
            row_layers = set()
            recall_evidence_valid = False
            if case["test_kind"] == "recall":
                retrieved = row.get("retrieved")
                injected = row.get("injected")
                dropped = row.get("dropped")
                if (not isinstance(row.get("search_invoked"), bool)
                        or not isinstance(retrieved, dict) or not isinstance(injected, dict)
                        or not isinstance(dropped, dict) or type(row.get("citation_delta")) is not int
                        or any(type(value) is not int or value < 0 for value in list(retrieved.values()) + list(injected.values()))):
                    errors.append(f"查询 {query_id} 第 {row.get('repeat', '?')} 次缺少召回分层证据")
                else:
                    recall_evidence_valid = True
                    requires_positive = any(
                        (_bounds(rule)[0] or 0) > 0 for rule in query["expect"].values()
                    )
                    if row["search_invoked"] is not True and requires_positive:
                        row_layers.add("decision")
                    for name, rule in query["expect"].items():
                        low, _ = _bounds(rule)
                        if low is None or low <= 0:
                            continue
                        if retrieved.get(name, 0) < low:
                            row_layers.add("retrieval")
                        elif injected.get(name, 0) < low:
                            row_layers.add("injection")
                    for name, count in retrieved.items():
                        if count > injected.get(name, 0) and not text(dropped.get(name)):
                            row_layers.add("injection")
                            errors.append(f"查询 {query_id} 第 {row.get('repeat', '?')} 次"
                                          f"未说明 {name} 的丢弃原因")
                    for name, value in query["expect"].items():
                        if name in actual and type(actual[name]) is int and actual[name] != injected.get(name, 0):
                            errors.append(f"查询 {query_id} 第 {row.get('repeat', '?')} 次"
                                          f"的 actual 与注入层 {name} 不一致")
                    citation_min = query.get("citation_min")
                    if type(citation_min) is int and row["citation_delta"] < citation_min:
                        row_layers.add("citation")
            if not ok and not row_layers and (case["test_kind"] != "recall" or recall_evidence_valid):
                row_layers.add("assertion")
            if ok and not row_layers and (case["test_kind"] != "recall" or recall_evidence_valid):
                matched += 1
                continue
            if not row_layers:
                continue
            layers.update(row_layers)
            all_layers.update(row_layers)
            labels = "、".join(
                LAYER_LABELS[layer] for layer in
                ("decision", "retrieval", "injection", "citation", "assertion")
                if layer in row_layers
            )
            detail = f"未命中 {key}" if not ok else "最终指标命中但证据层未通过"
            errors.append(f"查询 {query_id} 第 {row.get('repeat', '?')} 次 {detail}（{labels}）")
        if matched not in (0, len(rows)) or len(actuals) > 1:
            flaky = True
            errors.append(f"查询 {query_id} 的 {len(rows)} 次重复结果不稳定")
        if query.get("role") == "positive" and matched == 0:
            errors.append(f"正向对照 {query_id} 在 {len(rows)} 次重复中未命中")
        if query.get("role") == "negative" and matched != len(rows):
            errors.append(f"负向对照 {query_id} 出现误命中")
        items.append({
            "id": query_id,
            "role": query.get("role"),
            "intent": query.get("factors", {}).get("intent"),
            "prompt_length": query.get("factors", {}).get("prompt_length"),
            "trigger": query.get("factors", {}).get("trigger"),
            "matched": matched,
            "repeats": len(rows),
            "flaky": matched not in (0, len(rows)) or len(actuals) > 1,
            "layers": sorted(layers),
        })
    summary = {
        "case_id": case.get("id"),
        "kind": case.get("test_kind"),
        "passed": not errors,
        "queries": len(planned),
        "observations": len(observations),
        "repeats": repeats,
        "flaky": flaky,
        "layers": sorted(all_layers),
        "items": items,
        "errors": errors,
    }
    combined = hashlib.sha256(path.read_bytes())
    root = directory.resolve()
    for row_path in sorted(row_paths, key=lambda value: value.relative_to(root).as_posix()):
        combined.update(row_path.relative_to(root).as_posix().encode("utf-8"))
        combined.update(row_path.read_bytes())
    return {
        "passed": not errors,
        "errors": errors,
        "sha256": combined.hexdigest(),
        "summary": summary,
    }
