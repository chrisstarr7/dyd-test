"""校验测试数据的授权范围、写入回查和回收证据。"""
import hashlib
import json
from datetime import datetime

from qa_browser import exact_fixture, redacted_measurement, validate_capture
from qa_contract import digest, evidence_file, index_objects, text, url_key
from qa_pool import verify_lease


WRITE_ACTIONS = ("create", "update", "delete")
CLEANUP_ACTIONS = {"generated": "delete", "pool": "release", "reused": "restore"}


def operation(run, case):
    action = case.get("operation")
    if action is None and not run.get("fixture_contract"):
        return "read"
    if action not in ("read", *WRITE_ACTIONS):
        raise ValueError("用例须声明 operation 为 read、create、update 或 delete")
    return action


def proof(directory, name):
    file = evidence_file(directory, name)
    return {"path": file.relative_to(directory.resolve()).as_posix(),
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}


def load_context(directory):
    def load(name, default):
        path = directory / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    manifest = load("fixtures.json", {})
    ledger = load("fixture-actions.json", {"attempts": {}, "closed": {}})
    if (not isinstance(manifest, dict) or not isinstance(ledger, dict)
            or any(not isinstance(ledger.get(key), dict) for key in ("attempts", "closed"))
            or any(not isinstance(value, dict) for group in ledger.values() if isinstance(group, dict)
                   for value in group.values())):
        raise ValueError("测试数据台账格式无效")
    errors = []
    fixtures = index_objects(manifest.get("fixtures", []), "测试数据", errors)
    if errors:
        raise ValueError("；".join(errors))
    return manifest.get("boundary", {}), fixtures, ledger


def fixture_cases(cases, fixtures):
    """备数和补偿清理进入同一台账，独立于业务用例结果。"""
    support = {f"{kind}:{key}": {"operation": action, "fixture": key}
               for key, fixture in fixtures.items() if fixture.get("source") == "generated"
               for kind, action in (("setup", "create"), ("cleanup", "delete"))}
    if cases.keys() & support.keys():
        raise ValueError("用例编号与备数动作编号冲突")
    return {**cases, **support}


def fixture_binding(directory, run, context, fixture_id, tenant, action):
    """绑定运行、环境、租户、来源和证据，以便在恢复与收尾时重新核对。"""
    boundary, fixtures, _ = context
    fixture = fixtures.get(fixture_id) if text(fixture_id) else None
    origin = url_key(run.get("url"))
    allowed_origin = url_key(boundary.get("url")) if isinstance(boundary, dict) else None
    if (not origin or not allowed_origin or origin[:3] != allowed_origin[:3]
            or boundary.get("environment") != "test" or not text(boundary.get("authorization"))
            or not text(tenant) or boundary.get("tenant") != tenant
            or boundary.get("external_effects") not in ("disabled", "test_sink")):
        raise ValueError("测试环境、租户授权或外部副作用边界未确认")
    if not fixture or fixture.get("tenant") != tenant:
        raise ValueError("测试数据未登记或不属于当前租户")
    cleanup = CLEANUP_ACTIONS.get(fixture.get("source")) if text(fixture.get("source")) else None
    run_id = run.get("run_id")
    marker = fixture.get("marker")
    if (fixture.get("ownership") != "verified" or not text(fixture.get("reference"))
            or not text(run_id) or not text(marker) or run_id not in marker
            or not cleanup or fixture.get("cleanup") != cleanup):
        raise ValueError("测试数据归属、运行标记或清理方案未确认")
    if any(key != fixture_id and other.get("tenant") == tenant
           and (other.get("marker") == marker or other.get("reference") == fixture["reference"])
           for key, other in fixtures.items()):
        raise ValueError("同一数据引用或运行标记被重复登记")
    for scope in (boundary, fixture):
        actions = scope.get("actions")
        if not isinstance(actions, list) or cleanup not in actions or (action and action not in actions):
            raise ValueError("测试写入或清理未授权")
    if run.get("qa_contract") == 2 and fixture["source"] == "pool":
        pool_receipt(directory, fixture, "reservation", action=action)
    if run.get("assurance_contract") == 1:
        related = exact_fixture(fixture).get("related", {})
        counts = fixture.get("cleanup_counts")
        if (not text(fixture.get("cleanup_scope")) or (fixture["source"] == "generated"
                and (not isinstance(counts, dict) or counts.keys() != related.keys()
                     or any(type(count) is not int or count != 0 for count in counts.values())))):
            raise ValueError("须声明关联数据清理范围和独立回查的预期状态")
    return digest([run_id, origin, boundary, fixture, proof(directory, boundary.get("evidence")),
                   proof(directory, fixture.get("evidence"))])


