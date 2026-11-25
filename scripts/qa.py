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

from qa_browser import PRIVATE_TEXT, SECRET_KEY, read_capture, redact_data, result_from_capture, validate_capture
from qa_contract import check_page, check_plan, check_result, digest, evidence_file, index_objects, objects, text, url_key
from qa_fixtures import (
    close_fixture, fixture_cases, load_context, operation, prepare, reconcile, review_fixtures, verify_attempt,
)
from qa_plan import freeze, verify_frozen
from qa_release import review_admission, review_defects
from qa_risk import review_risks


ALLOWED_STATES = {"planned", "passed", "failed", "blocked", "skipped"}
BLOCKER_LABELS = {
    "requirement": "需求口径", "environment": "环境", "data": "测试数据",
    "authorization": "授权范围", "dependency": "依赖", "tool": "测试工具",
}
OUTCOME_LABELS = {"passed": "测试通过", "failed": "测试失败", "incomplete": "测试未完成"}
RELEASE_LABELS = {"pass": "通过", "fail": "未通过"}
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
    previous = load_json(target / "run.json") if (target / "run.json").exists() else None
    if previous is None or previous.get("fixture_contract"):
        run["fixture_contract"] = 1
    if previous is None or previous.get("qa_contract") == 2:
        run["qa_contract"] = 2
    if previous and previous.get("qa_contract") == 2:
        # 续跑保留发布版本和既有范围，以免冻结计划与历史证据脱节。
        run = previous
    dump_json(target / "run.json", run)
    (target / "execution.jsonl").touch(exist_ok=True)
    for name, default in (("requirements.json", []), ("test-cases.json", {"cases": []}), ("pages.json", []),
                          ("defects.json", []), ("preflight.json", {}),
                          ("fixtures.json", {"boundary": {}, "fixtures": []})):
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


def cmd_freeze_plan(args):
    directory = run_dir(Path(args.dir))
    run = load_json(directory / "run.json")
    pages, errors = load_pages(directory)
    if errors or run.get("qa_contract") != 2:
        raise SystemExit("仅新版运行可冻结有效的 PRD、页面及用例计划")
    try:
        freeze(directory, run, load_json(directory / "requirements.json"),
               load_cases(directory / "test-cases.json"), pages)
    except (OSError, TypeError, ValueError) as error:
        raise SystemExit(f"计划未冻结：{error}")
    print("已冻结 PRD、测试范围与逐项预期")


def cmd_check_cases(args):
    path = Path(args.cases)
    raw = load_cases(path)
    if args.reqs:
        cases, _, problems = check_plan(load_json(Path(args.reqs)), raw)
    else:
        problems = []
        cases = index_objects(raw, "用例", problems)
        for case_id, case in cases.items():
            if not case.get("steps") or not case.get("expected"):
                problems.append(f"用例 {case_id} 缺少步骤或预期")
    run_path = path.parent / "run.json"
    run = load_json(run_path) if run_path.exists() else {}
    for case_id, case in cases.items():
        try:
            operation(run, case)
        except ValueError as error:
            problems.append(f"用例 {case_id}：{error}")
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


def capture_ref(record):
    observation = record.get("observation")
    return observation.get("capture") if isinstance(observation, dict) else None


def evaluate(directory, run, case, pages, criteria, observation, context=None):
    page = pages.get(case.get("page"))
    action = operation(run, case)
    capture_path = None
    if run.get("qa_contract") == 2:
        if not isinstance(observation, dict):
            raise ValueError("缺少浏览器采集引用")
        fixture = None
        if action != "read":
            if context is None or not text(case.get("fixture")):
                raise ValueError("测试数据台账或用例夹具不可用")
            fixture = context[1].get(case["fixture"])
            if not fixture:
                raise ValueError("用例夹具未登记")
        calculated, fingerprints, observed = result_from_capture(
            directory, run, case, page, criteria, observation.get("capture"), fixture,
            before_path=context[2]["attempts"].get(case["id"], {}).get("before", {}).get("path") if fixture else None,
        )
        capture_path = observed["capture"]
    else:
        calculated, fingerprints = check_result(directory, run, case, page, criteria, observation)
        observed = observation
    if action != "read":
        if context is None:
            raise ValueError("测试数据台账不可用")
        attempt = context[2]["attempts"].get(case["id"])
        fingerprints["data_action"] = verify_attempt(
            directory, run, context, case, attempt, pages=pages, path=capture_path,
        )
        if capture_path:
            after = case.get("expected_after", "absent" if action == "delete" else "present")
            if attempt["after_state"] != after:
                calculated = "failed"
                observed["lifecycle_mismatch"] = True
    return calculated, fingerprints, observed


