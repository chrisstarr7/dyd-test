"""把测试范围绑定到需求原文，并在浏览器采样前冻结验收预期。"""
import json

from qa_browser import LAYERS, browser_page, browser_probe
from qa_contract import check_plan, digest, evidence_file, index_objects, text
from qa_oracle import valid_check
from qa_risk import review_risks


RISKS = {"high", "medium", "low"}
SCENARIOS = {"normal", "boundary", "negative", "role", "regression"}


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
        if state == "pending":
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
        if criterion["risk"] == "high" and len(set(scenarios)) < 2 and not text(criterion.get("risk_note")):
            raise ValueError(f"{key} 高风险仅覆盖单一场景，须说明依据")
    frozen = [run.get("run_id"), run.get("mode"), run.get("version"), run.get("url"),
              scope, source.read_text(encoding="utf-8-sig"), requirements, cases, pages]
    issues, risk = review_risks(directory, run, planned, None)
    if issues:
        raise ValueError("；".join(issues))
    if risk["plan_sha256"]:
        frozen.append(risk["plan_sha256"])
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
