"""使用独立标签对已核验证据评分，比较技能的可比运行。"""
import argparse
import json
from pathlib import Path

from qa import assess, load_cases, load_json, load_pages, ratio
from qa_contract import digest, text
from qa_plan import verify_frozen


def score(directory, truth):
    """以一条独立真值用例为统计单位。缺失结果计入漏报和未完成。"""
    review = assess(directory)
    cases, results = review["cases"], review["results"]
    labels = truth.get("cases")
    if (not text(truth.get("cohort")) or not isinstance(labels, dict) or set(labels) != set(cases)
            or any(not isinstance(value, dict) or type(value.get("defective")) is not bool
                   or not text(value.get("risk")) or type(value.get("critical", False)) is not bool
                   for value in labels.values())
            or {item["defective"] for item in labels.values()} != {True, False}):
        raise ValueError("独立标签需与用例逐一对应，并包含正确和有缺陷样本")
    gate = review["gate"]
    if review["run"].get("qa_contract") != 2:
        raise ValueError("基准要求新版已核验采集")
    pages, problems = load_pages(directory)
    if problems:
        raise ValueError("基准目标定义无效")
    requirements = load_json(directory / "requirements.json")
    raw_cases = load_cases(directory / "test-cases.json")
    verify_frozen(directory, review["run"], requirements, raw_cases, pages)
    known = {key for key, value in labels.items() if value["defective"]}
    negatives = set(labels) - known
    reports = review["defects"]
    detected = set()
    erroneous = 0
    false_positive_samples = set()
    for report in reports:
        linked = set(report.get("case_ids", []))
        verified = {key for key in linked & known if results[key]["state"] == "failed"}
        detected.update(verified)
        false_positive_samples.update(linked & negatives)
        erroneous += not verified or bool(linked - known)
    passed = {key for key, result in results.items() if result["state"] == "passed"}
    clean_failed = {key for key in negatives if results[key]["state"] == "failed"}
    observed = {key for key, result in results.items() if result["state"] in ("passed", "failed")}
    falsely_passed = known & passed
    severe = {key for key in falsely_passed if labels[key].get("critical")}
    risks = {value["risk"] for value in labels.values()}
    verified_risks = {risk for risk in risks
                      if all(key in observed for key, label in labels.items() if label["risk"] == risk)}
    blockers = sum(item["state"] in ("blocked", "unknown") for item in results.values())
    residual = sum("未核验回收" in item or "未验证清理" in item or "清理证据无效" in item
                   for item in gate.get("test_reasons", gate["reasons"]))
    summary = {"detected": len(detected), "known_defects": len(known), "reports": len(reports),
               "invalid_reports": erroneous, "false_passes": len(falsely_passed), "critical_false_passes": len(severe),
               "detection_rate": ratio(len(detected), len(known)), "miss_rate": ratio(len(known - detected), len(known)),
               "false_report_share": ratio(erroneous, len(reports)),
               "false_positive_rate": ratio(len(false_positive_samples), len(negatives)),
               "clean_false_failures": len(clean_failed),
               "false_pass_rate": ratio(len(falsely_passed), len(known)),
               "risk_coverage": ratio(len(verified_risks), len(risks)),
               "blocking_rate": ratio(blockers, len(labels)), "completion_rate": ratio(len(observed), len(labels)),
               "residuals": residual, "root_cause_accuracy": None, "root_cause_coverage": None,
               "autonomy_rate": None, "elapsed_seconds": None, "cost": None,
               "by_risk": {risk: {"observed": sum(labels[key]["risk"] == risk for key in observed),
                                  "known_defects": sum(labels[key]["risk"] == risk for key in known),
                                  "detected": sum(labels[key]["risk"] == risk for key in detected)}
                           for risk in sorted(risks)}}
    run = review["run"]
    scope = load_json(directory / "prd-scope.json")
    source = (directory / scope["document"]).read_text(encoding="utf-8-sig")
    risk_path = directory / "risk-review.json"
    risk = load_json(risk_path) if risk_path.is_file() else {}
    risk.pop("run_id", None)
    probability_digests = [(case["id"], digest((directory / case["matrix"]["probability"]["label_file"]).read_bytes().hex()))
                           for case in raw_cases if (case.get("matrix") or {}).get("probability")]
    review_path = directory / "prd-review.json"
    prd_review = load_json(review_path) if review_path.is_file() else None
    signature = digest([run["version"], run["url"], run["mode"], run.get("project_id"), run.get("assurance_contract"),
                        run.get("evidence_contract"), requirements, raw_cases, pages, scope, source, risk, prd_review,
                        probability_digests])
    return signature, summary


def compare(truth, baseline, candidate):
    """已知严重虚假通过、残留和检测能力退化会拒绝晋升。"""
    if baseline.resolve() == candidate.resolve():
        raise ValueError("基线与候选须独立执行")
    if load_json(baseline / "run.json").get("run_id") == load_json(candidate / "run.json").get("run_id"):
        raise ValueError("基线与候选必须使用不同运行编号")
    old_signature, old = score(baseline, truth)
    new_signature, new = score(candidate, truth)
    if old_signature != new_signature:
        raise ValueError("基线与候选被测版本或验收判据不可比")
    regressions = [key for key in ("detected", "risk_coverage", "completion_rate") if new[key] < old[key]]
    regressions.extend(key for key in ("false_passes", "invalid_reports", "blocking_rate") if new[key] > old[key])
    regressions.extend(risk for risk, metrics in new["by_risk"].items()
                       if metrics["detected"] < old["by_risk"][risk]["detected"]
                       or metrics["observed"] < old["by_risk"][risk]["observed"])
    if new["critical_false_passes"] or new["clean_false_failures"] or new["residuals"] or new["completion_rate"] != 1:
        regressions.append("严重虚假通过、正常对照误判、数据残留或运行未完成")
    return {"cohort": truth["cohort"], "truth_sha256": digest(truth), "plan_sha256": new_signature,
            "status": "passed" if not regressions else "failed", "regressions": regressions,
            "baseline": old, "candidate": new}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("truth", "baseline", "candidate"):
        parser.add_argument(f"--{field}", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = compare(json.loads(args.truth.read_text(encoding="utf-8")), args.baseline, args.candidate)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result = {"status": "blocked", "reason": "运行、可比计划或独立标签无法核验"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit({"passed": 0, "failed": 1, "blocked": 2}[result["status"]])


if __name__ == "__main__":
    main()
