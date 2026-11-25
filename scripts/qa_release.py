"""核验正式准入的发布凭据，以及缺陷的复测和书面让步。"""
from __future__ import annotations

import json

from qa_contract import evidence_file, text, url_key


RELEASE_COMPONENTS = {
    "frontend": {"frontend"},
    "backend": {"backend"},
    "fullstack": {"frontend", "backend"},
}
MISSING_VALUES = {"unknown", "n/a", "tbd", "-", "未确认", "待确认", "未提供", "未知", "不适用"}


def _confirmed(value):
    return text(value) and value.strip().lower() not in MISSING_VALUES


def _read_record(path, record_type):
    # shortcut: 本地记录仅校验结构与关联，需要验证来源时接入可信发布或审批系统。
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("type") != record_type or not _confirmed(record.get("source")):
        raise ValueError("证据类型或来源无效")
    return record


def review_admission(directory, run, preflight) -> list[str]:
    """核对本轮 URL、版本、冒烟及独立发布凭据，返回阻塞原因。"""
    if not isinstance(run, dict) or not isinstance(preflight, dict):
        return ["准入记录无效"]
    problems = []
    if preflight.get("smoke") != "passed":
        problems.append("冒烟未通过或未记录，正式准入阻塞")
    url = url_key(run.get("url"))
    version = run.get("version")
    if not url or url_key(preflight.get("url")) != url:
        problems.append("测试环境未确认或与任务配置不一致")
    if not _confirmed(version) or preflight.get("version") != version:
        problems.append("测试版本未确认或与任务配置不一致")
    sql_status = preflight.get("sql")
    if sql_status not in ("verified", "not_applicable") or not _confirmed(preflight.get("sql_note")):
        problems.append("增量 SQL 核对或不适用依据缺失")
    try:
        smoke = evidence_file(directory, preflight.get("evidence"))
    except ValueError:
        smoke = None
        problems.append("缺少冒烟快照证据")
    try:
        source = evidence_file(directory, preflight.get("release_evidence"))
        if smoke is not None and source.samefile(smoke):
            raise ValueError("发布凭据与冒烟快照共用文件")
        release = _read_record(source, "release_record")
    except (OSError, ValueError):
        problems.append("缺少独立的发布凭据，无法核验版本与增量 SQL")
        return problems
    if not url or url_key(release.get("url")) != url or release.get("version") != version:
        problems.append("发布凭据的 URL 或版本与本轮不一致")
    if not any(_confirmed(release.get(key)) for key in ("deployment_id", "build_id")):
        problems.append("发布凭据缺少部署或构建编号")
    scope = release.get("scope")
    expected = RELEASE_COMPONENTS.get(scope) if text(scope) else None
    components = release.get("components")
    if (not expected or not isinstance(components, dict) or components.keys() != expected
            or not all(map(_confirmed, components.values()))):
        problems.append("发布范围或前后端组件构建标识不完整")
    migration = release.get("migration")
    if (not isinstance(migration, dict) or sql_status not in ("verified", "not_applicable")
            or migration.get("status") != sql_status or not _confirmed(migration.get("basis"))
            or (sql_status == "verified" and not _confirmed(migration.get("execution_id")))):
        problems.append("发布凭据缺少本版本增量 SQL 执行记录或不适用依据")
    return problems


def _accepted_waiver(directory, waiver, defect_id, version):
    if (not isinstance(waiver, dict) or not _confirmed(version) or waiver.get("version") != version
            or not _confirmed(waiver.get("by")) or not _confirmed(waiver.get("reason"))):
        return False
    try:
        record = _read_record(evidence_file(directory, waiver.get("reference")), "defect_waiver")
    except (OSError, ValueError):
        return False
    return (record.get("decision") == "accepted" and record.get("defect_id") == defect_id
            and record.get("version") == version and record.get("by") == waiver["by"]
            and record.get("reason") == waiver["reason"])


def review_defects(directory, run, defects, results) -> list[str]:
    """以调用方已核验的最新执行记录检查缺陷闭环，返回阻塞原因。"""
    if not isinstance(run, dict) or not isinstance(defects, list) or not isinstance(results, dict):
        return ["缺陷或复测记录无效"]
    version = run.get("version")
    problems = []
    seen = set()
    for defect in defects:
        defect_id = defect.get("id") if isinstance(defect, dict) else None
        if not text(defect_id):
            problems.append("缺陷编号缺失或无效")
            continue
        if defect_id in seen:
            problems.append(f"缺陷编号重复：{defect_id}")
            continue
        seen.add(defect_id)
        severity, status = defect.get("severity"), defect.get("status")
        if (severity not in ("P0", "P1", "P2", "P3", "P4")
                or status not in ("new", "fixed", "closed", "activated", "unable")):
            problems.append(f"缺陷等级或状态无效：{defect_id}")
        elif status == "closed":
            if not _confirmed(version) or defect.get("fix_version") != version:
                problems.append(f"缺陷关闭缺少本版本修复记录：{defect_id}")
            # 只有明确关联的最新通过结果才能支持本轮关闭。
            case_id = defect.get("retest_case_id")
            retest = results.get(case_id) if text(case_id) else None
            linked = retest.get("defect_ids") if isinstance(retest, dict) else None
            if not isinstance(linked, list) or defect_id not in linked or retest.get("state") != "passed":
                problems.append(f"缺陷关闭缺少关联的通过复测：{defect_id}")
        elif severity in ("P0", "P1"):
            problems.append(f"P0/P1 缺陷未闭环：{defect_id}")
        elif severity == "P2" and not _accepted_waiver(directory, defect.get("waiver"), defect_id, version):
            problems.append(f"P2 缺陷无本版本书面让步：{defect_id}")
    return problems
