#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dyd-test deterministic QA core.

All statistics and quality gates are computed from structured records here,
never from free-form reports. Python 3.8+, stdlib only, Windows/macOS safe.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from qa_contract import check_page, check_plan, check_result, digest, evidence_file, index_objects, objects, text, url_key


ALLOWED_STATES = {"planned", "passed", "failed", "blocked", "skipped"}
DEFECT_SEVERITIES = {"P0", "P1", "P2", "P3", "P4"}
SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"),
    re.compile(r"(?i)(token|cookie|secret|access[_-]?key)\s*[:=]\s*\S+"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def run_dir(path: Path) -> Path:
    if not (path / "run.json").exists():
        raise SystemExit(f"不是有效的任务目录（缺少 run.json）：{path}")
    return path


def cmd_init(args):
    out = Path(args.dir)
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out / args.project / run_id
    if target.exists() and not args.force:
        raise SystemExit(f"任务目录已存在（避免重复启动）：{target}")
    target.mkdir(parents=True, exist_ok=True)
    run = {
        "project": args.project,
        "run_id": run_id,
        "mode": args.mode,
        "url": args.url or "",
        "version": args.version or "",
        "status": "initialized",
        "created_at": now_iso(),
        "cases_file": "",
        "requirements_file": "",
    }
    dump_json(target / "run.json", run)
    (target / "execution.jsonl").touch(exist_ok=True)
    for name, default in (("requirements.json", []), ("test-cases.json", {"cases": []}), ("pages.json", []),
                          ("defects.json", []), ("preflight.json", {})):
        p = target / name
        if not p.exists():
            dump_json(p, default)
    print(target)


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="replace")


def parse_case_cell(cell: str) -> dict:
    cell = cell.replace("\ufeff", "").strip()
    lines = [ln.strip() for ln in cell.splitlines() if ln.strip()]
    case = {"id": "", "title": "", "entry": "", "precondition": "", "steps": []}
    m = re.search(r"[A-Z]{1,8}-\d{1,6}", cell)
    case["id"] = m.group(0) if m else ""
    if lines:
        case["title"] = re.sub(r"【?[A-Z]{1,8}-\d{1,6}】?", "", lines[0]).strip()
    for ln in lines:
        if ln.startswith("入口："):
            case["entry"] = ln[len("入口："):].strip()
        elif ln.startswith("前置："):
            case["precondition"] = ln[len("前置："):].strip()
        elif re.match(r"^\d+[、.．]\s*", ln):
            case["steps"].append(re.sub(r"^\d+[、.．]\s*", "", ln))
    return case


def cmd_parse_cases(args):
    raw = Path(args.file).read_bytes()
    text = decode_bytes(raw)
    rows = list(csv.reader(io.StringIO(text)))
    cases = []
    header_seen = False
    for row in rows:
        if all(not (c or "").strip() for c in row):
            continue
        joined = "".join(row)
        if not header_seen and ("步骤" in joined or "预期" in joined or "用例编号" in joined):
            header_seen = True
            continue
        steps_cell = (row[0] if len(row) > 0 else "").strip()
        expected_cell = (row[1] if len(row) > 1 else "").strip()
        if not steps_cell and not expected_cell:
            continue
        case = parse_case_cell(steps_cell)
        case["expected"] = [ln for ln in expected_cell.splitlines() if ln.strip()]
        case["source"] = str(Path(args.file).name)
        cases.append(case)
    out = Path(args.out)
    dump_json(out, {"cases": cases, "count": len(cases)})
    print(f"解析到 {len(cases)} 条用例 -> {out}")


def load_cases(path: Path) -> list:
    data = load_json(path)
    return data.get("cases", []) if isinstance(data, dict) else data


def load_pages(directory):
    path = directory / "pages.json"
    problems = []
    pages = index_objects(load_json(path) if path.exists() else [], "页面", problems)
    return pages, problems


