"""执行冻结的 HTTP 契约，只持久化判据所需的投影。"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from urllib.parse import quote, urlencode, urlsplit

from qa_browser import SECRET_KEY, redact_data, redacted_measurement, registered_capture, write_capture
from qa_contract import digest, text, url_key


NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
ENV = re.compile(r"DYD_QA_[A-Z][A-Z0-9_]{0,63}\Z")
PATH = re.compile(r"/(?:[A-Za-z0-9_.~/-]|\{[A-Za-z][A-Za-z0-9_-]*\})*\Z")
POINTER = re.compile(r"(?:/(?:[A-Za-z0-9_.-]+|~[01])*)*\Z")
TYPES = {dict: "object", list: "array", str: "string", int: "integer",
         float: "number", bool: "boolean", type(None): "null"}
READ_METHODS = {"GET", "HEAD"}
METHODS = READ_METHODS | {"POST", "PUT", "PATCH", "DELETE"}
LOOPBACK = {"127.0.0.1", "::1"}
MISSING = object()


class Blocked(ValueError):
    """执行条件不足，保留可独立复测的原因。"""


@contextmanager
def run_lock(directory):
    """同一运行的执行与回收互斥；进程退出时操作系统释放锁。"""
    with (directory / ".api.lock").open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Blocked("本轮已有 API 执行或回收正在进行") from None
        yield


def pointer(value, path):
    for part in path.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isascii() and key.isdigit() and len(key) < 6 and int(key) < len(value):
            value = value[int(key)]
        else:
            return MISSING
    return value


def field_path(value):
    return isinstance(value, str) and POINTER.fullmatch(value) and not SECRET_KEY.search(value)


def validate_target(run, target):
    origin = url_key(run.get("url"))
    selected = url_key(target.get("url"))
    if (not origin or not selected or selected[:3] != origin[:3] or selected[3:] != ("/", "", "")
            or (selected[0] != "https" and selected[1] not in LOOPBACK)
            or target.get("layer") != "api" or target.get("status") != "confirmed"
            or not all(text(target.get(key)) for key in ("entry", "role", "tenant"))):
        raise ValueError("API 入口须登记已确认的同源 HTTPS 或本机 HTTP、身份和租户")


def validate_request(step, *, writes=False):
    method, path = step.get("method"), step.get("path")
    if (method not in (METHODS if writes else READ_METHODS)
            or not isinstance(path, str) or not PATH.fullmatch(path)
            or "//" in path or set(path.split("/")) & {".", ".."}):
        raise ValueError("HTTP 方法或路径超出本执行器范围")
    if type(step.get("timeout", 5)) not in (int, float) or not 0.05 <= step.get("timeout", 5) <= 15:
        raise ValueError("请求超时须在 0.05 到 15 秒之间")
    for key in ("query", "headers", "extract", "auth"):
        if not isinstance(step.get(key, {}), dict):
            raise ValueError("请求参数必须为对象")
    reserved = {"host", "connection", "proxy-authorization", "proxy-connection",
                "transfer-encoding", "content-length", "upgrade", "forwarded",
                "x-http-method", "x-http-method-override", "x-method-override",
                "x-original-url", "x-rewrite-url"}
    headers = {**step.get("headers", {}), **step.get("auth", {})}
    if (len(set(name.lower() for name in headers)) != len(headers)
            or step.get("headers", {}).keys() & step.get("auth", {}).keys()
            or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name)
                   or name.lower() in reserved or name.lower().startswith("x-forwarded-") for name in headers)
            or any(SECRET_KEY.search(name) or not isinstance(value, str) or not value.isascii()
                   or any(ord(char) < 32 or ord(char) == 127 for char in value)
                   for name, value in step.get("headers", {}).items())
            or any(not isinstance(alias, str) or not ENV.fullmatch(alias) for alias in step.get("auth", {}).values())):
        raise ValueError("认证须使用环境变量引用，禁止覆盖连接与路由头")
    if (any(SECRET_KEY.search(key) or type(value) not in (str, int, bool)
            for key, value in step.get("query", {}).items())
            or any(not NAME.fullmatch(key) or SECRET_KEY.search(key) or not field_path(value)
                   for key, value in step.get("extract", {}).items())):
        raise ValueError("Query 或变量提取包含无效或敏感字段")
    bodies = {"json", "form"} & step.keys()
    if (len(bodies) > 1 or (bodies and method in READ_METHODS)
            or ("form" in step and (not isinstance(step["form"], dict)
                                    or any(type(value) not in (str, int, bool) for value in step["form"].values())))
            or any(redact_data(step[key]) != step[key] for key in bodies)):
        raise ValueError("请求体须为已脱敏 JSON 或表单，且只用于允许写入的方法")


def validate_case(run, case, target):
    validate_target(run, target)
    steps, checks = case.get("http"), case.get("checks")
    fixture = case.get("local_fixture")
    if (case.get("operation") != ("create" if fixture else "read")
            or any(key in case for key in ("fixture", "actor", "effect", "test_kind"))
            or not isinstance(steps, list) or not 1 <= len(steps) <= 12
            or not isinstance(checks, dict) or checks.keys() != case["expected"].keys()):
        raise ValueError("API 用例须登记 1–12 个 HTTP 步骤及独立断言")
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("无效 HTTP 步骤")
        validate_request(step, writes=bool(fixture))
    if fixture:
        writes = [step for step in steps if step["method"] not in READ_METHODS]
        if (urlsplit(target["url"]).hostname not in LOOPBACK or not isinstance(fixture, dict)
                or not text(fixture.get("path")) or not PATH.fullmatch(fixture["path"])
                or fixture["path"].split("/")[-1] != "{fixture}" or fixture["path"].count("{") != 1
                or len(writes) != 1 or writes[0]["method"] not in ("POST", "PUT")
                or writes[0]["path"] != fixture["path"] or "extract" in writes[0]
                or writes[0].get("auth", {}) != fixture.get("auth", {})
                or writes[0].get("query")
                or not any(check.get("read") == "sqlite_count" for check in checks.values() if isinstance(check, dict))):
            raise ValueError("本机写入须为单个隔离夹具创建步骤，绑定独立 SQLite 终态及回收")
        validate_request({"method": "GET", "path": fixture["path"], "auth": fixture.get("auth", {})})
    for step in steps:
        if ("parallel" in step and (not fixture or step["method"] in READ_METHODS
                or type(step["parallel"]) is not int or not 2 <= step["parallel"] <= 8)):
            raise ValueError("并发仅用于本机测试夹具，单批 2–8 次，不重试")
    for check in checks.values():
        if isinstance(check, dict) and check.get("read") == "sqlite_count":
            if (urlsplit(target["url"]).hostname not in LOOPBACK
                    or not isinstance(check.get("file"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+\.sqlite", check["file"])
                    or not all(isinstance(check.get(key), str)
                               and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", check[key])
                               for key in ("table", "key"))
                    or type(check.get("value")) not in (str, int)
                    or (fixture and check.get("value") != "{fixture}")
                    or type(check.get("within", 0)) is not int or not 0 <= check.get("within", 0) <= 10):
                raise ValueError("SQLite 核对须登记本机测试库、表、精确夹具键和有界期限")
            continue
        if not isinstance(check, dict) or type(check.get("step")) is not int or not 0 <= check["step"] < len(steps):
            raise ValueError("断言须关联实际 HTTP 步骤")
        read = check.get("read")
        schema = check.get("types")
        if not (read in ("status", "statuses") or (read in ("field", "exists") and field_path(check.get("path")))
                or (read == "schema" and isinstance(schema, dict) and schema
                    and all(field_path(path) and kind in TYPES.values() for path, kind in schema.items()))):
            raise ValueError("HTTP 断言须使用 status、field、exists 或必需字段类型契约")
        if (read == "statuses") != ("parallel" in steps[check["step"]]):
            raise ValueError("并发步骤仅采集各请求状态；独立终态用另一步骤判断")


def resolve(value, variables, encode=False):
    if isinstance(value, dict):
        return {key: resolve(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    match = re.fullmatch(r"\{([A-Za-z][\w-]*)\}", value)
    if match and not encode:
        return variables[match[1]]
    def substitute(match):
        item = str(variables[match[1]])
        if encode and (item in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9_.~-]{1,128}", item)):
            raise Blocked("提取值无法安全用作路径段")
        return quote(item, safe="") if encode else item
    return re.sub(r"\{([A-Za-z][\w-]*)\}", substitute, value)


def _scrub(value, secrets):
    if isinstance(value, dict):
        if any(any(secret in key for secret in secrets) for key in value):
            return "[REDACTED]"
        return {key: _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    if isinstance(value, str) and any(secret in value for secret in secrets):
        return "[REDACTED]"
    return value


def _invalid_json(_value):
    raise ValueError("非法 JSON 数值")


def _unique_keys(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("重复 JSON 字段")
    return value


def _abort(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def request(url, step, variables, credentials):
    """每步使用独立连接并限制时间、大小；重定向只作为响应返回。"""
    origin = urlsplit(url)
    headers = dict(step.get("headers", {}))
    headers.update({name: credentials[alias] for name, alias in step.get("auth", {}).items()})
    try:
        path = resolve(step["path"], variables, encode=True)
        query = urlencode(resolve(step.get("query", {}), variables))
        body = None
        if "json" in step:
            headers.setdefault("Content-Type", "application/json")
            body = json.dumps(resolve(step["json"], variables), allow_nan=False).encode("utf-8")
        if "form" in step:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            body = urlencode(resolve(step["form"], variables)).encode("ascii")
    except KeyError:
        raise Blocked("前置 HTTP 步骤未取得所需变量") from None
    timeout = step.get("timeout", 5)
    start = time.monotonic()
    connect = http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
    connection = connect(origin.hostname, origin.port, timeout=timeout)
    timer = None
    try:
        connection.connect()
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError
        timer = threading.Timer(remaining, _abort, (connection.sock,))
        timer.daemon = True
        timer.start()
        connection.request(step["method"], path + ("?" + query if query else ""), body=body, headers=headers)
        with connection.getresponse() as response:
            content = bytearray()
            while len(content) <= 1024 * 1024:
                if time.monotonic() - start > timeout:
                    raise TimeoutError
                chunk = response.read1(65536)
                if not chunk:
                    if time.monotonic() - start >= timeout:
                        raise TimeoutError
                    if response.length not in (None, 0):
                        raise http.client.IncompleteRead(b"")
                    try:
                        parsed = json.loads(content, parse_constant=_invalid_json,
                                            object_pairs_hook=_unique_keys) if content else MISSING
                    except (ValueError, RecursionError):
                        parsed = MISSING
                    return response.status, parsed, round((time.monotonic() - start) * 1000)
                content.extend(chunk)
            raise Blocked("HTTP 响应超过 1 MiB 上限")
    except TimeoutError:
        raise Blocked("HTTP 请求超时") from None
    except (OSError, http.client.HTTPException):
        raise Blocked("HTTP 请求超时" if time.monotonic() - start >= timeout else "HTTP 连接或协议错误") from None
    finally:
        if timer:
            timer.cancel()
        connection.close()


def project_response(check, status, body, secrets):
    read = check["read"]
    if read == "status":
        return status
    if read == "schema":
        return {path: TYPES.get(type(pointer(body, path)), "missing") for path in check["types"]}
    if body is MISSING:
        raise Blocked("HTTP 响应无有效 JSON，无法证明字段存在或缺失")
    value = pointer(body, check["path"])
    if read == "exists":
        return value is not MISSING
    return {"present": False} if value is MISSING else {"present": True, "value": redact_data(_scrub(value, secrets))}


def count_rows(directory, check, variables, expected):
    """按精确键通过 SQLite 只读连接查验，限定锁等待、查询时间和轮询期限。"""
    path = (directory / check["file"]).resolve()
    if path.parent != directory.resolve() or not path.is_file():
        raise Blocked("本轮 SQLite 只读数据源不可用")
    deadline = time.monotonic() + check.get("within", 0)
    try:
        key = resolve(check["value"], variables)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
            while True:
                statement_deadline = time.monotonic() + 0.25
                db.set_progress_handler(lambda: time.monotonic() > statement_deadline, 1000)
                observed = db.execute(f'SELECT COUNT(*) FROM "{check["table"]}" WHERE "{check["key"]}" = ?',
                                      (key,)).fetchone()[0]
                if observed == expected or time.monotonic() >= deadline:
                    return observed
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except (sqlite3.Error, KeyError):
        raise Blocked("本轮 SQLite 查验失败") from None


def intent_id(run, case):
    return digest([run.get("created_at"), run["project"], run["run_id"], run["version"], case["id"]])[:24]


def _fixture_request(target, spec, method, variables, credentials):
    step = {"method": method, "path": spec["path"], "auth": spec.get("auth", {})}
    return request(target["url"], step, variables, credentials)[0]


def _register_intent(directory, run, case, capture, target):
    key = intent_id(run, case)
    journal = directory / ".api-intents"
    journal.mkdir(exist_ok=True)
    intent = journal / (key + ".json")
    payload = {"run": run["run_id"], "case": case["id"], "key": key,
               "plan": digest([run.get("created_at"), target, case]),
               "time": datetime.now(timezone.utc).isoformat(),
               "capture": capture.relative_to(directory.resolve()).as_posix()}
    try:
        with intent.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream)
    except FileExistsError:
        raise Blocked("此运行已登记夹具写入，先核对既有回收记录；复测使用新运行") from None


def _cleanup(directory, target, fixture, variables, credentials, data_checks):
    _fixture_request(target, fixture, "DELETE", variables, credentials)
    return {"status": _fixture_request(target, fixture, "GET", variables, credentials),
            "rows": {key: count_rows(directory, rule, variables, 0) for key, rule in data_checks.items()}}


def _read_intent(directory, run, case, target):
    key = intent_id(run, case)
    payload = json.loads((directory / ".api-intents" / (key + ".json")).read_text(encoding="utf-8"))
    if (not case.get("local_fixture") or payload.get("key") != key or payload.get("case") != case["id"]
            or payload.get("run") != run["run_id"] or not text(payload.get("time"))
            or payload.get("plan") != digest([run.get("created_at"), target, case])):
        raise ValueError("写入意图与本轮、用例或冻结计划不符")
    return payload


def check_cleanup(directory, run, cases, pages):
    """回收证明单独约束发布及用例，避免中断后覆盖状态掩盖残留。"""
    reasons, entries = [], []
    for intent in sorted((directory / ".api-intents").glob("*.json")):
        try:
            payload = json.loads(intent.read_text(encoding="utf-8"))
            case = cases[payload["case"]]
            target = pages[case["page"]]
            payload = _read_intent(directory, run, case, target)
            checks = {key for key, rule in case["checks"].items() if rule["read"] == "sqlite_count"}
            if intent.stem != payload["key"]:
                raise ValueError("写入台账路径不符")
            paths = [payload["capture"], f".api-recovery/{payload['key']}.json"]
            for receipt in paths:
                try:
                    _, capture = registered_capture(directory, receipt)
                except (OSError, ValueError):
                    continue
                if (capture.get("plan") == payload["plan"] and capture.get("case") == case["id"]
                        and capture.get("source") == "http.client" and capture.get("run_id") == run["run_id"]
                        and text(capture.get("time")) and capture["time"] >= payload["time"]
                        and capture.get("cleanup") == {"status": 404, "rows": dict.fromkeys(checks, 0)}):
                    break
            else:
                raise ValueError("回收证明未匹配")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            reasons.append(f"API 测试夹具未核验回收：{intent.stem}")
        else:
            entries.append(f"{case['id']} · API 夹具已回收 · {receipt}")
    return reasons, entries


def _credentials(steps):
    credentials = {}
    for step in steps:
        for alias in step.get("auth", {}).values():
            value = os.environ.get(alias)
            if not value or any(ord(char) < 32 or ord(char) > 126 for char in value):
                raise Blocked("缺少有效的测试身份环境变量")
            credentials[alias] = value
    return credentials


def execute(directory, run, case, target, output, allow_origin, allow_write=False, recovery=None):
    """校验授权和输入，再执行；捕获失败也写本轮证据。"""
    validate_case(run, case, target)
    allow = url_key(allow_origin)
    if not allow or allow != url_key(target["url"]):
        raise ValueError("请传入明确授权、与冻结 API 入口完全匹配的 --allow-origin")
    fixture = case.get("local_fixture")
    if (fixture or recovery is not None) and not allow_write:
        raise ValueError("本机写入需要 --allow-local-write，且仅可使用隔离测试服务")
    data_checks = {key: rule for key, rule in case["checks"].items() if rule["read"] == "sqlite_count"}
    if recovery is not None:
        payload = _read_intent(directory, run, case, target)
        if not text(recovery) or redact_data(recovery) != recovery or len(recovery) > 300:
            raise ValueError("回收前须提供非敏感的业务任务已终结或隔离服务已停止在途写入的确认依据")
        path = directory.resolve() / ".api-recovery" / (payload["key"] + ".json")
        _issues, entries = check_cleanup(directory, run, {case["id"]: case}, {target["id"]: target})
        if path.exists() or entries:
            raise ValueError("已有回收证据，请核对原记录")
        cleanup = _cleanup(directory, target, fixture, {"fixture": "dydqa-" + payload["key"]},
                           _credentials([fixture]), data_checks)
        if cleanup != {"status": 404, "rows": dict.fromkeys(data_checks, 0)}:
            raise Blocked("回收后仍有数据残留")
        write_capture(directory, path, {"source": "http.client", "run_id": run["run_id"], "case": case["id"],
                                        "plan": payload["plan"], "time": datetime.now(timezone.utc).isoformat(),
                                        "recovery_basis": recovery, "cleanup": cleanup})
        return path, None
    if fixture and (directory / ".api-intents" / (intent_id(run, case) + ".json")).exists():
        raise ValueError("本轮已有写入意图，禁止重放；检查回收后另建复测")
    path = (directory / output).resolve()
    if directory.resolve() not in path.parents or path.suffix != ".json" or path.exists():
        raise ValueError("HTTP 采集需要本轮目录内尚未使用的 JSON 路径")
    started = datetime.now(timezone.utc).isoformat()
    steps, checks, measurements, receipts = case["http"], case["checks"], {}, []
    variables = {}
    error = None
    cleanup = None
    intent_registered = False
    write_uncertain = False
    try:
        credentials = _credentials(steps + ([fixture] if fixture else []))
        secrets = set(credentials.values()) | {value[7:] for value in credentials.values() if value.lower().startswith("bearer ")}
        secrets.discard("")
        if fixture:
            for rule in data_checks.values():
                db_path = (directory / rule["file"]).resolve()
                if db_path.parent != directory.resolve() or not db_path.is_file():
                    raise Blocked("本轮 SQLite 只读数据源不可用")
            variables["fixture"] = "dydqa-" + intent_id(run, case)
            if (_fixture_request(target, fixture, "GET", variables, credentials) != 404
                    or any(count_rows(directory, {**rule, "within": 0}, variables, 0) != 0 for rule in data_checks.values())):
                raise Blocked("夹具初始状态不为空，禁止写入或删除")
            _register_intent(directory, run, case, path, target)
            intent_registered = True
        for index, step in enumerate(steps):
            parallel = step.get("parallel")
            write_uncertain = step["method"] not in READ_METHODS
            if parallel:
                barrier = threading.Barrier(parallel, timeout=3)
                def send():
                    barrier.wait()
                    return request(target["url"], step, variables, credentials)
                with ThreadPoolExecutor(max_workers=parallel) as executor:
                    results = [executor.submit(send) for _index in range(parallel)]
                    replies = [job.result() for job in results]
                status, body, elapsed = sorted(reply[0] for reply in replies), MISSING, max(reply[2] for reply in replies)
            else:
                status, body, elapsed = request(target["url"], step, variables, credentials)
            if write_uncertain and any(code == 202 or 300 <= code < 400 or code >= 500
                                       for code in (status if parallel else [status])):
                raise Blocked("写入回执未能确认同步终态，需回查后回收")
            write_uncertain = False
            receipts.append({"step": index, "status": status, "elapsed_ms": elapsed})
            for key, check in checks.items():
                if check.get("step") == index:
                    measurements[key] = status if parallel else project_response(check, status, body, secrets)
                    if redacted_measurement(measurements[key]):
                        raise Blocked("采集字段含敏感值；改用存在性或授权的非敏感投影")
            for key, field in step.get("extract", {}).items():
                value = pointer(body, field)
                if (key == "fixture" or status >= 400 or type(value) not in (str, int, bool)
                        or redact_data(_scrub(value, secrets)) != value):
                    raise Blocked("HTTP 前置响应缺少合格的非敏感变量")
                variables[key] = value
        for key, rule in data_checks.items():
            measurements[key] = count_rows(directory, rule, variables, case["expected"][key])
    except (Blocked, threading.BrokenBarrierError) as exc:
        error = str(exc) or "并发启动失败"
    except KeyboardInterrupt:
        error = "HTTP 执行中断"
    finally:
        if intent_registered and not write_uncertain:
            try:
                cleanup = _cleanup(directory, target, fixture, variables, credentials, data_checks)
                if cleanup != {"status": 404, "rows": dict.fromkeys(data_checks, 0)}:
                    error = error or "本机 API 夹具回收未核验"
            except Blocked:
                error = error or "本机 API 夹具回收失败"
    data = {"schema": 1, "source": "http.client", "run_id": run["run_id"], "version": run["version"],
            "plan": digest([run.get("created_at"), target, case]), "case": case["id"], "url": target["url"],
            "start": started, "time": datetime.now(timezone.utc).isoformat(), "error": error,
            "tool": "Python " + sys.version.split()[0], "steps": receipts, "observed": measurements,
            "cleanup": cleanup}
    write_capture(directory, path, data)
    return path, error


def result(directory, run, case, target, path):
    """使用被冻结的规则复算证据；同运行、目标或计划变更都会拒绝。"""
    file, data = registered_capture(directory, path)
    if (data.get("schema") != 1 or data.get("source") != "http.client" or data.get("error") is not None
            or (data.get("run_id"), data.get("version"), data.get("case"), data.get("url"))
            != (run["run_id"], run["version"], case["id"], target["url"])
            or data.get("plan") != digest([run.get("created_at"), target, case])
            or not all(text(data.get(key)) for key in ("start", "time", "tool"))
            or not run.get("created_at") <= data["start"] <= data["time"] <= datetime.now(timezone.utc).isoformat()):
        raise ValueError("HTTP 证据的执行状态、运行、采样时间或计划不匹配")
    observed, receipts = data.get("observed"), data.get("steps")
    if (not isinstance(observed, dict) or observed.keys() != case["checks"].keys()
            or not isinstance(receipts, list) or len(receipts) != len(case["http"])):
        raise ValueError("HTTP 采集不完整")
    for index, receipt in enumerate(receipts):
        if (not isinstance(receipt, dict) or receipt.get("step") != index
                or type(receipt.get("elapsed_ms")) is not int or receipt["elapsed_ms"] < 0):
            raise ValueError("HTTP 步骤回执不完整")
        status, parallel = receipt.get("status"), case["http"][index].get("parallel")
        codes = status if parallel else [status]
        if (not isinstance(codes, list) or len(codes) != (parallel or 1)
                or not all(type(code) is int and 100 <= code <= 599 for code in codes)):
            raise ValueError("HTTP 步骤回执状态无效")
    relative = file.relative_to(directory.resolve()).as_posix()
    if case.get("local_fixture"):
        key = intent_id(run, case)
        journal = directory / ".api-intents" / (key + ".json")
        if not journal.is_file() or json.loads(journal.read_text(encoding="utf-8")).get("capture") != relative:
            raise ValueError("本机写入证据缺少对应写入登记")
        issues, _entries = check_cleanup(directory, run, {case["id"]: case}, {target["id"]: target})
        if f"API 测试夹具未核验回收：{key}" in issues:
            raise ValueError("本机写入缺少独立回收证据")
    assertions, passed = {}, True
    for key, rule in case["checks"].items():
        value = observed[key]
        if redacted_measurement(value):
            raise ValueError("HTTP 采集含敏感值，请改用存在性或非敏感字段")
        if rule["read"] == "schema":
            if not isinstance(value, dict) or value.keys() != rule["types"].keys():
                raise ValueError("HTTP 字段类型采集不完整")
            value = all(value.get(field) == kind or (kind == "number" and value.get(field) == "integer")
                        for field, kind in rule["types"].items())
        elif rule["read"] == "field":
            if not isinstance(value, dict) or type(value.get("present")) is not bool:
                raise ValueError("HTTP 字段采集无效")
            if value.get("present") is not True:
                passed = False
            value = value.get("value")
        elif rule["read"] in ("status", "statuses") and value != receipts[rule["step"]]["status"]:
            raise ValueError("HTTP 状态断言与原始步骤回执不一致")
        assertions[key] = {"actual": value, "evidence": relative,
                           "source": "sqlite" if rule["read"] == "sqlite_count" else "http"}
        passed = digest(case["expected"][key]) == digest(value) and passed
    return "passed" if passed else "failed", {"capture": digest(data)}, {
        "capture": relative, "url": target["url"], "tenant": target["tenant"], "role": target["role"],
        "layer": "api", "assertions": assertions, "sampled_at": data["time"], "session": data["capture_id"],
        "tool": data["tool"], "started_at": data["start"],
    }