def check_proof(directory, stored):
    if not isinstance(stored, dict) or proof(directory, stored.get("path")) != stored:
        raise ValueError("数据证据已变更或未登记")


def pool_receipt(directory, fixture, field, *, active=False, action=None):
    if not text(fixture.get("pool_profile")):
        raise ValueError("夹具缺少受控项目池配置")
    file = evidence_file(directory, fixture.get(field))
    receipt = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(receipt, dict):
        raise ValueError("夹具池回执必须是对象")
    verify_lease(directory, fixture["pool_profile"], fixture, receipt,
                 released=field == "release_receipt", active=active, action=action)
    return receipt


def fixture_sample(directory, run, pages, fixture, saved, phase, case=None):
    check_proof(directory, saved)
    page = pages.get(fixture.get("page"))
    if not page:
        raise ValueError("夹具没有已登记的页面")
    if case and (case.get("page") != page["id"] or case.get("layer", "online_ui") != fixture.get("layer", "online_ui")):
        raise ValueError("夹具与用例页面或执行层不一致")
    _, capture = validate_capture(directory, run, page, saved["path"], phases={phase}, case=case, fixture=fixture)
    try:
        timestamp = datetime.fromisoformat(capture["time"])
        if timestamp.utcoffset() is None:
            raise ValueError("未指定时区")
    except (TypeError, ValueError) as error:
        raise ValueError("夹具采集时间无效") from error
    return capture, timestamp


def before_expected(run, case, action):
    expected = "absent" if action == "create" else "present"
    if run.get("qa_contract") == 2 and action == "create" and case.get("scenario") == "negative":
        return case.get("expected_before", expected)
    return expected


def verify_attempt(directory, run, context, case, attempt, *, settled=True, pages=None, path=None):
    action = operation(run, case)
    if (not attempt or action not in WRITE_ACTIONS or case.get("fixture") != attempt.get("fixture")
            or action != attempt.get("action")):
        raise ValueError("缺少与用例匹配的写入前登记")
    if attempt.get("before_state") != before_expected(run, case, action):
        raise ValueError("写入前基线无效")
    binding = fixture_binding(directory, run, context, attempt["fixture"], attempt.get("tenant"), attempt["action"])
    if binding != attempt.get("binding"):
        raise ValueError("测试数据边界或归属证据已变更")
    check_proof(directory, attempt.get("before"))
    if settled:
        if attempt.get("after_state") not in ("present", "absent"):
            raise ValueError("写入结果未回查，禁止重复提交")
        check_proof(directory, attempt.get("after"))
    if run.get("qa_contract") == 2:
        fixture = context[1][attempt["fixture"]]
        probe_case = case if case.get("id") else None
        start, start_at = fixture_sample(directory, run, pages, fixture, attempt["before"], "before", probe_case)
        if (start["count"] == 1) != (attempt["before_state"] == "present"):
            raise ValueError("写入前状态与浏览器采集不一致")
        effect = case.get("effect", {}) if run.get("evidence_contract") == 1 else {}
        if effect.get("mode") == "change":
            field = effect["field"]
            if field not in start["fields"] or redacted_measurement(start["fields"][field]):
                raise ValueError("修改目标字段的写入前基线无法核验")
            if digest(start["fields"][field]) == digest(effect["to"]):
                raise ValueError("修改前字段已等于目标值，请另选能验证变化的测试数据")
        elif effect.get("mode") == "unchanged":
            if start.get("action_received") is not False or (start["count"] and not start["fields"]):
                raise ValueError("无变化操作需证明旧响应不存在，并采集对象字段基线")
        if settled:
            end, end_at = fixture_sample(directory, run, pages, fixture, attempt["after"], "after", probe_case)
            if run.get("assurance_contract") == 1 and start["subject_digest"] != end["subject_digest"]:
                raise ValueError("写入前后的实际登录主体不一致")
            if (end["session"] != start["session"] or end_at <= start_at
                    or (end["count"] == 1) != (attempt["after_state"] == "present")
                    or (path and attempt["after"]["path"] != path)):
                raise ValueError("写入回查顺序、会话、业务状态或断言证据不一致")
            if (case.get("verify_reload") or (run.get("assurance_contract") == 1 and not case.get("id"))) and (
                not isinstance(start.get("document_start"), (int, float))
                or not isinstance(end.get("document_start"), (int, float))
                or end["document_start"] <= start["document_start"]
            ):
                raise ValueError("持久化回查须在重新载入后的浏览器文档采集")
    return digest(attempt)