def cmd_record(args):
    directory = run_dir(Path(args.dir))
    state = args.state
    if state not in ALLOWED_STATES | {"observed"} or state == "planned":
        raise SystemExit(f"非法状态：{state}")
    problems = []
    raw_cases = load_cases(directory / "test-cases.json")
    cases = index_objects(raw_cases, "用例", problems)
    if problems or args.case not in cases:
        raise SystemExit(f"用例计划无效或用例未登记：{args.case}")
    run = load_json(directory / "run.json")
    strict = run.get("qa_contract") == 2
    observation = None
    fingerprints = None
    if state in ("observed", "passed", "failed"):
        if not args.result:
            raise SystemExit("PASS/FAIL 必须提供 --result 结构化断言与证据")
        case = cases[args.case]
        pages, problems = load_pages(directory)
        if problems or not text(case.get("page")):
            raise SystemExit("页面计划无效")
        try:
            requirements = load_json(directory / "requirements.json")
            if strict:
                verify_frozen(directory, run, requirements, raw_cases, pages)
                result = Path(args.result)
                observation = {"capture": str(result.resolve()) if result.is_absolute() else args.result}
            else:
                observation = load_json(Path(args.result))
            _, criteria, _ = check_plan(requirements, raw_cases)
            context = load_context(directory) if operation(run, case) != "read" else None
            calculated, fingerprints, observation = evaluate(directory, run, case, pages, criteria, observation, context)
            if state == "observed":
                state = calculated
            if strict and state == "passed":
                history = [item for item in read_execution(directory / "execution.jsonl")
                           if isinstance(item, dict) and item.get("case_id") == args.case
                           and item.get("state") in ("passed", "failed")]
                if history and history[-1].get("state") == "failed":
                    if not args.retry_reason or not text(args.note):
                        raise ValueError("失败后改判通过须说明复测归因")
                    if capture_ref(history[-1]) == observation["capture"]:
                        raise ValueError("复测必须重新采集")
                    if args.retry_reason == "fix_verified" and not args.defect:
                        raise ValueError("修复回归必须关联缺陷")
        except (OSError, TypeError, ValueError) as error:
            raise SystemExit(str(error))
        if state != calculated:
            raise SystemExit(f"申报状态与实测断言不符，计算结果：{calculated}")
    if state in ("blocked", "skipped") and not text(args.note):
        raise SystemExit("blocked/skipped 必须提供原因 note")
    if args.blocker_kind or args.unblock:
        if state != "blocked" or not args.blocker_kind or not text(args.unblock):
            raise SystemExit("分类阻塞须同时提供 blocked 状态、原因类别和解除条件")
    rec = {
        "case_id": args.case,
        "state": state,
        "ts": now_iso(),
        "observation": observation,
        "fingerprints": fingerprints,
        "defect_ids": args.defect or [],
        "note": redact_text(args.note or ""),
    }
    if args.retry_reason:
        rec["retry_reason"] = args.retry_reason
    if args.blocker_kind:
        rec["blocker_kind"] = args.blocker_kind
        rec["unblock"] = redact_text(args.unblock)
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
        if run.get("qa_contract") == 2:
            validate_capture(directory, run, page, args.snapshot, phases={"page"})
            if url_key(args.url) != url_key(page["url"]) or args.role != page["role"]:
                raise ValueError("命令的页面与角色参数和采集计划不一致")
        else:
            check_page(directory, run, page, {"url": page.get("url"), "role": page.get("role"), "snapshot": page.get("snapshot")})
            check_page(directory, run, page, {"url": args.url, "role": args.role, "snapshot": args.snapshot})
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
    print(f"页面校验通过：{args.page}")


