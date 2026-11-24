#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dyd-test deterministic QA core.

All statistics and quality gates are computed from structured records here,
never from free-form reports. Python 3.8+, stdlib only, Windows/macOS safe.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


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
    for name, default in (("requirements.json", []), ("test-cases.json", []),
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


def load_cases(path: Path) -> dict:
    data = load_json(path)
    return {c.get("id") or f"case-{i}": c for i, c in enumerate(data.get("cases", []))}


def cmd_check_cases(args):
    raw = load_json(Path(args.cases)).get("cases", [])
    reqs = load_json(Path(args.reqs)) if args.reqs else []
    req_ids = {r.get("id") for r in reqs}
    problems = []
    seen = {}
    for case in raw:
        cid = case.get("id") or ""
        if not re.fullmatch(r"[A-Z]{1,8}-\d{1,6}", cid):
            problems.append(f"用例编号非法：{cid}")
        if cid in seen:
            problems.append(f"用例编号重复：{cid}")
        seen[cid] = True
        if args.reqs and case.get("req") not in req_ids:
            problems.append(f"用例 {cid} 关联需求不存在：{case.get('req')}")
        if not case.get("steps") or not case.get("expected"):
            problems.append(f"用例 {cid} 缺少步骤或预期（测试数据不完整）")
    for req in req_ids:
        if not any(c.get("req") == req for c in raw):
            problems.append(f"需求 {req} 无关联用例")
    if problems:
        print("\n".join(problems))
        raise SystemExit(1)
    print("用例结构校验通过")


def read_execution(path: Path) -> list:
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    raise SystemExit(f"执行记录损坏：{path}")
    return out


def cmd_record(args):
    d = run_dir(Path(args.dir))
    state = args.state
    if state not in ALLOWED_STATES or state == "planned":
        raise SystemExit(f"非法状态：{state}")
    if state in ("passed", "failed"):
        if not args.evidence:
            raise SystemExit("PASS/FAIL 必须提供证据文件，拒绝将未执行用例标记为通过")
        for ev in args.evidence:
            p = Path(ev)
            if not p.is_absolute():
                p = d / ev
            if not p.exists():
                raise SystemExit(f"证据文件不存在：{p}")
            if not p.resolve().is_relative_to(d.resolve()):
                raise SystemExit(f"证据路径越界（必须在任务目录内）：{p}")
    if state in ("blocked", "skipped") and not args.note:
        raise SystemExit("blocked/skipped 必须提供原因 note")
    rec = {
        "case_id": args.case,
        "state": state,
        "ts": now_iso(),
        "evidence": [str(p) for p in args.evidence],
        "defect_ids": args.defect or [],
        "note": args.note or "",
    }
    with open(d / "execution.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("已记录", args.case, state)


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


def last_state(records: list, case_id: str) -> str:
    states = [r["state"] for r in records if r.get("case_id") == case_id]
    return states[-1] if states else "planned"


def cmd_stats(args):
    d = run_dir(Path(args.dir))
    cases = load_cases(d / "test-cases.json")
    reqs = load_json(d / "requirements.json")
    records = read_execution(d / "execution.jsonl")
    defects = load_json(d / "defects.json")
    planned = len(cases)
    passed = failed = blocked = skipped = 0
    for cid in cases:
        s = last_state(records, cid)
        if s == "passed":
            passed += 1
        elif s == "failed":
            failed += 1
        elif s == "blocked":
            blocked += 1
        elif s == "skipped":
            skipped += 1
    executed = passed + failed
    case_exec_rate = executed / planned if planned else None
    case_pass_rate = passed / executed if executed else None

    req_count = len(reqs)
    designed = sum(1 for r in reqs if any(c.get("req") == r.get("id") for c in cases.values()))
    executed_reqs = sum(1 for r in reqs if cases_for(reqs, r, cases) and all(
        last_state(records, cid) in ("passed", "failed") for cid in cases_for(reqs, r, cases)))
    passed_reqs = sum(1 for r in reqs if cases_for(reqs, r, cases) and all(
        last_state(records, cid) == "passed" for cid in cases_for(reqs, r, cases)))
    stats = {
        "planned_cases": planned,
        "passed": passed, "failed": failed, "blocked": blocked, "skipped": skipped,
        "executed": executed,
        "case_execution_rate": round(case_exec_rate, 4) if case_exec_rate is not None else None,
        "executed_case_pass_rate": round(case_pass_rate, 4) if case_pass_rate is not None else None,
        "requirement_count": req_count,
        "requirement_design_coverage": round(designed / req_count, 4) if req_count else None,
        "requirement_execution_coverage": round(executed_reqs / req_count, 4) if req_count else None,
        "requirement_pass_rate": round(passed_reqs / req_count, 4) if req_count else None,
        "defects_by_severity": {s: sum(1 for x in defects if x.get("severity") == s) for s in DEFECT_SEVERITIES},
        "open_defects_by_severity": {s: sum(1 for x in defects if x.get("severity") == s and x.get("status") != "closed") for s in DEFECT_SEVERITIES},
    }
    dump_json(d / "stats.json", stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def cases_for(reqs, req, cases):
    rid = req.get("id")
    return [cid for cid, c in cases.items() if c.get("req") == rid]


def cmd_gate(args):
    d = run_dir(Path(args.dir))
    cases = load_cases(d / "test-cases.json")
    reqs = load_json(d / "requirements.json")
    records = read_execution(d / "execution.jsonl")
    defects = load_json(d / "defects.json")
    preflight = load_json(d / "preflight.json")
    reasons = []

    if preflight.get("smoke") != "passed":
        reasons.append("冒烟未通过或未记录，正式准入阻塞")
    for cid in cases:
        recs = [r for r in records if r.get("case_id") == cid]
        if not recs:
            reasons.append(f"必测用例未执行：{cid}")
            continue
        state = last_state(records, cid)
        if state == "passed":
            ev = recs[-1].get("evidence") or []
            if not ev:
                reasons.append(f"PASS 用例缺少断言证据：{cid}")
        elif state == "failed":
            reasons.append(f"用例失败未闭环：{cid}")
        elif state == "blocked":
            reasons.append(f"用例阻塞：{cid}")
    for x in defects:
        if x.get("severity") in ("P0", "P1") and x.get("status") != "closed":
            reasons.append(f"P0/P1 缺陷未闭环：{x.get('id')}")
    verdict = "pass" if not reasons else "fail"
    result = {"verdict": verdict, "reasons": reasons, "at": now_iso()}
    dump_json(d / "gate.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if verdict != "pass":
        raise SystemExit(1)


def cmd_report(args):
    d = run_dir(Path(args.dir))
    stats = load_json(d / "stats.json") if (d / "stats.json").exists() else {}
    gate = load_json(d / "gate.json") if (d / "gate.json").exists() else {}
    run = load_json(d / "run.json")
    lines = ["# 测试报告", "",
             f"- 项目：{run.get('project')}", f"- 运行编号：{run.get('run_id')}",
             f"- 测试模式：{run.get('mode')}", f"- 测试环境：{run.get('url') or '未提供'}",
             f"- 版本：{run.get('version') or '未提供'}", "",
             "## 结论", f"- 质量门禁：{gate.get('verdict', '未执行')}"]
    if gate.get("reasons"):
        lines.append("- 未通过原因：")
        lines += [f"  - {r}" for r in gate["reasons"]]
    lines += ["", "## 统计"]
    if not stats:
        lines.append("无有效统计数据")
    else:
        for k in ("planned_cases", "passed", "failed", "blocked", "skipped",
                  "case_execution_rate", "executed_case_pass_rate",
                  "requirement_design_coverage", "requirement_execution_coverage",
                  "requirement_pass_rate"):
            if k in stats:
                lines.append(f"- {k}：{stats[k]}")
    report = "\n".join(lines) + "\n"
    (d / "report.md").write_text(report, encoding="utf-8")
    print(f"报告已生成：{d / 'report.md'}")


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

    s = sub.add_parser("record", help="追加执行记录并做状态流转校验")
    s.add_argument("--dir", required=True)
    s.add_argument("--case", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--evidence", nargs="*", default=[])
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