def prepare(directory, run, context, cases, case_id, tenant, before_state, evidence, pages=None):
    """先登记写入意图。相同用例在本轮只登记一次，避免超时后重复写入。"""
    _, fixtures, ledger = context
    case = cases[case_id]
    action = operation(run, case)
    fixture_id = case.get("fixture")
    if action == "read":
        raise ValueError("只读用例无需登记写入")
    binding = fixture_binding(directory, run, context, fixture_id, tenant, action)
    if run.get("qa_contract") == 2 and fixtures[fixture_id]["source"] == "pool":
        pool_receipt(directory, fixtures[fixture_id], "reservation", active=True, action=action)
    if case_id in ledger["attempts"] or fixture_id in ledger["closed"]:
        raise ValueError("本用例已登记过写入或该数据已回收")
    expected = before_expected(run, case, action)
    if before_state != expected:
        raise ValueError(f"{action} 的数据基线须为 {expected}")
    for previous_id, previous in ledger["attempts"].items():
        if previous.get("fixture") == fixture_id:
            verify_attempt(directory, run, context, cases.get(previous_id, {}), previous, pages=pages)
    previous_states = [entry["after_state"] for entry in ledger["attempts"].values()
                       if entry.get("fixture") == fixture_id]
    if previous_states and previous_states[-1] != before_state:
        raise ValueError("当前数据基线与上次回查不一致")
    if not previous_states and fixtures[fixture_id]["source"] == "generated" and before_state == "present":
        raise ValueError("本轮生成数据缺少已回查的创建记录")
    ledger["attempts"][case_id] = {"fixture": fixture_id, "tenant": tenant, "action": action,
                                  "binding": binding, "before_state": before_state,
                                  "before": proof(directory, evidence)}
    verify_attempt(directory, run, context, case, ledger["attempts"][case_id], settled=False, pages=pages)


def reconcile(directory, run, context, cases, case_id, tenant, state, evidence, pages=None, receipt=None):
    _, _, ledger = context
    attempt = ledger["attempts"].get(case_id)
    if not attempt or tenant != attempt.get("tenant") or "after" in attempt:
        raise ValueError("没有匹配租户的待回查写入")
    after = proof(directory, evidence)
    if after["path"] == attempt["before"]["path"]:
        raise ValueError("回查必须另存实际状态证据")
    attempt.update(after_state=state, after=after)
    if receipt:
        attempt["receipt"] = proof(directory, receipt)
    verify_attempt(directory, run, context, cases[case_id], attempt, pages=pages)