def cmd_fixture(args):
    directory = run_dir(Path(args.dir))
    run = load_json(directory / "run.json")
    errors = []
    raw_cases = load_cases(directory / "test-cases.json")
    cases = index_objects(raw_cases, "用例", errors)
    pages, page_errors = load_pages(directory)
    if errors:
        raise SystemExit("用例计划无效")
    try:
        if run.get("qa_contract") == 2:
            if page_errors:
                raise ValueError("页面计划无效")
            verify_frozen(directory, run, load_json(directory / "requirements.json"), raw_cases, pages)
        context = load_context(directory)
        if (run.get("qa_contract") == 2 and args.cmd == "prepare-data" and args.setup
                and not any(case.get("fixture") == args.setup for case in cases.values())):
            raise ValueError("备数目标未关联本轮冻结用例")
        cases = fixture_cases(cases, context[1])
        if args.cmd != "close-data":
            case_id = f"setup:{args.setup}" if args.setup else args.case
            if case_id not in cases or (not args.setup and case_id.startswith("setup:")):
                raise ValueError("用例未登记或备数目标不属于本轮拟生成数据")
        if args.cmd == "prepare-data":
            prepare(directory, run, context, cases, case_id, args.tenant, args.state, args.evidence, pages)
        elif args.cmd == "reconcile-data":
            reconcile(directory, run, context, cases, case_id, args.tenant, args.state, args.evidence, pages)
        else:
            close_fixture(directory, run, context, cases, args.fixture, args.tenant, args.evidence, pages)
        staged = directory / "fixture-actions.tmp"
        dump_json(staged, context[2])
        staged.replace(directory / "fixture-actions.json")
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
    print(f"测试数据台账已更新：{args.cmd}")


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
        "id": args.id, "title": redact_text(args.title), "severity": args.severity,
        "status": args.status, "case_ids": args.case or [], "ts": now_iso(),
    })
    dump_json(d / "defects.json", defects)
    print("已记录缺陷", args.id)


def cmd_close_defect(args):
    directory = run_dir(Path(args.dir))
    review = assess(directory)
    run = review["run"]
    found = [item for item in review["defects"] if item.get("id") == args.id]
    if run.get("qa_contract") != 2 or len(found) != 1:
        raise SystemExit("新版运行须有唯一的待关闭缺陷")
    record = dict(found[0], status="closed", fix_version=args.fix_version, retest_case_id=args.retest_case)
    reasons = review_defects(directory, run, [record], review["results"])
    if reasons:
        raise SystemExit("；".join(reasons))
    data = [record if item.get("id") == args.id else item for item in review["defects"]]
    dump_json(directory / "defects.json", data)
    print("已按回归证据关闭缺陷", args.id)


