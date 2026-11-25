#!/usr/bin/env python3
"""按项目授权预留测试数据，供已登录的测试执行者走正常业务流程。"""

import argparse
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from qa_contract import digest, evidence_file, url_key
from qa_pool import OPERATIONS


SEED_DB = ".qa-seed.sqlite3"
FIELD = r"[A-Za-z][A-Za-z0-9_-]{0,39}"
ALIAS = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"
DOMAIN = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
PHONE = r"\+[1-9][0-9]{6,14}"
MAX_IDENTITIES = 10000
IDENTITIES = ("phone_pool", "phone_range", "email_pool", "sandbox_email")
PUBLIC_FIELDS = ("name", "identifier", "marker")
FIELD_OPTIONS = {
    "name": {"prefix"}, "identifier": {"prefix"}, "marker": set(),
    "email": {"domain"}, "integer": {"min", "max"}, "choice": {"values"}, "secret": set(),
    "phone_pool": {"values"}, "phone_range": {"start", "end"}, "email_pool": {"values"},
    "sandbox_email": {"mailbox", "sink"},
}


class SeedError(ValueError):
    """公开错误只包含预置诊断，不携带配置原值或私有表单值。"""

    def __init__(self, code, message, *, status="denied"):
        super().__init__(message)
        self.code = code
        self.status = status

    def result(self):
        return {"status": self.status, "code": self.code, "message": str(self), "evidence": "none"}


def _matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _alias(value):
    return _matches(ALIAS, value) and not re.search(r"\d(?:[-_]?\d){6}", value)


def _run_alias(value):
    if _alias(value):
        return True
    if not _matches(r"[0-9]{8}(?:-[0-9]{6})?(?:-[a-zA-Z][a-zA-Z0-9-]{0,50})?", value):
        return False
    try:
        stamp = value.split("-")
        with_time = len(stamp) > 1 and stamp[1].isdigit()
        datetime.strptime("-".join(stamp[:2]) if with_time else stamp[0],
                          "%Y%m%d-%H%M%S" if with_time else "%Y%m%d")
        return True
    except ValueError:
        return False


def _unique_keys(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("JSON 包含重复字段")
    return fields


def _read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys)


def _origin(url):
    key = url_key(url)
    if (not key or urlsplit(url).port == 0 or re.search(r"[\x00-\x1f\x7f]", url)
            or not _matches(r"[A-Za-z0-9.:-]+", key[1]) or key[1].endswith(".")):
        return None
    return key


def _actions(actions):
    return (isinstance(actions, list) and all(item in OPERATIONS for item in actions)
            and len(set(actions)) == len(actions) and {"create", "delete"} <= set(actions))


def _email(value):
    if not isinstance(value, str) or len(value) > 254:
        return False
    local, _, domain = value.rpartition("@")
    return (_matches(r"[a-z0-9][a-z0-9_+.-]{0,63}", local.lower()) and not local.endswith(".")
            and ".." not in local and _matches(DOMAIN, domain.lower()))


def _routable_email(value):
    return _email(value) and not value.lower().endswith(".invalid")


def _list(values, predicate):
    return (isinstance(values, list) and 0 < len(values) <= MAX_IDENTITIES
            and all(map(predicate, values)) and len({item.casefold() for item in values}) == len(values))


def _valid_field(spec, profile):
    kind = spec.get("kind") if isinstance(spec, dict) else None
    if not isinstance(kind, str) or kind not in FIELD_OPTIONS or set(spec) != FIELD_OPTIONS[kind] | {"kind"}:
        return False
    if kind in ("name", "identifier"):
        pattern = r"[^\W\d][\w-]{0,19}" if kind == "name" else r"[a-z][a-z0-9-]{0,19}"
        return _matches(pattern, spec["prefix"])
    if kind == "email":
        return (_matches(DOMAIN, spec["domain"]) and spec["domain"].endswith(".invalid")
                and len(spec["domain"]) <= 180)
    if kind == "integer":
        low, high = spec["min"], spec["max"]
        return type(low) is int and type(high) is int and -2**53 < low <= high < 2**53
    if kind == "choice":
        return _list(spec["values"], _alias)
    if kind in ("phone_pool", "email_pool"):
        predicate = _routable_email if kind == "email_pool" else lambda value: _matches(PHONE, value)
        return _list(spec["values"], predicate)
    if kind == "phone_range":
        start, end = spec["start"], spec["end"]
        return (_matches(PHONE, start) and _matches(PHONE, end) and len(start) == len(end)
                and 0 <= int(end) - int(start) < MAX_IDENTITIES)
    if kind == "sandbox_email":
        return (profile["external_effects"] == "test_sink" and spec["sink"] == profile["test_sink"]
                and _routable_email(spec["mailbox"])
                and _matches(r"[a-z0-9][a-z0-9._-]{0,11}", spec["mailbox"].split("@")[0])
                and len(spec["mailbox"].split("@")[1]) <= 180)
    return True