def mutation_effect(directory, run, pages, fixture, case, attempt):
    """区分发生变化、能力未获证明，以及已观察到结果不符合预期。"""
    effect = case.get("effect", {}) if run.get("evidence_contract") == 1 else {}
    if not effect:
        return {}
    start, start_at = fixture_sample(directory, run, pages, fixture, attempt["before"], "before", case)
    end, end_at = fixture_sample(directory, run, pages, fixture, attempt["after"], "after", case)
    result = {"mode": effect["mode"], "baseline": attempt["before"]["path"]}
    if effect["mode"] == "change":
        field = effect["field"]
        actual = end["fields"].get(field)
        result.update(field=field, before=start["fields"][field], after=actual,
                      verified=field in end["fields"] and digest(actual) == digest(effect["to"]))
    else:
        if not attempt.get("receipt"):
            raise ValueError("无变化操作缺少本次应用响应证据")
        receipt, receipt_at = fixture_sample(directory, run, pages, fixture, attempt["receipt"], "action", case)
        if (not start_at < receipt_at < end_at or receipt.get("action_received") is not True
                or any(receipt[key] != start[key] for key in ("session", "subject_digest"))):
            raise ValueError("应用响应与本次操作的主体、会话或采样顺序不一致")
        if redacted_measurement(start["fields"]) or redacted_measurement(end["fields"]):
            raise ValueError("无变化操作的前后字段无法核对")
        result.update(receipt=attempt["receipt"]["path"],
                      verified=all(start[key] == end[key] for key in ("count", "fields", "related")))
    return result


def verify_cleanup(directory, run, pages, fixture, attempts, saved):
    final, final_at = fixture_sample(directory, run, pages, fixture, saved, "cleanup")
    prior = [fixture_sample(directory, run, pages, fixture, attempt["after"], "after", case)
             for case, attempt in attempts]
    if any(final_at <= measured_at for _, measured_at in prior):
        raise ValueError("清理证据须采集于写入回查之后")
    if run.get("assurance_contract") == 1 and prior:
        last, _ = max(prior, key=lambda sample: sample[1])
        if (last["subject_digest"] != final["subject_digest"]
                or (last["session"], last.get("document_start")) == (final["session"], final.get("document_start"))):
            raise ValueError("须以同一授权主体在新文档或独立会话复核清理")
    if fixture["source"] == "generated" and final["count"] != 0:
        raise ValueError("本轮生成夹具仍存在，不能登记清理")
    if fixture["source"] == "generated" and final["related"] != fixture.get("cleanup_counts", {
        key: 0 for key in final["related"]
    }):
        raise ValueError("本轮关联对象仍残留，不能登记清理")
    if fixture["source"] != "generated" and attempts:
        case, first = attempts[0]
        original, _ = fixture_sample(directory, run, pages, fixture, first["before"], "before", case)
        if (not original["fields"] or redacted_measurement(original["fields"])
                or redacted_measurement(final["fields"])
                or any(final[key] != original[key] for key in ("count", "fields", "related"))):
            raise ValueError("夹具字段或关联未恢复到写入前状态")
    if fixture["source"] == "pool" and not attempts and final["count"] != 1:
        raise ValueError("未观察到可用的池夹具")
    return final


def verify_pool_restore(directory, fixture_id, evidence):
    """归还租约之前先核验同一对象的业务恢复，避免下轮拿到脏数据。"""
    def load(name):
        return json.loads((directory / name).read_text(encoding="utf-8"))

    run = load("run.json")
    context = load_context(directory)
    fixture = context[1].get(fixture_id) if text(fixture_id) else None
    if not fixture or fixture.get("source") != "pool":
        raise ValueError("本轮未登记这一测试池夹具")
    pool_receipt(directory, fixture, "reservation")
    pages = {item["id"]: item for item in load("pages.json")}
    raw = load("test-cases.json")
    raw = raw["cases"] if isinstance(raw, dict) else raw
    cases = fixture_cases({item["id"]: item for item in raw}, context[1])
    attempts = [(key, item) for key, item in context[2]["attempts"].items() if item.get("fixture") == fixture_id]
    for key, item in attempts:
        verify_attempt(directory, run, context, cases.get(key, {}), item, pages=pages)
    verify_cleanup(directory, run, pages, fixture, [(cases[key] if cases[key].get("id") else None, item)
                                                    for key, item in attempts], proof(directory, evidence))