def strict_admission(directory, run, pages, preflight):
    reasons = review_admission(directory, run, preflight)
    if not isinstance(preflight, dict):
        return reasons
    try:
        _, smoke = read_capture(directory, preflight.get("evidence"))
        page = pages.get(smoke.get("page"))
        if not page:
            raise ValueError("冒烟目标页面未登记")
        validate_capture(directory, run, page, preflight.get("evidence"), phases={"page"})
    except (OSError, TypeError, ValueError) as error:
        reasons.append(f"缺少本轮真实页面的冒烟采集：{error}")
    if run.get("mode") != "full":
        reasons.append("定向或回归测试仅能给出本次范围的功能结论")
    return reasons


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
    strict = run.get("qa_contract") == 2
    requirements = load_json(directory / "requirements.json")
    raw_cases = load_cases(directory / "test-cases.json")
    cases, criteria, reasons = check_plan(requirements, raw_cases)
    pages, page_errors = load_pages(directory)
    defect_data = load_json(directory / "defects.json")
    defects = objects(defect_data)
    if not isinstance(defect_data, list) or len(defects) != len(defect_data):
        reasons.append("缺陷记录无效")
    preflight = load_json(directory / "preflight.json")
    reasons.extend(page_errors)
    if strict:
        try:
            verify_frozen(directory, run, requirements, raw_cases, pages)
        except (OSError, TypeError, ValueError) as error:
            reasons.append(f"需求范围或冻结计划未核验：{error}")
    else:
        reasons.extend(preflight_problems(directory, run, preflight))
    fixture_entries = []
    try:
        fixture_context = load_context(directory)
        fixture_errors, fixture_entries = review_fixtures(directory, run, fixture_context, cases, pages)
        reasons.extend(fixture_errors)
    except (OSError, ValueError) as error:
        fixture_context = None
        reasons.append(f"测试数据台账无法核验：{error}")
    latest = {}
    last_measurement = {}
    history = {key: [] for key in cases}
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
            previous = last_measurement.get(case_id, {})
            if strict and previous.get("state") == "failed" and record.get("state") == "passed":
                if (record.get("retry_reason") not in ("observation_invalid", "transient", "fix_verified")
                        or not text(record.get("note"))
                        or capture_ref(previous) == capture_ref(record)
                        or (record.get("retry_reason") == "fix_verified" and not record.get("defect_ids"))):
                    reasons.append(f"用例 {case_id} 复测缺少归因")
                elif record["retry_reason"] == "transient":
                    reasons.append(f"用例 {case_id} 存在待定位的偶发失败")
            if record.get("state") in ("passed", "failed"):
                last_measurement[case_id] = record
            history[case_id].append(record)
            latest[case_id] = record

    planned_by = {key: [] for key in criteria}
    results = {}
    for case_id, case in cases.items():
        try:
            action = operation(run, case)
        except ValueError as error:
            action = None
            reasons.append(f"用例 {case_id}：{error}")
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
                if action is None:
                    raise ValueError("缺少有效的操作分类")
                observation = record.get("observation")
                calculated, fingerprints, recomputed = evaluate(
                    directory, run, case, pages, criteria, observation, fixture_context,
                )
                if calculated != state or fingerprints != record.get("fingerprints") or observation != recomputed:
                    raise ValueError("断言、页面计划或证据与记录时不一致")
            except (OSError, TypeError, ValueError) as error:
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

    for defect in ([] if strict else defects):
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
    risk_review = None
    if strict:
        reasons.extend(review_defects(directory, run, defects, results))
        defect_ids = {item.get("id") for item in defects if text(item.get("id"))}
        for case_id, result in results.items():
            linked = result.get("defect_ids", [])
            if (not isinstance(linked, list) or not all(text(item) and item in defect_ids for item in linked)):
                reasons.append(f"用例 {case_id} 关联的缺陷未登记")
        release_reasons = strict_admission(directory, run, pages, preflight)
        risk_reasons, risk_review = review_risks(directory, run, cases, results)
        reasons.extend(risk_reasons)

    covered = {key for key, ids in planned_by.items() if ids}
    completed = {key for key in covered if all(results[case_id]["state"] in ("passed", "failed")
                                                for case_id in planned_by[key])}
    passed_criteria = {
        key for key in completed if all(
            not results[case_id]["observation"].get("lifecycle_mismatch")
            and digest(cases[case_id]["expected"][key]) == digest(results[case_id]["observation"]["assertions"][key]["actual"])
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
    if strict:
        release = list(dict.fromkeys(reasons + release_reasons))
        outcome = "failed" if counts["failed"] else ("incomplete" if reasons else "passed")
        gate.update(test_verdict=gate["verdict"], test_reasons=gate["reasons"],
                    test_outcome=outcome,
                    release_verdict="fail" if release else "pass", release_reasons=release_reasons,
                    verdict="fail" if release else "pass", reasons=release)
    return {"run": run, "cases": cases, "results": results, "defects": defects, "stats": stats, "gate": gate,
            "criteria": criteria, "sources": sources, "covered": covered,
            "completed": completed, "passed_criteria": passed_criteria, "fixtures": fixture_entries,
            "history": history, "risk_review": risk_review}


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
             "## 结论"]
    if "test_verdict" in gate:
        lines.append(f"- 本轮功能验证：{OUTCOME_LABELS[gate['test_outcome']]}")
        lines += [f"  - {reason}" for reason in gate["test_reasons"]]
        lines.append(f"- 正式发布准入：{RELEASE_LABELS[gate['release_verdict']]}")
        lines += [f"  - {reason}" for reason in gate["release_reasons"]]
    else:
        lines.append(f"- 历史质量门禁：{gate.get('verdict', '未执行')}")
        lines += [f"  - {reason}" for reason in gate.get("reasons", [])]
    if review["fixtures"]:
        lines += ["", "## 测试数据台账"]
        lines += [f"- {entry}" for entry in review["fixtures"]]
    blockers = [(key, item) for key, item in review["results"].items()
                if item["state"] == "blocked" and item.get("blocker_kind") in BLOCKER_LABELS]
    if blockers:
        lines += ["", "## 阻塞与解除条件"]
        for key, item in blockers:
            lines.append(f"- {key} · {BLOCKER_LABELS[item['blocker_kind']]} · {item['note']}"
                         f"。解除条件为 {item.get('unblock', '待确认')}")
    risk = review.get("risk_review")
    if risk and risk["status"] == "reviewed":
        dimensions = {
            "functionality": "功能", "data": "数据", "permission": "权限", "integration": "集成",
            "regression": "回归", "reliability": "可靠性", "performance": "性能", "accessibility": "无障碍",
        }
        status = {
            "verified": "已验证", "open": "待验证", "planned": "已计划", "accepted": "已接受剩余风险",
            "not_applicable": "不适用", "out_of_scope": "不在本轮范围", "unreviewed": "未评审",
        }
        lines += ["", "## 风险覆盖与剩余风险"]
        for name, item in risk["dimensions"].items():
            lines.append(f"- {dimensions[name]} · {status[item.get('coverage', item['status'])]}")
        for key, item in risk["risks"].items():
            lines.append(f"- {key} · {item['severity']} · {item['invariant']} · {status[item['status']]}")
            for scenario_key, scenario in item["scenarios"].items():
                if scenario["status"] != "verified":
                    lines.append(f"  - {scenario_key} · {scenario['layer']} · {scenario['criterion']}"
                                 f" · {scenario['status']}")
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
        layer = {"online_ui": "在线界面", "offline_ui": "隔离客户端"}.get(observation.get("layer"))
        suffix = f" · {layer}" if layer else ""
        lines.append(f"- {case_id} · {case['page']} · {observation['role']} · {observation['url']}{suffix}")
        if observation.get("baseline_evidence"):
            lines.append(f"  - 写入前基线 {observation['baseline_evidence']}")
        for key, assertion in observation["assertions"].items():
            expected = json.dumps(case["expected"][key], ensure_ascii=False)
            actual = json.dumps(assertion["actual"], ensure_ascii=False)
            lines.append(f"  - {key} 预期 {expected}，实测 {actual}，证据 {assertion['evidence']}")
            if "raw_ms" in assertion:
                lines.append(f"    - 本次导航耗时 {assertion['raw_ms']} ms，计划上限 {assertion['max_ms']} ms")
    attempts = [(key, entries) for key, entries in review["history"].items()
                if len(entries) > 1 or any(item.get("note") for item in entries)]
    if attempts:
        lines += ["", "## 执行与复测记录"]
        for key, entries in attempts:
            for item in entries:
                lines.append(f"- {key} · {item.get('ts', '')} · {item.get('state', '')}"
                             f" · {item.get('retry_reason', '初次或常规记录')} · {item.get('note', '')}")
    return redact_text("\n".join(lines) + "\n")


def cmd_report(args):
    directory = run_dir(Path(args.dir))
    review = assess(directory)
    dump_json(directory / "stats.json", review["stats"])
    dump_json(directory / "gate.json", review["gate"])
    if review["risk_review"] and review["risk_review"]["status"] != "not_provided":
        dump_json(directory / "risk-summary.json", redact_data(review["risk_review"]))
    (directory / "report.md").write_text(format_report(review), encoding="utf-8")
    print(f"报告已生成：{directory / 'report.md'}")


def redact_text(text: str) -> str:
    quoted_field = r'("([\w -]+)"\s*:\s*)("(?:[^"\\]|\\.)*"|[\d.+-]+|null|false|true)'
    text = re.sub(quoted_field, lambda match: f'{match[1]}"[REDACTED]"'
                  if SECRET_KEY.search(match[2]) else match[0], text)
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(0).split(":")[0] + ": [REDACTED]" if ":" in m.group(0) else "[REDACTED]", text)
    return PRIVATE_TEXT.sub("[REDACTED]", text)


