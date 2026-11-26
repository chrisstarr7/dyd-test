"""从已验证的历史缺陷提取项目建议，由独立基准和明确审批约束晋升。"""
import argparse
import json
import re
import subprocess
from pathlib import Path

from qa import assess, load_cases, load_pages
from qa_api import LOOPBACK
from qa_benchmark import compare
from qa_browser import redact_data
from qa_contract import digest, text, url_key
from qa_plan import verify_frozen


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def candidate(directory, defect_id, project_id, subject, conditions, exclusions, method):
    """只从当前可复核的已关闭缺陷构建建议，丢弃原始测试数据。"""
    review = assess(directory)
    run = review["run"]
    url = url_key(run.get("url"))
    if (not url or url[0] != "https" or url[1] in LOOPBACK | {"localhost"}
            or run.get("qa_contract") != 2 or run.get("project_id") != project_id):
        raise ValueError("候选只能来自获授权的真实项目证据")
    pages, errors = load_pages(directory)
    if errors:
        raise ValueError("来源运行的目标无效")
    verify_frozen(directory, run, read_json(directory / "requirements.json"),
                  load_cases(directory / "test-cases.json"), pages)
    matches = [item for item in review["defects"] if item.get("id") == defect_id]
    if len(matches) != 1:
        raise ValueError("未找到唯一的已确认缺陷")
    defect = matches[0]
    retest = review["results"].get(defect.get("retest_case_id"), {})
    if (defect.get("status") != "closed" or defect.get("fix_version") != run["version"]
            or retest.get("state") != "passed" or defect_id not in retest.get("defect_ids", [])):
        raise ValueError("缺少可信、明确关联的通过复测")
    value = {"kind": "experience", "project_id": project_id, "version": run["version"],
             "subject": subject, "conditions": conditions, "exclusions": exclusions, "method": method,
             "source": {"run": run["run_id"], "defect": defect_id, "case": defect["retest_case_id"],
                        "evidence_sha256": digest(retest["fingerprints"])}}
    validate(value)
    return value


def validate(value):
    if (not isinstance(value, dict) or value.get("kind") not in ("fact", "defect", "experience")
            or not isinstance(value.get("project_id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", value["project_id"])
            or not all(text(value.get(key)) and len(value[key]) <= 500
                       for key in ("version", "subject", "conditions", "exclusions", "method"))
            or not isinstance(value.get("source"), dict) or not value["source"]
            or redact_data(value) != value):
        raise ValueError("记录缺少项目作用域、版本、来源或非敏感的适用条件")


def approve(value, decision, truth, baseline, challenger):
    """重算比较结果，并校验操作者决定绑定当前候选与完整比较结果。"""
    validate(value)
    if value["kind"] != "experience" or not isinstance(decision, dict):
        raise ValueError("只能晋升明确的候选经验")
    result = compare(truth, baseline, challenger)
    if (result["status"] != "passed" or truth.get("candidate_sha256") != digest(value)
            or decision.get("decision") != "approved" or decision.get("candidate_sha256") != digest(value)
            or decision.get("comparison_sha256") != digest(result)
            or not all(text(decision.get(key)) for key in ("by", "reference"))
            or redact_data(decision) != decision):
        raise ValueError("缺少绑定当前候选和独立评估结果的显式审批，或评估失败")
    return {"record": value, "approval": {key: decision[key] for key in
                                         ("decision", "candidate_sha256", "comparison_sha256", "by", "reference")}}


def selected_records(records, project_id, version, query, overridden):
    """返回相关已批准建议；同主题冲突及被当前契约覆盖的记录不参与规划。"""
    eligible = {}
    for item in records:
        value, approval = item["record"], item["approval"]
        validate(value)
        if (value["project_id"] != project_id or value["version"] != version or value["subject"] in overridden
                or approval.get("decision") != "approved" or approval.get("candidate_sha256") != digest(value)
                or not all(text(approval.get(key)) for key in ("by", "reference"))
                or (value["kind"] == "experience" and not re.fullmatch(r"[a-f0-9]{64}",
                                                                        approval.get("comparison_sha256", "")))):
            continue
        if any(term.casefold() in " ".join(value[key] for key in ("subject", "method", "conditions")).casefold()
               for term in query):
            eligible.setdefault(value["subject"], {})[digest(value)] = value
    conflicts = [subject for subject, versions in eligible.items() if len(versions) > 1]
    return {"advice": [next(iter(items.values())) for subject, items in sorted(eligible.items())
                       if subject not in conflicts][:8], "conflicts": sorted(conflicts)}


def recall(repo, ref, project_id, version, query, overridden):
    """只从项目治理仓库已提交的指定版本读取建议，可指定之前的批准版本回滚。"""
    if not re.fullmatch(r"[a-f0-9]{32}", project_id) or not query or not all(map(text, query)):
        raise ValueError("提供稳定项目 ID 和相关主题")
    git = ["git", "-C", str(repo)]
    commit = subprocess.run([*git, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
                            capture_output=True, check=True, text=True, timeout=5).stdout.strip()
    result = subprocess.run([*git, "show", f"{commit}:qa-experience/{project_id}.json"],
                            capture_output=True, encoding="utf-8", timeout=5)
    if result.returncode != 0:
        return {"status": "not_configured", "commit": commit, "advice": [], "conflicts": []}
    if len(result.stdout) > 512_000:
        raise ValueError("项目经验目录过大")
    return {"status": "available", "commit": commit,
            **selected_records(json.loads(result.stdout), project_id, version, query, overridden)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    proposal = commands.add_parser("candidate")
    proposal.add_argument("--run", type=Path, required=True)
    for field in ("defect", "project-id", "subject", "conditions", "exclusions", "method"):
        proposal.add_argument(f"--{field}", required=True)
    promotion = commands.add_parser("approve")
    for field in ("proposal", "decision", "truth", "baseline", "challenger"):
        promotion.add_argument(f"--{field}", type=Path, required=True)
    search = commands.add_parser("recall")
    for field in ("repo", "ref", "project-id", "version"):
        search.add_argument(f"--{field}", required=True)
    search.add_argument("--query", action="append", required=True)
    search.add_argument("--overridden", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "candidate":
            result = candidate(args.run, args.defect, args.project_id, args.subject, args.conditions,
                               args.exclusions, args.method)
        elif args.command == "approve":
            result = approve(read_json(args.proposal), read_json(args.decision), read_json(args.truth),
                             args.baseline, args.challenger)
        else:
            result = recall(args.repo, args.ref, args.project_id, args.version, args.query, args.overridden)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        print(json.dumps({"status": "blocked", "reason": "来源、审批、评估或项目配置未核验"}, ensure_ascii=False))
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