def close_fixture(directory, run, context, cases, fixture_id, tenant, evidence, pages=None):
    _, fixtures, ledger = context
    binding = fixture_binding(directory, run, context, fixture_id, tenant, None)
    if fixture_id in ledger["closed"]:
        raise ValueError("测试数据已登记回收")
    attempts = {key: item for key, item in ledger["attempts"].items() if item.get("fixture") == fixture_id}
    if not attempts and fixtures[fixture_id]["source"] != "pool":
        raise ValueError("没有需要回收的写入或预留数据")
    for case_id, attempt in attempts.items():
        verify_attempt(directory, run, context, cases.get(case_id, {}), attempt, pages=pages)
    cleanup_proof = proof(directory, evidence)
    if any(cleanup_proof["path"] in (attempt["before"]["path"], attempt["after"]["path"])
           for attempt in attempts.values()):
        raise ValueError("回收必须另存最终复核证据")
    if run.get("qa_contract") == 2:
        verify_cleanup(directory, run, pages, fixtures[fixture_id],
                       [(cases[key] if cases[key].get("id") else None, item) for key, item in attempts.items()],
                       cleanup_proof)
        if fixtures[fixture_id]["source"] == "pool":
            if pool_receipt(directory, fixtures[fixture_id], "reservation")["lease_id"] != pool_receipt(
                directory, fixtures[fixture_id], "release_receipt"
            )["lease_id"]:
                raise ValueError("归还回执与原测试池租约不一致")
    ledger["closed"][fixture_id] = {"tenant": tenant, "method": fixtures[fixture_id]["cleanup"],
                                    "binding": binding, "evidence": cleanup_proof}


def review_fixtures(directory, run, context, cases, pages=None):
    """即使写入用例被标记为阻塞，未回查或未清理的写入仍阻止放行。"""
    _, fixtures, ledger = context
    cases = fixture_cases(cases, fixtures)
    reasons, entries = [], []
    used = {key for key, fixture in fixtures.items() if fixture.get("source") == "pool"}
    for case_id, attempt in ledger["attempts"].items():
        fixture_id = attempt.get("fixture")
        if text(fixture_id):
            used.add(fixture_id)
        try:
            verify_attempt(directory, run, context, cases.get(case_id, {}), attempt, pages=pages)
        except (OSError, ValueError) as error:
            reasons.append(f"测试数据写入未闭环 {case_id}：{error}")
        else:
            entries.append(f"{case_id} · {fixture_id} · {attempt['action']}："
                           f"{attempt['before_state']} → {attempt['after_state']}，"
                           f"证据 {attempt['before']['path']}、{attempt['after']['path']}")
    for fixture_id in sorted(used | set(ledger["closed"])):
        closure = ledger["closed"].get(fixture_id)
        if not closure:
            reasons.append(f"测试数据未验证清理：{fixture_id}")
            entries.append(f"{fixture_id}：待回收")
            continue
        try:
            binding = fixture_binding(directory, run, context, fixture_id, closure.get("tenant"), None)
            if binding != closure.get("binding") or closure.get("method") != fixtures[fixture_id]["cleanup"]:
                raise ValueError("清理范围或方式已变更")
            check_proof(directory, closure.get("evidence"))
            if run.get("qa_contract") == 2:
                attempts = [(cases.get(key) if cases.get(key, {}).get("id") else None, attempt)
                            for key, attempt in ledger["attempts"].items() if attempt.get("fixture") == fixture_id]
                verify_cleanup(directory, run, pages, fixtures[fixture_id], attempts, closure["evidence"])
                if fixtures[fixture_id]["source"] == "pool":
                    if pool_receipt(directory, fixtures[fixture_id], "reservation")["lease_id"] != pool_receipt(
                        directory, fixtures[fixture_id], "release_receipt"
                    )["lease_id"]:
                        raise ValueError("归还回执与原测试池租约不一致")
        except (OSError, ValueError) as error:
            reasons.append(f"测试数据清理证据无效 {fixture_id}：{error}")
            entries.append(f"{fixture_id}：回收证据无效")
        else:
            entries.append(f"{fixture_id}：已核验回收（{closure['method']}，{closure['evidence']['path']}）")
    return reasons, entries