def _valid_recipe(recipe, profile):
    return (isinstance(recipe, dict) and set(recipe) == {"actions", "reference_field", "fields"}
            and _actions(recipe["actions"]) and set(recipe["actions"]) <= set(profile["actions"])
            and isinstance(recipe["fields"], dict) and 1 <= len(recipe["fields"]) <= 99
            and all(_matches(FIELD, field) and _valid_field(spec, profile) for field, spec in recipe["fields"].items())
            and _matches(FIELD, recipe["reference_field"])
            and recipe["fields"].get(recipe["reference_field"], {}).get("kind") in PUBLIC_FIELDS)


def _scope(directory, profile_path, tenant, recipe_name, fixture, operation):
    invalid = SeedError("invalid_profile", "备数配置格式无效")
    if directory in profile_path.parents:
        raise SeedError("untrusted_profile", "授权配置须由项目维护者从任务目录之外提供")
    try:
        run = _read(evidence_file(directory, "run.json"))
    except (OSError, ValueError):
        raise SeedError("invalid_run", "无法读取有效的当前运行") from None
    try:
        profile = _read(profile_path)
    except (OSError, ValueError):
        raise invalid from None
    required = {"seed_contract", "project", "url", "environment", "tenant", "authorization",
                "external_effects", "actions", "namespace", "recipes"}
    if (not isinstance(profile, dict) or not required <= profile.keys()
            or set(profile) - required - {"test_sink"} or type(profile["seed_contract"]) is not int
            or profile["seed_contract"] != 1 or not isinstance(profile["recipes"], dict)
            or not _matches(r"[a-z][a-z0-9-]{0,19}", profile["namespace"])):
        raise invalid
    configured = _origin(profile["url"])
    origin = _origin(run.get("url")) if isinstance(run, dict) else None
    run_id = run.get("run_id") if origin else None
    effects = profile["external_effects"]
    if (not origin or not configured or configured != (*origin[:3], "/", "", "")
            or "?" in profile["url"] or "#" in profile["url"]
            or not _run_alias(run_id)
            or not isinstance(run.get("project"), str) or not run["project"].strip()
            or profile["project"] != run["project"] or profile["environment"] != "test"
            or run.get("environment", "test") != "test" or not _alias(tenant)
            or profile["tenant"] != tenant or run.get("tenant", tenant) != tenant
            or not _alias(profile["authorization"]) or effects not in ("disabled", "test_sink")
            or (effects == "test_sink" and not _alias(profile.get("test_sink")))
            or (effects == "disabled" and "test_sink" in profile)
            or not _actions(profile["actions"]) or operation not in profile["actions"]):
        raise SeedError("scope_denied", "项目、来源、租户、操作或外部副作用未经配置授权")
    if not _matches(FIELD, recipe_name) or not _matches(r"[A-Z][A-Z0-9-]{0,39}-[0-9]{1,6}", fixture):
        raise SeedError("invalid_request", "造数方案名或夹具编号无效")
    if not all(_matches(FIELD, name) and _valid_recipe(recipe, profile)
               for name, recipe in profile["recipes"].items()):
        raise invalid
    if recipe_name not in profile["recipes"]:
        raise SeedError("recipe_unavailable", "当前项目未配置此备数方案", status="blocked")
    recipe = profile["recipes"][recipe_name]
    if operation not in recipe["actions"]:
        raise SeedError("operation_denied", "备数方案未授权当前操作")
    return run_id, origin[:3], profile, recipe


def _identity(spec, suffix, index):
    kind = spec["kind"]
    if kind == "sandbox_email":
        mailbox, domain = spec["mailbox"].split("@")
        return f"{mailbox}+qa-{suffix}@{domain}"
    if kind == "phone_range":
        return f"+{int(spec['start']) + index}"
    return spec["values"][index]


def _capacity(spec):
    if spec["kind"] == "phone_range":
        return int(spec["end"]) - int(spec["start"]) + 1
    return len(spec["values"]) if "values" in spec else 1


def _reserve(db, owner, binding, namespace, fields):
    # 事务覆盖槽位及所有身份，任一字段无法分配时整体回滚。
    db.execute("BEGIN IMMEDIATE")
    db.execute("""CREATE TABLE IF NOT EXISTS seed_slots (
        serial INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL UNIQUE, binding TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS seed_identities (
        fingerprint TEXT PRIMARY KEY, serial INTEGER NOT NULL, field TEXT NOT NULL, idx INTEGER NOT NULL,
        UNIQUE(serial, field)
    )""")
    identity_fields = {field: spec for field, spec in fields if spec["kind"] in IDENTITIES}
    existing = db.execute("SELECT serial, binding FROM seed_slots WHERE owner=?", (owner,)).fetchone()
    if existing:
        serial, previous = existing
        if binding != previous:
            raise SeedError("reservation_conflict", "本轮夹具已绑定其他备数配置或操作，须先核对原预留")
        indexes = dict(db.execute("SELECT field, idx FROM seed_identities WHERE serial=?", (serial,)))
        if (indexes.keys() != identity_fields.keys()
                or any(type(index) is not int or not 0 <= index < _capacity(identity_fields[field])
                       for field, index in indexes.items())):
            raise SeedError("state_unavailable", "已预留身份状态不完整", status="blocked")
        return serial, indexes

    serial = db.execute("INSERT INTO seed_slots(owner, binding) VALUES (?, ?)", (owner, binding)).lastrowid
    indexes = {}
    for position, (field, spec) in enumerate(fields, 1):
        if field not in identity_fields:
            continue
        suffix = f"{namespace}-{serial}-{position}"
        for index in range(_capacity(spec)):
            fingerprint = digest(_identity(spec, suffix, index).casefold())
            claimed = db.execute("INSERT INTO seed_identities VALUES (?, ?, ?, ?) ON CONFLICT(fingerprint) DO NOTHING",
                                 (fingerprint, serial, field, index))
            if claimed.rowcount:
                indexes[field] = index
                break
        else:
            raise SeedError("identity_exhausted", "授权测试身份已占用或耗尽", status="blocked")
    return serial, indexes


