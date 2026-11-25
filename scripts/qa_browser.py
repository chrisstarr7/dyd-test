"""从本轮 agent-browser 会话采集有限字段，计算逐项断言和夹具状态。"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from qa_contract import digest, evidence_file, text, url_key
from qa_oracle import compute, probes, valid_check


SECRET_KEY = re.compile(r"password|passwd|pwd|token|cookie|secret|authorization|api.?key|access.?key|密码|令牌", re.I)
PRIVATE_TEXT = re.compile(r"(?<![A-Za-z0-9])1[3-9]\d{9}(?![A-Za-z0-9])|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?i:bearer\s+[\w.-]+)")
SESSION = re.compile(r"[\w-]{1,100}\Z")
READS = {"count", "text", "value", "visible", "texts", "focused", "label"}
NAVIGATION_METRICS = {"domContentLoadedEventEnd", "loadEventEnd"}
LAYERS = {"online_ui", "offline_ui"}
PHASES = {"page", "assert", "before", "after", "cleanup"}

# 固定脚本只读取当前页面。页面配置可以指定选择器和读取类型。
PROBE = r"""
({page, checks, fixture, resultReady, allowMissingRole}) => {
  if (location.href !== new URL(page.url).href) throw Error('当前路由与计划不一致');
  const nodes = (selector) => Array.from(document.querySelectorAll(selector));
  const visible = (el) => el.getClientRects().length && getComputedStyle(el).visibility === 'visible';
  const clean = (el) => (el.innerText || '').trim();
  const read = (scope, probe) => {
    if (probe.read === 'navigation_ms') {
      const navigation = performance.getEntriesByType('navigation')[0];
      if (!navigation || !navigation[probe.metric]) throw Error('页面计时尚未完成');
      return Math.round(navigation[probe.metric] - navigation.startTime);
    }
    const matches = scope ? Array.from(scope.querySelectorAll(probe.selector)) : nodes(probe.selector);
    const shown = matches.filter(visible);
    if (probe.read === 'count') return shown.length;
    if (probe.read === 'visible') {
      if (shown.length > 1) throw Error('标识对应多个可见控件');
      return shown.length === 1;
    }
    if (probe.read === 'texts') return shown.map(clean);
    if (shown.length !== 1) throw Error('字段未就绪或目标不唯一');
    const el = shown[0];
    if (probe.read === 'focused') return el === document.activeElement;
    if (probe.read === 'label') {
      const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
      if (labelledBy.length) return labelledBy.map(id => clean(document.getElementById(id) || {})).join(' ').trim();
      return el.getAttribute('aria-label')?.trim() || Array.from(el.labels || []).map(clean).join(' ').trim() || clean(el);
    }
    if (el instanceof HTMLInputElement && (el.type === 'password' || el.type === 'hidden'))
      throw Error('凭据控件不能采集');
    if (probe.read === 'value') {
      if (typeof el.value !== 'string') throw Error('目标不是有值的表单控件');
      return el.value;
    }
    return clean(el);
  };
  const readIdentity = probe => read(null, {selector:probe.selector, read:'text'});
  const roleVisible = nodes(page.identity.role.selector).some(visible);
  const role = !roleVisible && allowMissingRole ? null : readIdentity(page.identity.role);
  const tenant = readIdentity(page.identity.tenant);
  const principal = page.identity.principal ? readIdentity(page.identity.principal) : null;
  const anchors = page.anchors.map(selector => nodes(selector).filter(visible).length);
  const ready = nodes(page.ready).filter(visible).length;
  if ((role !== page.role && role !== null) || tenant !== page.tenant || anchors.some(n => n !== 1) || ready !== 1
      || (page.identity.principal && !principal)
      || (page.busy && nodes(page.busy).some(visible))
      || (resultReady && nodes(resultReady).filter(visible).length !== 1))
    throw Error('页面身份、主要控件或数据尚未就绪');
  const hits = fixture ? nodes(fixture.selector).filter(visible) : [];
  if (hits.length > 1) throw Error('测试夹具目标不唯一');
  const fields = hits.length ? Object.fromEntries(Object.entries(fixture.fields || {})
      .map(([key, probe]) => [key, read(hits[0], probe)])) : {};
  const related = fixture ? Object.fromEntries(Object.entries(fixture.related || {})
      .map(([key, selector]) => [key, nodes(selector).filter(visible).length])) : {};
  return {url:location.href, role, tenant, principal, online:navigator.onLine, anchors, ready,
    document_start:performance.timeOrigin,
    observed:Object.fromEntries(Object.entries(checks).map(([key, probe]) => [key, read(null, probe)])),
    count:fixture ? hits.length : null, fields, related};
}
"""


def redact_data(value):
    """脱敏在持久化之前。采集值含隐私时使用脱敏标识或非敏感断言。"""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SECRET_KEY.search(key) else redact_data(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return PRIVATE_TEXT.sub("[REDACTED]", value)
    if type(value) is int and PRIVATE_TEXT.fullmatch(str(value)):
        return "[REDACTED]"
    return value


def browser_probe(value):
    if not isinstance(value, dict):
        return False
    if value.get("read") == "navigation_ms":
        return (value.get("metric") in NAVIGATION_METRICS and type(value.get("max_ms")) is int
                and 0 < value["max_ms"] <= 300_000)
    return text(value.get("selector")) and value.get("read") in READS


def redacted_measurement(value):
    if isinstance(value, str):
        return "[REDACTED]" in value
    if isinstance(value, dict):
        return any(map(redacted_measurement, value.values()))
    if isinstance(value, list):
        return any(map(redacted_measurement, value))
    return False


def browser_page(page):
    anchors = page.get("anchors")
    identity = page.get("identity")
    if (page.get("status") != "confirmed" or not text(page.get("entry"))
            or not url_key(page.get("url")) or not text(page.get("role")) or not text(page.get("tenant"))
            or not isinstance(anchors, list) or not all(map(text, anchors))
            or len(set(anchors)) < 2 or len(anchors) != len(set(anchors))
            or not text(page.get("ready")) or ("busy" in page and not text(page["busy"]))
            or not isinstance(identity, dict) or any(not isinstance(identity.get(key), dict)
            or not text(identity[key].get("selector")) for key in ("role", "tenant"))):
        raise ValueError("页面须登记真实路由、角色/租户定位、两个独立锚点及就绪控件")
    if "principal" in identity and (not isinstance(identity["principal"], dict)
            or not text(identity["principal"].get("selector"))
            or identity["principal"]["selector"] in (identity["role"]["selector"], identity["tenant"]["selector"])):
        raise ValueError("登录主体须有独立于角色和租户的页面标识")


def browser_fixture(fixture):
    probe = fixture.get("probe")
    if (not isinstance(probe, dict) or not text(probe.get("selector"))
            or not isinstance(probe.get("fields"), dict)
            or any(not text(key) or SECRET_KEY.search(key) or not browser_probe(value)
                   or not text(value.get("selector"))
                   for key, value in probe["fields"].items())
            or not isinstance(probe.get("related", {}), dict)
            or any(not text(key) or SECRET_KEY.search(key) or not text(value)
                   for key, value in probe.get("related", {}).items())):
        raise ValueError("夹具须登记唯一定位及允许采集的非敏感字段")
    return probe


def read_capture(directory, path):
    file = evidence_file(directory, path)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError("证据须为浏览器采集生成的结构化文件") from error
    if not isinstance(data, dict):
        raise ValueError("证据须为浏览器采集生成的对象")
    return file, data


def write_capture(directory, path, data):
    """登记采集文件及其内容摘要，写到一半中断的证据不会被接纳。"""
    data = dict(data, capture_id=uuid.uuid4().hex)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
    register = directory / ".captures"
    register.mkdir(exist_ok=True)
    with (register / (data["capture_id"] + ".json")).open("x", encoding="utf-8") as stream:
        json.dump([path.relative_to(directory.resolve()).as_posix(), digest(data)], stream)


def verify_identity(directory, run, page, case, fixture, path, measured):
    write_phase = measured.get("phase") in {"before", "after", "cleanup"}
    write_ok = (fixture and write_phase
                and (case is None or case.get("operation") in {"create", "update", "delete"}))
    read_ok = (not fixture and case is not None and measured.get("phase") == "assert"
               and case.get("operation") == "read")
    if ((case is not None and case.get("scenario") == "role")
            or (case is not None and case.get("layer", "online_ui") != "online_ui")
            or not page["identity"].get("principal") or not text(path)
            or not (write_ok or read_ok)):
        raise ValueError("会话身份凭据仅适用于同页面、同文档的在线读断言或写入回查")
    # 只认直接采集的页面凭据，禁止证据链递归继承。
    _, initial = read_capture(directory, path)
    if initial.get("identity_proof") is not None:
        raise ValueError("角色须来自首次直接采集")
    _, initial = validate_capture(directory, run, page, path, phases={"page"})
    if (not text(initial.get("principal_digest")) or initial.get("principal_digest") != measured.get("principal_digest")
            or not isinstance(initial.get("document_start"), (int, float))
            or any(initial.get(key) != measured.get(key) for key in ("session", "document_start", "tenant"))
            or measured.get("time", "") < initial["time"]):
        raise ValueError("登录主体、租户、会话或页面文档已变化，需重新核验角色")


def validate_capture(directory, run, page, path, *, phases, case=None, fixture=None):
    browser_page(page)
    file, data = read_capture(directory, path)
    capture_id = data.get("capture_id")
    if not text(capture_id) or not re.fullmatch(r"[a-f0-9]{32}", capture_id):
        raise ValueError("证据没有采集登记")
    try:
        registered = json.loads((directory / ".captures" / (capture_id + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("证据没有有效采集登记") from error
    if registered != [file.relative_to(directory.resolve()).as_posix(), digest(data)]:
        raise ValueError("采集证据内容或路径已变更")
    page_origin = url_key(page["url"])
    run_origin = url_key(run.get("url"))
    layer = (case or fixture or {}).get("layer", "online_ui")
    if (data.get("source") != "agent-browser" or data.get("schema") != 1
            or data.get("run_id") != run.get("run_id") or data.get("version") != run.get("version")
            or not text(data.get("session")) or not SESSION.fullmatch(data["session"])
            or not text(data.get("time")) or data.get("phase") not in phases
            or data.get("page") != page["id"] or data.get("case") != (case or {}).get("id")
            or data.get("fixture") != (fixture or {}).get("id")
            or data.get("plan") != digest([page, case, fixture])
            or not run_origin or page_origin[:3] != run_origin[:3] or url_key(data.get("url")) != page_origin
            or data.get("role") != page["role"] or data.get("tenant") != page["tenant"]
            or data.get("ready") != 1 or data.get("anchors") != [1] * len(page["anchors"])
            or (data.get("online") is not (layer == "online_ui"))):
        raise ValueError("采集记录的运行、会话、操作阶段、页面身份或执行层不匹配")
    if fixture and (type(data.get("count")) is not int or data["count"] not in (0, 1)
                    or (fixture.get("tenant") != page["tenant"])
                    or not isinstance(data.get("fields"), dict)
                    or set(data["fields"]) != (set(browser_fixture(fixture)["fields"]) if data["count"] else set())
                    or not isinstance(data.get("related"), dict)
                    or data["related"].keys() != browser_fixture(fixture).get("related", {}).keys()
                    or any(type(n) is not int or n < 0 for n in data["related"].values())):
        raise ValueError("夹具采集结果缺失、重复或租户不匹配")
    measured = {key: data.get(key) for key in ("observed", "fields", "role", "tenant", "url", "related")}
    if redact_data(measured) != measured:
        raise ValueError("采集记录含有未脱敏值")
    if "identity_proof" in data:
        if data["phase"] not in {"assert", "before", "after", "cleanup"}:
            raise ValueError("只有读断言或写入回查可引用会话身份凭据")
        verify_identity(directory, run, page, case, fixture, data["identity_proof"], data)
    return file, data


def result_from_capture(directory, run, case, page, criteria, path, fixture=None, before_path=None):
    if not page:
        raise ValueError("用例的目标页面尚未登记")
    file, observed = validate_capture(directory, run, page, path, case=case, fixture=fixture,
                                      phases={"after"} if fixture else {"assert"})
    expected = case["expected"]
    checks = case["checks"]
    current = observed.get("observed")
    before = {}
    fingerprints = {"capture": digest(observed)}
    if probes(checks, "before"):
        initial_file, initial = validate_capture(directory, run, page, before_path, case=case, fixture=fixture,
                                                 phases={"before"})
        if initial["session"] != observed["session"] or initial["time"] >= observed["time"]:
            raise ValueError("基线与当前业务结果的会话或采样顺序不一致")
        before = initial.get("observed")
        fingerprints["baseline"] = digest(initial)
        before_path = initial_file.relative_to(directory.resolve()).as_posix()
    if redacted_measurement(current) or redacted_measurement(before):
        raise ValueError("采集值含已脱敏文本，改用非敏感标识或数量断言")
    actual = compute(checks, current, before)
    if actual.keys() != expected.keys():
        raise ValueError("采集值和验收点不一致")
    for key in expected:
        req_id, criterion = criteria.get(key, (None, {}))
        if (req_id != case["req"] or criterion.get("status") == "pending"
                or criterion.get("layer", "online_ui") != case.get("layer", "online_ui")):
            raise ValueError(f"{key} 的验收点或执行层未确认")
    relative = file.relative_to(directory.resolve()).as_posix()
    observation = {key: observed[key] for key in ("url", "role", "tenant")}
    observation.update(capture=relative, layer=case.get("layer", "online_ui"),
                       assertions={key: {"actual": value, "evidence": relative} for key, value in actual.items()})
    for key, rule in checks.items():
        if rule.get("read") == "navigation_ms":
            observation["assertions"][key].update(raw_ms=current[key], max_ms=rule["max_ms"])
    if fingerprints.get("baseline"):
        observation["baseline_evidence"] = before_path
    passed = all(digest(expected[key]) == digest(actual[key]) for key in expected)
    return "passed" if passed else "failed", fingerprints, observation


def collect(session, page, checks, fixture, timeout, online, ready, allow_missing_role=False):
    """短轮询等待页面及采样字段就绪，缺失的断言结果保留给脚本比对。"""
    executable = shutil.which("agent-browser")
    if not executable:
        raise ValueError("找不到 agent-browser，先初始化浏览器运行时")
    payload = json.dumps({"page": page, "checks": checks, "fixture": fixture, "resultReady": ready,
                          "allowMissingRole": allow_missing_role}, ensure_ascii=True)
    script = f"({PROBE})({payload})"
    until = time.monotonic() + timeout
    while True:
        reply = subprocess.run([executable, "--session", session, "--json", "eval", "--stdin"],
                               input=script, encoding="utf-8", stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=min(10, timeout + 1))
        try:
            result = json.loads(reply.stdout)["data"]["result"]
            if (isinstance(result, dict) and url_key(result.get("url")) == url_key(page["url"])
                    and result.get("online") is online):
                return result
        except (KeyError, TypeError, ValueError):
            pass
        if time.monotonic() >= until:
            raise ValueError("未采集到处于正确路由、角色、租户、网络模式和就绪状态的浏览器页面")
        time.sleep(0.25)


def capture(directory, session, phase, output, *, page_id=None, case_id=None, fixture_id=None,
            identity_proof=None, timeout=6):
    """按计划采集一次证据，文件必须是当前运行中尚未写入的 JSON。"""
    def load(name):
        return json.loads((directory / name).read_text(encoding="utf-8"))

    run = load("run.json")
    if run.get("qa_contract") != 2:
        raise ValueError("浏览器采集需要新版运行")
    if not SESSION.fullmatch(session) or not 1 <= timeout <= 30 or phase not in PHASES:
        raise ValueError("会话名称、采集阶段或超时无效")
    from qa_plan import verify_frozen

    raw_cases = load("test-cases.json")
    raw_cases = raw_cases["cases"] if isinstance(raw_cases, dict) else raw_cases
    indexed_pages = {item["id"]: item for item in load("pages.json")}
    verify_frozen(directory, run, load("requirements.json"), raw_cases, indexed_pages)
    cases = {case["id"]: case for case in raw_cases}
    fixtures = {item["id"]: item for item in load("fixtures.json")["fixtures"]}
    case = cases.get(case_id)
    if (case_id and not case) or (fixture_id and fixture_id not in fixtures):
        raise ValueError("目标用例或夹具未登记")
    # 只读用例可以关联备数，断言仍按用例页面独立采集。
    mutation_fixture = (case or {}).get("fixture") if phase in {"before", "after"} else None
    fixture = fixtures.get(fixture_id or mutation_fixture)
    if (phase == "page" and (case or fixture or not page_id)
            or phase == "assert" and (not case or case.get("operation") != "read" or fixture)
            or phase in {"before", "after"} and (not fixture or (case and case.get("fixture") != fixture["id"]))
            or phase == "cleanup" and (not fixture or case)):
        raise ValueError("用例、夹具和采集阶段不匹配")
    page_ref = (case or {}).get("page") or (fixture or {}).get("page") or page_id
    if (page_id and page_id != page_ref) or (fixture and fixture.get("page") != page_ref):
        raise ValueError("夹具和用例须在同一已登记页面采集")
    page = indexed_pages.get(page_ref)
    if not page:
        raise ValueError("采集页面未登记")
    browser_page(page)
    origin = url_key(run.get("url"))
    if not origin or url_key(page["url"])[:3] != origin[:3]:
        raise ValueError("采集页面不属于运行授权来源")
    checks = (case or {}).get("checks", {})
    if not isinstance(checks, dict) or any(not valid_check(value, browser_probe, baseline=bool(fixture))
                                           for value in checks.values()):
        raise ValueError("断言采集规则无效")
    checks = probes(checks, "before" if phase == "before" else "current") if phase in {"assert", "before", "after"} else {}
    online = (case or fixture or {}).get("layer", "online_ui") == "online_ui"
    ready = (case or {}).get("result_ready") if phase in {"assert", "after"} else None
    proof_allowed = ((case is None or case.get("scenario") != "role") and online
                     and page["identity"].get("principal")
                     and ((fixture and phase in {"before", "after", "cleanup"}
                           and (case is None or case.get("operation") in {"create", "update", "delete"}))
                          or (not fixture and case is not None and phase == "assert"
                              and case.get("operation") == "read")))
    if identity_proof and not proof_allowed:
        raise ValueError("会话身份凭据仅可用于有登录主体标识的在线读断言或写入回查")
    measured = collect(session, page, checks, browser_fixture(fixture) if fixture else None,
                       timeout, online, ready, allow_missing_role=bool(identity_proof))
    # 字段和身份从当前浏览器采集；含敏感值的路由不落盘。
    if redact_data(measured["url"]) != measured["url"] or re.search(r"(?:[?&]|%26)(?:token|password|code)=", measured["url"], re.I):
        raise ValueError("采集路由可能包含凭据，需使用无凭据的测试入口")
    principal = measured.pop("principal", None)
    measured = redact_data(measured)
    if text(principal):
        measured["principal_digest"] = digest([run["run_id"], session, measured.get("document_start"), principal])
    measured.update(schema=1, source="agent-browser", run_id=run["run_id"], version=run["version"],
                    session=session, time=datetime.now(timezone.utc).isoformat(), phase=phase, page=page["id"],
                    case=case_id, fixture=(fixture or {}).get("id"), plan=digest([page, case, fixture]))
    if identity_proof:
        verify_identity(directory, run, page, case, fixture, identity_proof, measured)
        if measured.get("role") is None:
            measured.update(role=page["role"], identity_proof=identity_proof)
    if measured.get("role") != page["role"]:
        raise ValueError("当前页面无法确认角色")
    path = (directory / output).resolve()
    if directory.resolve() not in path.parents or path.suffix != ".json":
        raise ValueError("采集文件必须是任务目录内的 JSON")
    write_capture(directory, path, measured)
    return path


def main():
    parser = argparse.ArgumentParser(description="从已打开的 agent-browser 会话采集当前实际状态")
    for field in ("dir", "session", "output", "phase"):
        parser.add_argument(f"--{field}", required=True)
    for field in ("page", "case", "fixture"):
        parser.add_argument(f"--{field}")
    parser.add_argument("--timeout", type=float, default=6)
    parser.add_argument("--identity-proof", help="同会话、同文档内的直接角色采集")
    args = parser.parse_args()
    try:
        print(capture(Path(args.dir), args.session, args.phase, args.output,
                      page_id=args.page, case_id=args.case, fixture_id=args.fixture,
                      identity_proof=args.identity_proof, timeout=args.timeout))
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"采集未完成：{error}")


if __name__ == "__main__":
    main()