def cmd_redact(args):
    d = Path(args.dir).resolve()
    changed = 0
    for p in d.rglob("*"):
        if (p.is_file() and d in p.resolve().parents
                and p.suffix.lower() in {".md", ".json", ".jsonl", ".txt", ".csv"}):
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            new = redact_text(text)
            # 结构化证据保留合法 JSON，数字形式的手机号也需要脱敏。
            try:
                if p.suffix.lower() == ".json":
                    original = json.loads(text)
                    clean = redact_data(original)
                    if clean != original:
                        new = json.dumps(clean, ensure_ascii=False, indent=2)
                elif p.suffix.lower() == ".jsonl":
                    original = [json.loads(line) for line in text.splitlines() if line.strip()]
                    clean = redact_data(original)
                    if clean != original:
                        new = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in clean)
            except ValueError:
                pass
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

    s = sub.add_parser("freeze-plan", help="冻结原 PRD、页面及逐项预期")
    s.add_argument("--dir", required=True)
    s.set_defaults(func=cmd_freeze_plan)

    s = sub.add_parser("check-page", help="核对当前页面、角色和浏览器快照")
    s.add_argument("--dir", required=True)
    s.add_argument("--page", required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--role", required=True)
    s.add_argument("--snapshot", required=True)
    s.set_defaults(func=cmd_check_page)

    for command, help_text in (("prepare-data", "登记写入意图与基线"), ("reconcile-data", "登记写入后的回查"),
                               ("close-data", "登记已核验的测试数据回收")):
        s = sub.add_parser(command, help=help_text)
        s.add_argument("--dir", required=True)
        s.add_argument("--tenant", required=True)
        s.add_argument("--evidence", required=True)
        if command == "close-data":
            s.add_argument("--fixture", required=True)
        else:
            target = s.add_mutually_exclusive_group(required=True)
            target.add_argument("--case", help="PRD 用例编号")
            target.add_argument("--setup", help="仅备数时本轮拟生成的夹具编号")
            s.add_argument("--state", choices=["present", "absent"], required=True)
        s.set_defaults(func=cmd_fixture)

    s = sub.add_parser("record", help="追加执行记录并做状态流转校验")
    s.add_argument("--dir", required=True)
    s.add_argument("--case", required=True)
    s.add_argument("--state", default="observed")
    s.add_argument("--result", help="浏览器观察及逐验收点断言 JSON")
    s.add_argument("--defect", nargs="*", default=[])
    s.add_argument("--note", default="")
    s.add_argument("--retry-reason", choices=["observation_invalid", "transient", "fix_verified"])
    s.add_argument("--blocker-kind", choices=BLOCKER_LABELS)
    s.add_argument("--unblock", help="解除本次阻塞所需的可确认条件")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("defect", help="记录缺陷（按编号去重）")
    s.add_argument("--dir", required=True)
    s.add_argument("--id", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--severity", required=True)
    s.add_argument("--status", default="new")
    s.add_argument("--case", nargs="*", default=[])
    s.set_defaults(func=cmd_defect)

    s = sub.add_parser("close-defect", help="依据当前版本的通过回归关闭缺陷")
    for field in ("dir", "id", "fix-version", "retest-case"):
        s.add_argument(f"--{field}", required=True)
    s.set_defaults(func=cmd_close_defect)

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