def _value(spec, suffix, marker, serial, position, index):
    kind = spec["kind"]
    if kind in PUBLIC_FIELDS:
        return marker if kind == "marker" else f"{spec['prefix']}-{suffix}"
    if kind == "email":
        return f"qa-{suffix}@{spec['domain']}"
    if kind == "integer":
        return spec["min"] + (serial + position - 2) % (spec["max"] - spec["min"] + 1)
    if kind == "choice":
        return spec["values"][(serial + position - 2) % len(spec["values"])]
    if kind == "secret":
        return secrets.token_urlsafe(24) + "Aa1!"
    return _identity(spec, suffix, index)


def _prepare(directory, profile_path, tenant, recipe_name, fixture_id, operation, *, reserve, private=False,
             credential_key=None):
    try:
        directory, profile_path = Path(directory).resolve(), Path(profile_path).resolve()
        run_id, origin, profile, recipe = _scope(directory, profile_path, tenant, recipe_name, fixture_id, operation)
        profile_hash = digest(profile)
        result = {"status": "configured", "evidence": "profile_only", "run_id": run_id, "recipe": recipe_name,
                  "fixture_id": fixture_id, "operation": operation, "profile_sha256": profile_hash}
        if not reserve:
            return result, {}
        if private and credential_key is not None and (not isinstance(credential_key, bytes) or len(credential_key) < 32):
            raise SeedError("credential_key_invalid", "受控测试凭据密钥无效")

        # 字段顺序标准化，JSON 重排不会改变已预留的身份或取值。
        fields = sorted(recipe["fields"].items())
        owner = digest([str(directory), profile["project"], run_id, origin, tenant, fixture_id])
        binding = digest([profile_hash, recipe_name, operation])
        namespace = profile["namespace"]
        with closing(sqlite3.connect(str(profile_path.parent / SEED_DB), timeout=5)) as db, db:
            serial, indexes = _reserve(db, owner, binding, namespace, fields)
        marker = f"qa-{run_id}-{namespace}-{serial}"
        reference_field = recipe["reference_field"]
        values = {field: _value(spec, f"{namespace}-{serial}-{position}", marker, serial, position, indexes.get(field))
                  for position, (field, spec) in enumerate(fields, 1) if private or field == reference_field}
        if private and credential_key is not None:
            # 同一可信进程可用外部密钥重派本轮临时密码，回执与注册表均不保存密钥或密码。
            for field, spec in fields:
                if spec["kind"] == "secret":
                    purpose = digest([owner, binding, serial, field]).encode("ascii")
                    values[field] = hmac.digest(credential_key, purpose, "sha256").hex()[:24] + "Aa1!"
        boundary = {key: profile[key] for key in ("environment", "url", "tenant", "authorization", "external_effects")}
        if "test_sink" in profile:
            boundary["test_sink"] = profile["test_sink"]
        result.update(status="reserved", evidence="local_reservation_only",
                      boundary=dict(boundary, actions=recipe["actions"]),
                      fixture={"id": fixture_id, "source": "generated", "tenant": tenant,
                               "reference": values[reference_field], "marker": marker,
                               "actions": recipe["actions"], "cleanup": "delete"})
        return result, values
    except UnicodeError:
        raise SeedError("invalid_input", "运行或授权配置含有无效字符") from None
    except (OSError, RuntimeError, sqlite3.Error):
        raise SeedError("state_unavailable", "无法安全访问本地预留状态", status="blocked") from None


@contextmanager
def open_seed(directory, profile_path, tenant, recipe, fixture, operation, *, credential_key=None):
    """在已授权执行器进程内提供回执和临时表单值，退出时释放表单映射。"""
    result, values = _prepare(directory, profile_path, tenant, recipe, fixture, operation, reserve=True, private=True,
                              credential_key=credential_key)
    try:
        yield result, values
    finally:
        values.clear()


def main(argv=None):
    """CLI 输出可留档的状态和回执，业务写入仍由正常流程执行。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "prepare"))
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--operation", choices=OPERATIONS, required=True)
    args = parser.parse_args(argv)
    try:
        result, _ = _prepare(args.dir, args.profile, args.tenant, args.recipe, args.fixture, args.operation,
                             reserve=args.command == "prepare")
    except SeedError as error:
        print(json.dumps(error.result()))
        return 2 if error.status == "blocked" else 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