def cmd_check_cases(args):
    raw = load_cases(Path(args.cases))
    if args.reqs:
        _, _, problems = check_plan(load_json(Path(args.reqs)), raw)
    else:
        problems = []
        cases = index_objects(raw, "用例", problems)
        for case_id, case in cases.items():
            if not case.get("steps") or not case.get("expected"):
                problems.append(f"用例 {case_id} 缺少步骤或预期")
    if problems:
        print("\n".join(problems))
        raise SystemExit(1)
    print("用例结构校验通过")


def read_execution(path: Path) -> list:
    out = []
    if path.exists():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f"执行记录第 {line_number} 行损坏") from error
    return out


def cmd_record(args):
    directory = run_dir(Path(args.dir))
    state = args.state
    if state not in ALLOWED_STATES or state == "planned":
        raise SystemExit(f"非法状态：{state}")
    problems = []
    raw_cases = load_cases(directory / "test-cases.json")
    cases = index_objects(raw_cases, "用例", problems)
    if problems or args.case not in cases:
        raise SystemExit(f"用例计划无效或用例未登记：{args.case}")
    observation = None
    fingerprints = None
    if state in ("passed", "failed"):
        if not args.result:
            raise SystemExit("PASS/FAIL 必须提供 --result 结构化断言与证据")
        case = cases[args.case]
        pages, problems = load_pages(directory)
        if problems or not text(case.get("page")):
            raise SystemExit("页面计划无效")
        try:
            observation = load_json(Path(args.result))
            _, criteria, _ = check_plan(load_json(directory / "requirements.json"), raw_cases)
            calculated, fingerprints = check_result(directory, load_json(directory / "run.json"),
                                                    case, pages.get(case["page"]), criteria, observation)
        except (OSError, ValueError) as error:
            raise SystemExit(str(error))
        if state != calculated:
            raise SystemExit(f"申报状态与实测断言不符，计算结果：{calculated}")
    if state in ("blocked", "skipped") and not text(args.note):
        raise SystemExit("blocked/skipped 必须提供原因 note")
    rec = {
        "case_id": args.case,
        "state": state,
        "ts": now_iso(),
        "observation": observation,
        "fingerprints": fingerprints,
        "defect_ids": args.defect or [],
        "note": args.note or "",
    }
    with (directory / "execution.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("已记录", args.case, state)


def cmd_check_page(args):
    directory = run_dir(Path(args.dir))
    pages, problems = load_pages(directory)
    if problems or args.page not in pages:
        raise SystemExit(f"页面计划无效或页面未登记：{args.page}")
    try:
        run = load_json(directory / "run.json")
        page = pages[args.page]
        check_page(directory, run, page, {"url": page.get("url"), "role": page.get("role"), "snapshot": page.get("snapshot")})
        check_page(directory, run, page, {"url": args.url, "role": args.role, "snapshot": args.snapshot})
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
    print(f"页面校验通过：{args.page}")


def cmd_defect(args):
    d = run_dir(Path(args.dir))
    if args.severity not in DEFECT_SEVERITIES:
        raise SystemExit(f"非法缺陷等级：{args.severity}")
    if args.status not in {"new", "fixed", "closed", "activated", "unable"}:
        raise SystemExit(f"非法缺陷状态：{args.status}")
    defects = load_json(d / "defects.json")
    if any(x.get("id") == args.id for x in defects):
        raise SystemExit(f"缺陷编号重复（去重）：{args.id}")
    defects.append({
        "id": args.id, "title": args.title, "severity": args.severity,
        "status": args.status, "case_ids": args.case or [], "ts": now_iso(),
    })
    dump_json(d / "defects.json", defects)
    print("已记录缺陷", args.id)


def preflight_problems(directory, run, preflight):
    if not isinstance(preflight, dict):
        return ["准入记录无效"]
    problems = []
    if preflight.get("smoke") != "passed":
        problems.append("冒烟未通过或未记录，正式准入阻塞")
    if not url_key(run.get("url")) or url_key(preflight.get("url")) != url_key(run.get("url")):
        problems.append("测试环境未确认或与任务配置不一致")
    version = run.get("version")
    if (not text(version) or version.strip().lower() in {"unknown", "n/a", "未确认", "待确认", "未提供", "未知"}
            or preflight.get("version") != version):
        problems.append("测试版本未确认或与任务配置不一致")
    if preflight.get("sql") not in ("verified", "not_applicable") or not text(preflight.get("sql_note")):
        problems.append("增量 SQL 核对或不适用依据缺失")
    try:
        evidence_file(directory, preflight.get("evidence"))
    except ValueError:
        problems.append("缺少准入证据")
    return problems


def ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def assess(directory):
    """从同一批输入生成门禁、用例状态和覆盖指标，拒绝以旧结果放行。"""
    run = load_json(directory / "run.json")
    requirements = load_json(directory / "requirements.json")
    cases, criteria, reasons = check_plan(requirements, load_cases(directory / "test-cases.json"))
    pages, page_errors = load_pages(directory)
    defect_data = load_json(directory / "defects.json")
    defects = objects(defect_data)
    if not isinstance(defect_data, list) or len(defects) != len(defect_data):
        reasons.append("缺陷记录无效")
    preflight = load_json(directory / "preflight.json")
    reasons.extend(page_errors)
    reasons.extend(preflight_problems(directory, run, preflight))
    latest = {}
    try:
        execution = read_execution(directory / "execution.jsonl")
    except (OSError, ValueError):
        reasons.append("执行记录无法读取或格式损坏")
        execution = []
    for record in execution:
        case_id = record.get("case_id") if isinstance(record, dict) else None
        if not text(case_id) or case_id not in cases:
            reasons.append("执行记录含未登记用例或无效记录")
        else:
            latest[case_id] = record

    planned_by = {key: [] for key in criteria}
    results = {}
    for case_id, case in cases.items():
        expected = case.get("expected")
        keys = {key for key in expected if key in criteria and criteria[key][0] == case.get("req")
                and criteria[key][1]["page"] == case.get("page")} if isinstance(expected, dict) else set()
        for key in keys:
            planned_by[key].append(case_id)
        record = latest.get(case_id, {})
        state = record.get("state", "planned")
        required = not keys or any(criteria[key][1].get("required", True) for key in keys)
        if state in ("passed", "failed"):
            try:
                page_id = case.get("page")
                page = pages.get(page_id) if text(page_id) else None
                calculated, fingerprints = check_result(directory, run, case, page, criteria, record.get("observation"))
                if calculated != state or fingerprints != record.get("fingerprints"):
                    raise ValueError("断言、页面计划或证据与记录时不一致")
            except (OSError, ValueError) as error:
                reasons.append(f"用例 {case_id} 的执行证据无效：{error}")
                state = "unknown"
            else:
                if state == "failed":
                    reasons.append(f"用例失败未闭环：{case_id}")
        elif not record:
            if required:
                reasons.append(f"必测用例未执行：{case_id}")
        elif state == "blocked" and text(record.get("note")):
            reasons.append(f"用例阻塞：{case_id}")
        elif state == "skipped" and text(record.get("note")):
            if required:
                reasons.append(f"必测用例被跳过：{case_id}")
        else:
            reasons.append(f"用例状态或记录无效：{case_id}")
            state = "unknown"
        results[case_id] = {**record, "state": state}

    for defect in defects:
        severity = defect.get("severity")
        if defect.get("status") == "closed":
            continue
        if severity in ("P0", "P1"):
            reasons.append(f"P0/P1 缺陷未闭环：{defect.get('id')}")
        if severity == "P2":
            waiver = defect.get("waiver")
            if not (isinstance(waiver, dict) and text(waiver.get("by")) and text(waiver.get("reason"))
                    and text(waiver.get("reference"))
                    and text(waiver.get("version")) and waiver["version"] == run.get("version")):
                reasons.append(f"P2 缺陷无本版本书面让步：{defect.get('id')}")

    covered = {key for key, ids in planned_by.items() if ids}
    completed = {key for key in covered if all(results[case_id]["state"] in ("passed", "failed")
                                                for case_id in planned_by[key])}
    passed_criteria = {
        key for key in completed if all(
            digest(cases[case_id]["expected"][key]) == digest(results[case_id]["observation"]["assertions"][key]["actual"])
            for case_id in planned_by[key]
        )
    }
    sources = {req["id"]: req.get("source") for req in objects(requirements) if text(req.get("id"))}
    req_criteria = {key: set() for key in sources}
    for key, (req_id, _) in criteria.items():
        req_criteria[req_id].add(key)
    counts = {state: sum(record["state"] == state for record in results.values())
              for state in ("passed", "failed", "blocked", "skipped", "unknown")}
    executed = counts["passed"] + counts["failed"]
    stats = {
        "planned_cases": len(cases),
        **counts,
        "executed": executed,
        "case_execution_rate": ratio(executed, len(cases)),
        "executed_case_pass_rate": ratio(counts["passed"], executed),
        "requirement_count": len(sources),
        "criterion_count": len(criteria),
        "defects_by_severity": {s: sum(1 for x in defects if x.get("severity") == s) for s in DEFECT_SEVERITIES},
        "open_defects_by_severity": {s: sum(1 for x in defects if x.get("severity") == s and x.get("status") != "closed") for s in DEFECT_SEVERITIES},
    }
    for name, keys in (("design_coverage", covered), ("execution_coverage", completed), ("pass_rate", passed_criteria)):
        stats[f"criterion_{name}"] = ratio(len(keys), len(criteria))
        stats[f"requirement_{name}"] = ratio(sum(bool(items) and items <= keys for items in req_criteria.values()), len(sources))
    gate = {"verdict": "fail" if reasons else "pass", "reasons": list(dict.fromkeys(reasons)), "at": now_iso()}
    return {"run": run, "cases": cases, "results": results, "defects": defects, "stats": stats, "gate": gate,
            "criteria": criteria, "sources": sources, "covered": covered,
            "completed": completed, "passed_criteria": passed_criteria}


def cmd_stats(args):
    directory = run_dir(Path(args.dir))
    stats = assess(directory)["stats"]
    dump_json(directory / "stats.json", stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def cmd_gate(args):
    directory = run_dir(Path(args.dir))
    result = assess(directory)["gate"]
    dump_json(directory / "gate.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["verdict"] != "pass":
        raise SystemExit(1)


def format_report(review):
    """用已核验的快照生成报告，保留每个已执行断言的预期和实测值。"""
    stats, gate, run = review["stats"], review["gate"], review["run"]
    lines = ["# 测试报告", "",
             f"- 项目：{run.get('project')}", f"- 运行编号：{run.get('run_id')}",
             f"- 测试模式：{run.get('mode')}", f"- 测试环境：{run.get('url') or '未提供'}",
             f"- 版本：{run.get('version') or '未提供'}", "",
             "## 结论", f"- 质量门禁：{gate.get('verdict', '未执行')}"]
    if gate.get("reasons"):
        lines.append("- 未通过原因：")
        lines += [f"  - {r}" for r in gate["reasons"]]
    lines += ["", "## 统计"]
    for key in ("planned_cases", "passed", "failed", "blocked", "skipped", "unknown",
                "case_execution_rate", "executed_case_pass_rate", "requirement_count", "criterion_count",
                "criterion_design_coverage", "criterion_execution_coverage", "criterion_pass_rate"):
        value = stats[key] if stats[key] is not None else "无有效统计数据"
        lines.append(f"- {key}：{value}")
    lines += ["", "## PRD 验收点"]
    for key, (req_id, criterion) in review["criteria"].items():
        status = ("通过" if key in review["passed_criteria"] else "断言失败" if key in review["completed"]
                  else "未完成" if key in review["covered"] else "无关联用例")
        source = review["sources"].get(req_id) or "出处未确认"
        lines.append(f"- {key}（{req_id}，{source}）{criterion['rule']}：{status}")
    lines += ["", "## 断言与证据"]
    for case_id, record in review["results"].items():
        if record["state"] not in ("passed", "failed"):
            continue
        case = review["cases"][case_id]
        observation = record["observation"]
        lines.append(f"- {case_id} · {case['page']} · {observation['role']} · {observation['url']}")
        for key, assertion in observation["assertions"].items():
            expected = json.dumps(case["expected"][key], ensure_ascii=False)
            actual = json.dumps(assertion["actual"], ensure_ascii=False)
            lines.append(f"  - {key} 预期 {expected}，实测 {actual}，证据 {assertion['evidence']}")
    return redact_text("\n".join(lines) + "\n")


def cmd_report(args):
    directory = run_dir(Path(args.dir))
    review = assess(directory)
    dump_json(directory / "stats.json", review["stats"])
    dump_json(directory / "gate.json", review["gate"])
    (directory / "report.md").write_text(format_report(review), encoding="utf-8")
    print(f"报告已生成：{directory / 'report.md'}")


def redact_text(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(0).split(":")[0] + ": [REDACTED]" if ":" in m.group(0) else "[REDACTED]", text)
    return text


def cmd_redact(args):
    d = Path(args.dir)
    changed = 0
    for p in d.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".md", ".json", ".jsonl", ".txt", ".csv"}:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            new = redact_text(text)
            if new != text:
                p.write_text(new, encoding="utf-8")
                changed += 1
    print(f"已脱敏 {changed} 个文件")


def cmd_parse_csv(args):
    cmd_parse_cases(args)


def build_parser():
    p = argparse.ArgumentParser(prog="qa.py", description="dyd-test 确定性测试核心")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="初始化任务目录")
    s.add_argument("--project", required=True)
    s.add_argument("--run-id", default="")
    s.add_argument("--mode", default="full", choices=["full", "targeted", "regression"])
    s.add_argument("--url", default="")
    s.add_argument("--version", default="")
    s.add_argument("--dir", default="dyd-test-output")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("parse-cases", help="解析 CSV 用例（自动识别编码）")
    s.add_argument("--file", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_parse_cases)

    s = sub.add_parser("check-cases", help="用例与需求关联校验")
    s.add_argument("--cases", required=True)
    s.add_argument("--reqs", default="")
    s.set_defaults(func=cmd_check_cases)

    s = sub.add_parser("check-page", help="核对当前页面、角色和浏览器快照")
    s.add_argument("--dir", required=True)
    s.add_argument("--page", required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--role", required=True)
    s.add_argument("--snapshot", required=True)
    s.set_defaults(func=cmd_check_page)

    s = sub.add_parser("record", help="追加执行记录并做状态流转校验")
    s.add_argument("--dir", required=True)
    s.add_argument("--case", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--result", help="浏览器观察及逐验收点断言 JSON")
    s.add_argument("--defect", nargs="*", default=[])
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("defect", help="记录缺陷（按编号去重）")
    s.add_argument("--dir", required=True)
    s.add_argument("--id", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--severity", required=True)
    s.add_argument("--status", default="new")
    s.add_argument("--case", nargs="*", default=[])
    s.set_defaults(func=cmd_defect)

    s = sub.add_parser("stats", help="从结构化记录计算统计")
    s.add_argument("--dir", required=True)
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("gate", help="质量门禁判定")
    s.add_argument("--dir", required=True)
    s.set_defaults(func=cmd_gate)

    s = sub.add_parser("report", help="生成报告")
    s.add_argument("--dir", required=True)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("redact", help="输出目录敏感信息脱敏")
    s.add_argument("--dir", required=True)
    s.set_defaults(func=cmd_redact)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
