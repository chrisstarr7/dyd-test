#!/usr/bin/env python3
"""按受信项目配置预留和归还本地测试夹具。"""

import argparse
import json
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from qa_contract import digest, evidence_file, index_objects, text, url_key


OPERATIONS = ("read", "create", "update", "delete")
POOL_DB = ".qa-pool.sqlite3"


def _object(path):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("运行、配置或回执必须为 JSON 对象")
    return value


def _alias(value):
    return (isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value)
            and not re.search(r"\d(?:[-_]?\d){6}", value))


def _origin(value):
    key = url_key(value)
    if not key or urlsplit(value).port == 0 or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("测试 URL 来源无效")
    return key


def _actions(value):
    return (isinstance(value, list) and "release" in value
            and all(action in (*OPERATIONS, "release") for action in value))


def _scope(directory, profile_path, tenant, operation):
    run = _object(evidence_file(directory, "run.json"))
    run_id = run.get("run_id")
    if (not text(run.get("project")) or not text(run_id)
            or not (_alias(run_id) or re.fullmatch(r"\d{8}-\d{6}", run_id))):
        raise ValueError("当前运行编号或项目无效")
    if directory in profile_path.parents:
        raise ValueError("项目授权配置须来自任务目录之外")
    profile = _object(profile_path)
    origin = _origin(run.get("url"))[:3]
    profile_url = _origin(profile.get("url"))
    effects = profile.get("external_effects")
    actions = profile.get("actions")
    if (profile.get("project") != run["project"] or profile.get("environment") != "test"
            or profile_url != (*origin, "/", "", "") or not _alias(tenant)
            or profile.get("tenant") != tenant or run.get("tenant", tenant) != tenant
            or not _alias(profile.get("authorization"))
            or effects not in ("disabled", "test_sink")
            or (effects == "test_sink" and not _alias(profile.get("test_sink")))
            or not _actions(actions) or operation not in actions):
        raise ValueError("当前运行的项目、URL 来源、租户、操作或外部副作用未经配置授权")
    errors = []
    fixtures = index_objects(profile.get("fixtures"), "池夹具", errors)
    references = set()
    candidates = {}
    for fixture_id, fixture in fixtures.items():
        reference = fixture.get("reference")
        allowed = fixture.get("actions")
        if (not _alias(fixture_id) or not _alias(reference) or reference.casefold() in references
                or not _actions(allowed)
                or any(action not in actions for action in allowed)
                or fixture.get("ownership") != "verified"):
            raise ValueError("池夹具引用、归属或授权动作无效")
        references.add(reference.casefold())
        if operation in allowed:
            candidates[fixture_id] = (digest([origin, tenant.casefold(), reference.casefold()]), reference)
    if errors:
        raise ValueError("池夹具编号无效或重复")
    owner = digest([str(directory), run["project"], run_id, origin, tenant])
    return run_id, owner, candidates


def _receipt(fixture_id, tenant, reference, run_id, operation, lease_id, released):
    return {"source": "pool", "fixture_id": fixture_id, "tenant": tenant,
            "reference": reference, "run_id": run_id, "operation": operation,
            "lease_id": lease_id, "released": released}


def _reserve(db, candidates, owner, tenant, run_id, operation):
    available = None
    for key, (fixture_key, reference) in candidates.items():
        active = db.execute(
            "SELECT lease_id, owner, operation, fixture_id FROM leases WHERE fixture_key=? AND released=0",
            (fixture_key,),
        ).fetchone()
        if not active and available is None:
            available = key, fixture_key, reference
        elif active and active[1:] == (owner, operation, key):
            return _receipt(key, tenant, reference, run_id, operation, active[0], False)
    if available is None:
        raise ValueError("授权池夹具均已预留")
    key, fixture_key, reference = available
    lease_id = uuid.uuid4().hex
    db.execute("INSERT INTO leases VALUES (?, ?, ?, ?, ?, 0)",
               (lease_id, fixture_key, owner, operation, key))
    return _receipt(key, tenant, reference, run_id, operation, lease_id, False)


def _release(db, candidates, previous, owner, tenant, run_id, operation):
    fixture_id = previous.get("fixture_id")
    lease_id = previous.get("lease_id")
    if (not text(fixture_id) or fixture_id not in candidates or not text(lease_id)
            or not re.fullmatch(r"[a-f0-9]{32}", lease_id) or previous.get("released") is not False):
        raise ValueError("须提供本轮有效的预留回执")
    fixture_key, reference = candidates[fixture_id]
    expected = _receipt(fixture_id, tenant, reference, run_id, operation, lease_id, False)
    lease = db.execute(
        "SELECT released FROM leases WHERE lease_id=? AND fixture_key=? AND owner=? AND operation=? AND fixture_id=?",
        (lease_id, fixture_key, owner, operation, fixture_id),
    ).fetchone()
    if previous != expected or lease is None:
        raise ValueError("回执或租约与当前运行的归属不符")
    db.execute("UPDATE leases SET released=1 WHERE lease_id=? AND released=0", (lease_id,))
    return dict(expected, released=True)


def verify_lease(directory, profile_path, fixture, receipt, *, released, active=False, action=None):
    """验证运行回执仍对应项目池真实租约；恢复和在线操作分别核验。"""
    profile_path = Path(profile_path).resolve()
    tenant, operation = fixture["tenant"], receipt.get("operation")
    if operation not in OPERATIONS or receipt.get("released") is not released:
        raise ValueError("测试池回执类型或状态不符")
    run_id, owner, candidates = _scope(directory.resolve(), profile_path, tenant, operation)
    fixture_id = fixture["id"]
    key, reference = candidates.get(fixture_id, (None, None))
    if (not key or reference != fixture.get("reference") or not text(receipt.get("lease_id"))
            or receipt != _receipt(fixture_id, tenant, reference, run_id, operation, receipt["lease_id"], released)):
        raise ValueError("测试池回执与本轮对象不符")
    if action and action not in ("release", operation):
        _, _, allowed = _scope(directory.resolve(), profile_path, tenant, action)
        if fixture_id not in allowed:
            raise ValueError("测试池未授权此操作")
    database = profile_path.parent / POOL_DB
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            state = db.execute(
                "SELECT released FROM leases WHERE lease_id=? AND fixture_key=? AND owner=? AND operation=? AND fixture_id=?",
                (receipt["lease_id"], key, owner, operation, fixture_id),
            ).fetchone()
    except sqlite3.Error as error:
        raise ValueError("无法核验本地测试池租约") from error
    if not state or (released and state[0] != 1) or (active and state[0] != 0):
        raise ValueError("测试池租约未归还、已被归还或不属于本轮")


def main(argv=None):
    """从本轮运行读取边界，仅在成功提交事务后向标准输出写入回执。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("reserve", "release"):
        cli = commands.add_parser(command)
        cli.add_argument("--dir", type=Path, required=True)
        cli.add_argument("--profile", type=Path, required=True)
        cli.add_argument("--tenant", required=True)
        cli.add_argument("--operation", choices=OPERATIONS, required=True)
        if command == "reserve":
            cli.add_argument("--fixture")
        else:
            cli.add_argument("--receipt", required=True, help="任务目录内的原始预留回执")
            cli.add_argument("--evidence", help="新版运行需要实际恢复采集文件")
    args = parser.parse_args(argv)
    try:
        directory = args.dir.resolve()
        profile_path = args.profile.resolve()
        run_id, owner, candidates = _scope(directory, profile_path, args.tenant, args.operation)
        db_path = profile_path.parent / POOL_DB
        if args.command == "reserve":
            if args.fixture is not None:
                candidates = {key: value for key, value in candidates.items() if key == args.fixture}
            if not candidates:
                raise ValueError("池中没有授权执行该操作的夹具")
        else:
            previous = _object(evidence_file(directory, args.receipt))
            if _object(directory / "run.json").get("qa_contract") == 2:
                from qa_fixtures import verify_pool_restore
                verify_pool_restore(directory, previous.get("fixture_id"), args.evidence)
            if not db_path.is_file():
                raise ValueError("找不到预留租约所在的池")
        with closing(sqlite3.connect(str(db_path), timeout=5)) as db, db:
            # 写事务锁涵盖候选选择和归属核对，避免两个进程同时取走或归还夹具。
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS leases (
                lease_id TEXT PRIMARY KEY, fixture_key TEXT NOT NULL, owner TEXT NOT NULL,
                operation TEXT NOT NULL, fixture_id TEXT NOT NULL, released INTEGER NOT NULL
            )""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_fixture ON leases(fixture_key) WHERE released=0")
            if args.command == "reserve":
                receipt = _reserve(db, candidates, owner, args.tenant, run_id, args.operation)
            else:
                receipt = _release(db, candidates, previous, owner, args.tenant, run_id, args.operation)
    except (OSError, sqlite3.Error, RuntimeError):
        parser.exit(1, "无法读取本地配置或操作夹具池\n")
    except (json.JSONDecodeError, UnicodeError):
        parser.exit(1, "运行、配置或回执 JSON 无效\n")
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
