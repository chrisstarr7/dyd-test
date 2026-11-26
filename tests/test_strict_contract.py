import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_qa import URL, acceptance_run, observation, qa, run_qa
import dashboard
import qa_browser
import qa_fixtures
import qa_pool


class TestStrictContract(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.run = acceptance_run(Path(self.folder.name), strict=True)
        self.plan = qa.load_json(self.run / "test-cases.json")
        self.case = self.plan["cases"][0]
        self.case.update(scenario="normal", checks={"AC-001": {"selector": "#row", "read": "visible"}})
        self.requirements = qa.load_json(self.run / "requirements.json")
        self.requirements[0]["criteria"][0].update(risk="medium", scenarios=["normal"])
        self.page = {
            "id": "PAGE-001", "status": "confirmed", "entry": "物料列表", "url": URL,
            "role": "admin", "tenant": "local", "anchors": ["h1", "#table"], "ready": "#ready",
            "identity": {"role": {"selector": "#role"}, "tenant": {"selector": "#tenant"}},
        }
        (self.run / "prd.md").write_text("刷新后核对物料列表。\n", encoding="utf-8")
        qa.dump_json(self.run / "prd-scope.json", {
            "document": "prd.md", "revision": "v1",
            "sections": [{"from": 1, "to": 1, "status": "in_scope", "requirements": ["REQ-001"]}],
        })

    def cli(self, command, *args):
        return run_qa(command, "--dir", str(self.run), *args)[0]

    def freeze(self):
        qa.dump_json(self.run / "requirements.json", self.requirements)
        qa.dump_json(self.run / "test-cases.json", self.plan)
        qa.dump_json(self.run / "pages.json", [self.page])
        return self.cli("freeze-plan")

    def sample(self, filename, *, phase="assert", actual=True, count=None, session="local",
               case_id="TC-001", identity_proof=None, **other):
        value = dict(url=URL, role="admin", tenant="local", online=True, anchors=[1, 1], ready=1,
                     observed={"AC-001": actual} if phase in ("assert", "after") else {},
                     count=count, fields={}, related={})
        value.update(other)
        target = {"page_id": "PAGE-001"} if phase == "page" else (
            {"fixture_id": "DATA-001"} if phase == "cleanup" or case_id is None else {"case_id": case_id}
        )
        with patch("qa_browser.collect", return_value=value):
            qa_browser.capture(self.run, session, phase, filename, identity_proof=identity_proof, **target)
        return filename

    def record(self, path, state="passed", *args):
        return self.cli("record", "--case", "TC-001", "--state", state, "--result", path, *args)

    def assure(self, mode, *, evidence=False):
        run = qa.load_json(self.run / "run.json")
        run.update(mode=mode, assurance_contract=1)
        if evidence:
            run["evidence_contract"] = 1
        qa.dump_json(self.run / "run.json", run)

    def test_compound_prd_obligation_without_its_own_criterion_blocks_pass(self):
        self.assure("full", evidence=True)
        rule = "禁用员工后禁止登录并撤销已有会话"
        source = self.run / "prd.md"
        source.write_text(rule + "。\n", encoding="utf-8")
        scope = qa.load_json(self.run / "prd-scope.json")
        scope.update(source_complete=True, flows=[{"id": "FLOW-001", "source": "prd.md:1", "cases": ["TC-001"]}])
        qa.dump_json(self.run / "prd-scope.json", scope)
        self.requirements[0]["criteria"][0].update(source_line=1, source_part=1, source_quote=rule)
        review = {"revision": "v1", "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "reviewer": "独立复核样例", "reference": "本地需求原文",
                  "obligations": [
                      {"id": "OB-001", "source_line": 1, "source_part": 1, "excerpt": "禁止登录",
                       "rule": "禁止新登录", "criteria": ["AC-001"]},
                      {"id": "OB-002", "source_line": 1, "source_part": 1, "excerpt": "撤销已有会话",
                       "rule": "撤销已有会话", "criteria": []},
                  ]}
        qa.dump_json(self.run / "prd-review.json", review)
        self.assertEqual(self.freeze(), 0)
        self.assertEqual(self.record(self.sample("result.json")), 0)
        gate = qa.assess(self.run)["gate"]
        self.assertEqual(gate["test_outcome"], "incomplete")
        self.assertIn("PRD 复核义务 OB-002 缺少验收点：撤销已有会话", gate["test_reasons"])
        review["obligations"][1]["criteria"] = ["AC-001"]
        qa.dump_json(self.run / "prd-review.json", review)
        self.assertIn("复用其他义务", self.freeze())

    def documented_update(self, effect, expected):
        self.assure("targeted", evidence=True)
        self.page["identity"]["principal"] = {"selector": "#account"}
        self.case.update(operation="update", fixture="DATA-001", verify_reload=True, effect=effect,
                         expected={"AC-001": expected},
                         checks={"AC-001": {"selector": ".name", "read": "text", "scope": "fixture"}})
        self.assertEqual(self.freeze(), 0)
        (self.run / "scope.txt").write_text("Isolated local effect test", encoding="utf-8")
        marker = "qa-r1-item"
        qa.dump_json(self.run / "fixtures.json", {
            "boundary": {"environment": "test", "url": URL, "tenant": "local", "authorization": "local mock",
                         "actions": ["update", "restore"], "external_effects": "disabled", "evidence": "scope.txt"},
            "fixtures": [{"id": "DATA-001", "tenant": "local", "page": "PAGE-001", "source": "reused",
                          "ownership": "verified", "reference": marker, "marker": marker,
                          "actions": ["update", "restore"], "cleanup": "restore", "evidence": "scope.txt",
                          "cleanup_scope": "local object", "probe": {"selector": "#row", "ready": "#ready",
                              "lookup": "complete", "key": {"selector": ".id", "value": marker},
                              "fields": {"name": {"selector": ".name", "read": "text"}}}}],
        })
        return "--case", "TC-001", "--tenant", "local", "--state", "present"

    def test_update_requires_a_different_baseline_and_verified_new_value(self):
        args = self.documented_update({"mode": "change", "field": "name", "to": "after"}, "after")
        before = self.sample("stale.json", phase="before", principal="admin", count=1, document_start=1,
                             fields={"name": "after"})
        self.assertIn("已等于目标值", self.cli("prepare-data", *args, "--evidence", before))
        before = self.sample("before.json", phase="before", principal="admin", count=1, document_start=1,
                             fields={"name": "before"})
        self.assertEqual(self.cli("prepare-data", *args, "--evidence", before), 0)
        after = self.sample("after.json", phase="after", principal="admin", count=1, document_start=2,
                            fields={"name": "before"}, actual="after")
        self.assertEqual(self.cli("reconcile-data", *args, "--evidence", after), 0)
        self.assertNotEqual(self.record(after), 0)
        self.assertEqual(self.record(after, "failed"), 0)
        self.assertFalse(qa.assess(self.run)["results"]["TC-001"]["observation"]["effect"]["verified"])

    def test_unchanged_update_requires_a_fresh_application_receipt(self):
        args = self.documented_update({"mode": "unchanged", "receipt": {"selector": "#notice", "text": "Rejected"}}, "before")
        before = self.sample("before.json", phase="before", principal="admin", count=1, document_start=1,
                             fields={"name": "before"}, observed={"": False})
        self.assertEqual(self.cli("prepare-data", *args, "--evidence", before), 0)
        receipt = self.sample("receipt.json", phase="action", principal="admin", count=1, document_start=1,
                              fields={"name": "before"}, observed={"": True})
        after = self.sample("after.json", phase="after", principal="admin", count=1, document_start=2,
                            fields={"name": "before"}, actual="before")
        self.assertEqual(self.cli("reconcile-data", *args, "--evidence", after, "--receipt", receipt), 0)
        _, fixtures, ledger = qa_fixtures.load_context(self.run)
        attempt = dict(ledger["attempts"]["TC-001"])
        del attempt["receipt"]
        with self.assertRaisesRegex(ValueError, "缺少本次应用响应"):
            qa_fixtures.mutation_effect(self.run, qa.load_json(self.run / "run.json"), {"PAGE-001": self.page},
                                        fixtures["DATA-001"], self.case, attempt)
        self.assertEqual(self.record(after), 0)

    def test_full_run_rejects_same_line_omission_and_requires_reviewed_scope(self):
        self.assure("full")
        source = self.run / "prd.md"
        source.write_text("提交后刷新；失败时保留原记录。\n第二项规则待业务确认。\n", encoding="utf-8")
        scope = qa.load_json(self.run / "prd-scope.json")
        scope.update(source_complete=False, flows=[{"id": "FLOW-001", "source": "prd.md:1", "cases": ["TC-001"]}])
        scope["sections"].append({"from": 2, "to": 2, "status": "pending", "reason": "业务口径未确认"})
        qa.dump_json(self.run / "prd-scope.json", scope)
        criteria = self.requirements[0]["criteria"]
        criteria[0].update(source_line=1, source_part=1, source_quote="提交后刷新")
        self.assertIn("缺少验收点", self.freeze())
        criteria.append(dict(criteria[0], id="AC-002", source_part=2, source_quote="失败时保留原记录",
                             required=False, scope_note="跳过", scope_decision="本地说明"))
        self.assertIn("不能标为非必测", self.freeze())
        criteria[1].update(required=True, risk="high", risk_note="只验证主流程")
        self.case["expected"]["AC-002"] = True
        self.case["checks"]["AC-002"] = {"selector": "#other", "read": "visible"}
        self.assertIn("高风险缺少", self.freeze())
        criteria[1]["risk"] = "medium"
        self.assertEqual(self.freeze(), 0)
        self.assertEqual(self.record(self.sample("result.json", observed={"AC-001": True, "AC-002": True})), 0)
        gate = qa.assess(self.run)["gate"]
        self.assertEqual(gate["test_outcome"], "incomplete")
        self.assertIn("完整 PRD 或引用附件未确认", gate["test_reasons"])
        self.assertIn("缺少完整测试风险评审", gate["test_reasons"])
        self.assertTrue(any("业务口径未确认" in reason for reason in gate["test_reasons"]))

    def test_assured_chain_rejects_wrong_actor_and_keeps_compensation_available(self):
        self.assure("targeted")
        self.page["identity"]["principal"] = {"selector": "#account"}
        self.case.update(operation="create", fixture="DATA-001", verify_reload=True)
        login = dict(self.case, id="TC-002", operation="read", actor="fixture", depends_on=["TC-001"])
        self.plan["cases"].append(login)
        self.assertEqual(self.freeze(), 0)
        (self.run / "scope.txt").write_text("isolated local coverage test", encoding="utf-8")
        marker = "qa-r1-item"
        qa.dump_json(self.run / "fixtures.json", {
            "boundary": {"environment": "test", "url": URL, "tenant": "local", "authorization": "local mock",
                         "actions": ["create", "delete"], "external_effects": "disabled", "evidence": "scope.txt"},
            "fixtures": [{"id": "DATA-001", "tenant": "local", "page": "PAGE-001", "source": "generated",
                          "ownership": "verified", "reference": marker, "marker": marker,
                          "actions": ["create", "delete"], "cleanup": "delete", "evidence": "scope.txt",
                          "cleanup_scope": "account and session", "cleanup_counts": {"session": 0},
                          "probe": {"selector": "#row", "ready": "#ready", "lookup": "complete", "fields": {},
                                    "key": {"selector": ".id", "value": marker}, "related": {"session": "#session"}}}],
        })
        login_args = ("--case", "TC-002", "--state", "observed")
        early = self.sample("early.json", case_id="TC-002", session="member", principal=marker)
        self.assertIn("更早的有效通过证据", self.cli("record", *login_args, "--result", early))
        args = ("--case", "TC-001", "--tenant", "local")
        before = self.sample("before.json", phase="before", count=0, document_start=1, principal="admin-a",
                             related={"session": 0})
        self.assertEqual(self.cli("prepare-data", *args, "--state", "absent", "--evidence", before), 0)
        wrong = self.sample("wrong-admin.json", phase="after", count=1, document_start=2, principal="admin-b",
                            related={"session": 0})
        self.assertIn("实际登录主体不一致", self.cli("reconcile-data", *args, "--state", "present", "--evidence", wrong))
        after = self.sample("after.json", phase="after", count=1, document_start=2, principal="admin-a",
                            related={"session": 0})
        self.assertEqual(self.cli("reconcile-data", *args, "--state", "present", "--evidence", after), 0)
        self.assertEqual(self.record(after), 0)
        self.assertIn("更早的有效通过证据", self.cli("record", *login_args, "--result", early))
        reused = self.sample("reused.json", case_id="TC-002", principal=marker)
        self.assertIn("独立会话", self.cli("record", *login_args, "--result", reused))
        wrong = self.sample("wrong-member.json", case_id="TC-002", session="member", principal="other-account")
        self.assertEqual(self.cli("record", *login_args, "--result", wrong), 0)
        self.assertFalse(qa.assess(self.run)["results"]["TC-002"]["observation"]["actor_verified"])

        cleanup_args = ("--cleanup", "DATA-001", "--tenant", "local")
        before = self.sample("compensate-before.json", case_id=None, phase="before", count=1,
                             document_start=3, principal="admin-a", related={"session": 1})
        self.assertEqual(self.cli("prepare-data", *cleanup_args, "--state", "present", "--evidence", before), 0)
        after = self.sample("compensate-after.json", case_id=None, phase="after", count=0,
                            document_start=4, principal="admin-a", observed={}, related={"session": 1})
        self.assertEqual(self.cli("reconcile-data", *cleanup_args, "--state", "absent", "--evidence", after), 0)
        close_args = ("--fixture", "DATA-001", "--tenant", "local")
        same_doc = self.sample("same-doc.json", phase="cleanup", count=0, document_start=4, principal="admin-a",
                               related={"session": 0})
        self.assertIn("新文档", self.cli("close-data", *close_args, "--evidence", same_doc))
        retained = self.sample("retained.json", phase="cleanup", count=0, document_start=5, principal="admin-a",
                               related={"session": 1})
        self.assertIn("关联对象仍残留", self.cli("close-data", *close_args, "--evidence", retained))
        clean = self.sample("clean.json", phase="cleanup", count=0, document_start=6, principal="admin-a",
                            related={"session": 0})
        self.assertEqual(self.cli("close-data", *close_args, "--evidence", clean), 0)
        self.assertEqual(qa.assess(self.run)["gate"]["test_outcome"], "failed")

    def test_gate_requires_registered_measurement_and_separate_admission(self):
        self.assertEqual(self.freeze(), 0)
        self.assertNotEqual(self.record(observation(self.run)), 0)
        path = self.sample("measured.json")
        self.assertEqual(self.record(path), 0)
        gate = qa.assess(self.run)["gate"]
        self.assertEqual((gate["test_verdict"], gate["release_verdict"]), ("pass", "fail"))
        html = dashboard.build_html(self.run)
        self.assertIn('<p class="gate passed">测试通过</p>', html)
        self.assertIn('<summary>正式发布准入：未通过</summary>', html)
        report = qa.format_report(qa.assess(self.run))
        self.assertIn("- 本轮功能验证：测试通过\n- 正式发布准入：未通过", report)
        self.sample("smoke.json", phase="page")
        preflight = qa.load_json(self.run / "preflight.json")
        preflight.update(evidence="smoke.json", release_evidence="measured.json")
        qa.dump_json(self.run / "preflight.json", preflight)
        self.assertEqual(qa.assess(self.run)["gate"]["release_verdict"], "fail")
        qa.dump_json(self.run / "release.json", {
            "type": "release_record", "source": "isolated mock build manifest", "url": URL, "version": "v1",
            "build_id": "v1", "scope": "backend", "components": {"backend": "mock-v1"},
            "migration": {"status": "not_applicable", "basis": "内存数据"}},
        )
        preflight["release_evidence"] = "release.json"
        qa.dump_json(self.run / "preflight.json", preflight)
        self.assertEqual(qa.assess(self.run)["gate"]["release_verdict"], "pass")
        data = qa.load_json(self.run / path)
        data["observed"]["AC-001"] = False
        qa.dump_json(self.run / path, data)
        self.assertEqual(qa.assess(self.run)["results"]["TC-001"]["state"], "unknown")

    def test_nondeterministic_case_cannot_pass_without_complete_matrix(self):
        from test_matrix import default_row, matrix_plan

        self.assure("targeted")
        plan = matrix_plan()
        plan["id"] = "TC-001"
        plan["matrix"]["environment"].update(url=URL, version="v1")
        self.case.update(test_kind="recall", matrix=plan["matrix"])
        self.assertEqual(self.freeze(), 0)
        self.assertNotEqual(self.record(self.sample("matrix-missing.json")), 0)
        (self.run / "evidence").mkdir()
        rows = []
        for query in plan["matrix"]["queries"]:
            for repeat in (1, 2):
                row = default_row(query, repeat)
                rows.append(row)
                (self.run / row["evidence"]).write_text("{}", encoding="utf-8")
        qa.dump_json(self.run / "evidence" / "matrix.json", {
            "case_id": "TC-001",
            "environment": plan["matrix"]["environment"],
            "queries": rows,
        })
        self.assertEqual(self.record(self.sample("matrix-result.json")), 0)
        review = qa.assess(self.run)
        self.assertTrue(review["results"]["TC-001"]["observation"]["matrix"]["passed"])
        self.assertFalse(review["matrix_reviews"]["TC-001"]["flaky"])

    def test_unmapped_prd_and_plan_edits_are_blocked(self):
        prd = self.run / "prd.md"
        prd.write_text("第一条。\n第二条缺少验收。\n", encoding="utf-8")
        self.assertNotEqual(self.freeze(), 0)
        prd.write_text("第一条。\n", encoding="utf-8")
        self.assertEqual(self.freeze(), 0)
        self.sample("current.json")
        self.case["expected"]["AC-001"] = False
        qa.dump_json(self.run / "test-cases.json", self.plan)
        self.assertNotEqual(self.record("current.json", "failed"), 0)
        self.assertEqual(qa.assess(self.run)["gate"]["test_verdict"], "fail")

    def test_setup_requires_a_fixture_referenced_by_frozen_case(self):
        self.case.update(operation="read", fixture="DATA-001")
        self.assertEqual(self.freeze(), 0)
        evidence = "scope.txt"
        (self.run / evidence).write_text("本地模拟，范围已确认", encoding="utf-8")
        actions = ["create", "delete"]
        fixtures = []
        for fixture_id in ("DATA-001", "DATA-002"):
            marker = f"qa-r1-{fixture_id}"
            fixtures.append({"id": fixture_id, "tenant": "local", "page": "PAGE-001", "source": "generated",
                             "ownership": "verified", "reference": marker, "marker": marker,
                             "actions": actions, "cleanup": "delete", "evidence": evidence,
                             "probe": {"selector": f"#{marker}", "fields": {}}})
        qa.dump_json(self.run / "fixtures.json", {
            "boundary": {"environment": "test", "url": URL, "tenant": "local", "authorization": "本地模拟",
                         "actions": actions, "external_effects": "disabled", "evidence": evidence},
            "fixtures": fixtures,
        })
        measured = dict(url=URL, role="admin", tenant="local", online=True, anchors=[1, 1], ready=1,
                        observed={}, document_start=1, count=0, fields={}, related={})
        for fixture in fixtures:
            fixture_id = fixture["id"]
            before = f"{fixture_id}-before.json"
            with patch("qa_browser.collect", return_value=measured.copy()):
                qa_browser.capture(self.run, "local", "before", before, fixture_id=fixture_id)
            args = ("--setup", fixture_id, "--tenant", "local", "--state", "absent", "--evidence", before)
            if fixture_id == "DATA-001":
                self.assertEqual(self.cli("prepare-data", *args), 0)
            else:
                with self.assertRaisesRegex(SystemExit, "未关联本轮冻结用例"):
                    qa.main(["prepare-data", "--dir", str(self.run), *args])
        self.assertEqual(set(qa.load_json(self.run / "fixture-actions.json")["attempts"]), {"setup:DATA-001"})
        self.assertEqual(self.record(self.sample("search-result.json")), 0)

    def test_retry_needs_reason_and_p1_closure_needs_linked_retest(self):
        self.assertEqual(self.freeze(), 0)
        self.assertEqual(self.record(self.sample("first.json", actual=False), "failed"), 0)
        actual = self.sample("second.json")
        self.assertNotEqual(self.record(actual), 0)
        self.assertEqual(self.cli("defect", "--id", "BUG-001", "--title", "缺陷", "--severity", "P1",
                                  "--status", "closed"), 0)
        self.assertEqual(self.record(actual, "passed", "--retry-reason", "fix_verified", "--note", "已归因回归",
                                     "--defect", "BUG-001"), 0)
        self.assertEqual(qa.assess(self.run)["gate"]["test_verdict"], "fail")
        self.assertEqual(self.cli("close-defect", "--id", "BUG-001", "--fix-version", "v1",
                                  "--retest-case", "TC-001"), 0)
        review = qa.assess(self.run)
        self.assertEqual(review["gate"]["test_verdict"], "pass")
        self.assertIn("执行与复测记录", qa.format_report(review))
        self.assertEqual(len(review["history"]["TC-001"]), 2)

    def test_retained_or_wrong_session_fixture_is_not_cleared(self):
        self.case.update(operation="create", fixture="DATA-001", verify_reload=True)
        self.assertEqual(self.freeze(), 0)
        (self.run / "scope.txt").write_text("本地模拟，配置已确认", encoding="utf-8")
        qa.dump_json(self.run / "fixtures.json", {
            "boundary": {"environment": "test", "url": URL, "tenant": "local", "authorization": "本地模拟",
                         "actions": ["create", "delete"], "external_effects": "disabled", "evidence": "scope.txt"},
            "fixtures": [{"id": "DATA-001", "tenant": "local", "page": "PAGE-001", "source": "generated",
                          "ownership": "verified", "reference": "qa-r1-item", "marker": "qa-r1-item",
                          "actions": ["create", "delete"], "cleanup": "delete", "evidence": "scope.txt",
                          "probe": {"selector": "#row", "fields": {}}}],
        })
        before = self.sample("before.json", phase="before", count=0, document_start=1)
        args = ("--case", "TC-001", "--tenant", "local")
        self.assertEqual(self.cli("prepare-data", *args, "--state", "absent", "--evidence", before), 0)
        wrong = self.sample("wrong.json", phase="after", count=1, session="wrong", document_start=2)
        self.assertNotEqual(self.cli("reconcile-data", *args, "--state", "present", "--evidence", wrong), 0)
        stale = self.sample("stale.json", phase="after", count=1, document_start=1)
        self.assertNotEqual(self.cli("reconcile-data", *args, "--state", "present", "--evidence", stale), 0)
        after = self.sample("after.json", phase="after", count=1, document_start=2)
        self.assertEqual(self.cli("reconcile-data", *args, "--state", "present", "--evidence", after), 0)
        self.assertEqual(self.record(after), 0)
        invalid = self.sample("retained.json", phase="cleanup", count=1)
        final = ("--fixture", "DATA-001", "--tenant", "local")
        self.assertNotEqual(self.cli("close-data", *final, "--evidence", invalid), 0)
        self.assertEqual(qa.assess(self.run)["gate"]["test_verdict"], "fail")
        clean = self.sample("clean.json", phase="cleanup", count=0)
        self.assertEqual(self.cli("close-data", *final, "--evidence", clean), 0)
        self.assertEqual(qa.assess(self.run)["gate"]["test_verdict"], "pass")

    def test_pool_releases_only_after_browser_verifies_restored_fields(self):
        self.case.update(operation="update", fixture="DATA-001")
        self.case["expected"]["AC-001"] = "1.00"
        self.case["checks"]["AC-001"] = {"derive": "difference", "operands": [
            {"selector": "#qty", "read": "text"},
            {"selector": "#qty", "read": "text", "source": "before"},
        ]}
        self.assertEqual(self.freeze(), 0)
        profile = Path(self.folder.name) / "qa-profile.json"
        qa.dump_json(profile, {
            "project": "p", "environment": "test", "url": URL, "tenant": "local",
            "authorization": "approved-mock", "external_effects": "disabled", "actions": ["update", "release"],
            "fixtures": [{"id": "DATA-001", "reference": "mock-item", "ownership": "verified",
                          "actions": ["update", "release"]}],
        })
        scope = ("--dir", str(self.run), "--profile", str(profile), "--tenant", "local", "--operation", "update")
        with redirect_stdout(io.StringIO()) as output:
            qa_pool.main(["reserve", *scope])
        qa.dump_json(self.run / "reserved.json", json.loads(output.getvalue()))
        qa.dump_json(self.run / "fixtures.json", {
            "boundary": {"environment": "test", "url": URL, "tenant": "local", "authorization": "本地模拟",
                         "actions": ["update", "release"], "external_effects": "disabled", "evidence": "reserved.json"},
            "fixtures": [{"id": "DATA-001", "tenant": "local", "page": "PAGE-001", "source": "pool",
                          "ownership": "verified", "reference": "mock-item", "marker": "qa-r1-pool",
                          "actions": ["update", "release"], "cleanup": "release", "evidence": "reserved.json",
                          "pool_profile": str(profile), "reservation": "reserved.json", "release_receipt": "released.json",
                          "probe": {"selector": "#row", "fields": {"quantity": {"selector": "#qty", "read": "text"}}}}],
        })
        args = ("--case", "TC-001", "--tenant", "local", "--state", "present")
        before = self.sample("pool-before.json", phase="before", count=1, fields={"quantity": "2"},
                             observed={"AC-001.1": "2"})
        after = self.sample("pool-after.json", phase="after", count=1, fields={"quantity": "3"},
                            observed={"AC-001.0": "3"})
        self.assertEqual(self.cli("prepare-data", *args, "--evidence", before), 0)
        self.assertEqual(self.cli("reconcile-data", *args, "--evidence", after), 0)
        self.assertEqual(self.record(after), 0)
        measured = qa.assess(self.run)["results"]["TC-001"]
        self.assertEqual(measured["observation"]["baseline_evidence"], before)
        self.assertEqual(measured["observation"]["assertions"]["AC-001"]["actual"], "1.00")
        bad = self.sample("pool-dirty.json", phase="cleanup", count=1, fields={"quantity": "3"})
        with redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
            qa_pool.main(["release", *scope, "--receipt", "reserved.json", "--evidence", bad])
        self.assertIn("未恢复到写入前状态", error.getvalue())
        good = self.sample("pool-clean.json", phase="cleanup", count=1, fields={"quantity": "2"})
        with redirect_stdout(io.StringIO()) as output:
            qa_pool.main(["release", *scope, "--receipt", "reserved.json", "--evidence", good])
        qa.dump_json(self.run / "released.json", json.loads(output.getvalue()))
        self.assertEqual(self.cli("close-data", "--fixture", "DATA-001", "--tenant", "local", "--evidence", good), 0)
        self.assertEqual(qa.assess(self.run)["gate"]["test_verdict"], "pass")

    def test_empty_result_uses_checked_session_identity_only(self):
        self.page["identity"]["principal"] = {"selector": "#current-account"}
        self.case["expected"]["AC-001"] = 0
        self.case["checks"]["AC-001"] = {"selector": "#row", "read": "count"}
        self.assertEqual(self.freeze(), 0)
        principal = "test-account@example.test"
        source = self.sample("authenticated.json", phase="page", principal=principal, document_start=1)
        self.assertNotIn(principal, (self.run / source).read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "当前页面无法确认角色"):
            self.sample("no-proof.json", role=None, principal=principal, document_start=1, actual=0)
        for name, changes in (
            ("different-user", {"principal": "different", "document_start": 1}),
            ("reloaded", {"principal": principal, "document_start": 2}),
            ("other-session", {"principal": principal, "document_start": 1, "session": "other"}),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "页面文档已变化"):
                self.sample(name + ".json", role=None, actual=0, identity_proof=source, **changes)
        final = self.sample("empty.json", role=None, actual=0, principal=principal, document_start=1,
                            identity_proof=source)
        self.assertEqual(self.record(final), 0)
        (self.run / source).unlink()
        self.assertEqual(qa.assess(self.run)["results"]["TC-001"]["state"], "unknown")

    def test_write_recheck_can_reuse_same_document_role_when_filter_removes_row(self):
        self.page["identity"]["principal"] = {"selector": "#current-account"}
        self.case.update(operation="create", fixture="DATA-001", verify_reload=True)
        self.assertEqual(self.freeze(), 0)
        (self.run / "scope.txt").write_text("本地模拟，配置已确认", encoding="utf-8")
        qa.dump_json(self.run / "fixtures.json", {
            "boundary": {"environment": "test", "url": URL, "tenant": "local", "authorization": "本地模拟",
                         "actions": ["create", "delete"], "external_effects": "disabled", "evidence": "scope.txt"},
            "fixtures": [{"id": "DATA-001", "tenant": "local", "page": "PAGE-001", "source": "generated",
                          "ownership": "verified", "reference": "qa-r1-item", "marker": "qa-r1-item",
                          "actions": ["create", "delete"], "cleanup": "delete", "evidence": "scope.txt",
                          "probe": {"selector": "#row", "fields": {}}}],
        })
        principal = "test-account@example.test"
        page_before = self.sample("identity-before.json", phase="page", principal=principal, document_start=1)
        before = self.sample("before-empty.json", phase="before", count=0, role=None, principal=principal,
                             document_start=1, identity_proof=page_before)
        args = ("--case", "TC-001", "--tenant", "local")
        self.assertEqual(self.cli("prepare-data", *args, "--state", "absent", "--evidence", before), 0)
        with self.assertRaisesRegex(ValueError, "页面文档已变化"):
            self.sample("wrong-doc.json", phase="after", count=1, role=None, principal=principal,
                        document_start=2, identity_proof=page_before)
        page_after = self.sample("identity-after.json", phase="page", principal=principal, document_start=2)
        after = self.sample("after-present.json", phase="after", count=1, role=None, principal=principal,
                            document_start=2, identity_proof=page_after)
        self.assertEqual(self.cli("reconcile-data", *args, "--state", "present", "--evidence", after), 0)
        self.assertEqual(self.record(after), 0)
        page_clean = self.sample("identity-clean.json", phase="page", principal=principal, document_start=3)
        clean = self.sample("clean-empty.json", phase="cleanup", count=0, role=None, principal=principal,
                            document_start=3, identity_proof=page_clean)
        self.assertEqual(self.cli("close-data", "--fixture", "DATA-001", "--tenant", "local",
                                  "--evidence", clean), 0)
        self.assertEqual(qa.assess(self.run)["gate"]["test_verdict"], "pass")

    def test_finite_decimal_oracle_catches_unbalanced_amounts(self):
        self.case["expected"]["AC-001"] = "0.00"
        self.case["checks"]["AC-001"] = {"derive": "difference", "operands": [
            {"selector": "#total", "read": "text"},
            {"selector": "#allocated", "read": "text"},
            {"selector": "#remaining", "read": "text"},
        ], "scale": 2}
        self.assertEqual(self.freeze(), 0)
        result = self.sample("unbalanced.json", observed={
            "AC-001.0": "1,000.01", "AC-001.1": "750.00", "AC-001.2": "250.00"})
        self.assertNotEqual(self.record(result), 0)
        self.assertEqual(self.record(result, "failed"), 0)
        review = qa.assess(self.run)
        self.assertEqual(review["gate"]["test_outcome"], "failed")
        self.assertEqual(review["results"]["TC-001"]["observation"]["assertions"]["AC-001"]["actual"], "0.01")
        self.assertEqual(review["stats"]["criterion_pass_rate"], 0.0)
        self.assertNotIn("AC-001", review["passed_criteria"])

    def test_tool_block_is_reported_as_incomplete_not_product_failure(self):
        self.assertEqual(self.freeze(), 0)
        options = ("--case", "TC-001", "--state", "blocked", "--note", "采集未取得数据",
                   "--blocker-kind", "tool")
        self.assertNotEqual(self.cli("record", *options), 0)
        self.assertEqual(self.cli("record", *options, "--unblock", "恢复浏览器采集"), 0)
        review = qa.assess(self.run)
        self.assertEqual(review["gate"]["test_outcome"], "incomplete")
        self.assertIn("测试工具 · 采集未取得数据", qa.format_report(review))

    def test_risk_matrix_freezes_scope_and_omitted_critical_scenario(self):
        plan = {
            "schema_version": 1, "run_id": "r1", "version": "v1", "source": "prd.md",
            "dimensions": {"data": {"status": "in_scope"}},
            "risks": [{"id": "RISK-001", "dimension": "data", "severity": "high",
                       "source": "刷新后核对物料列表", "invariant": "重新查询仍包含目标物料",
                       "impact": "用户无法确认数据是否保存", "scenarios": [
                           {"id": "SCN-001", "scenario": "negative", "layer": "online_ui", "criterion": "AC-001"},
                       ]}],
        }
        qa.dump_json(self.run / "risk-review.json", plan)
        self.assertNotEqual(self.freeze(), 0)
        plan["risks"][0]["scenarios"][0]["scenario"] = "normal"
        qa.dump_json(self.run / "risk-review.json", plan)
        self.assertEqual(self.freeze(), 0)
        path = self.sample("risk-pass.json")
        self.assertEqual(self.record(path), 0)
        review = qa.assess(self.run)
        self.assertEqual(review["risk_review"]["risks"]["RISK-001"]["status"], "verified")
        self.assertIn("数据 · 已验证", qa.format_report(review))
        plan["risks"][0]["impact"] = "测试后变动判据"
        qa.dump_json(self.run / "risk-review.json", plan)
        self.assertEqual(qa.assess(self.run)["gate"]["test_outcome"], "incomplete")

    def test_redaction_keeps_json_valid_and_masks_personal_values(self):
        self.assertEqual(qa.load_json(self.run / "run.json")["qa_contract"], 2)
        self.assertEqual(
            qa.redact_text('{"password":"example","access_token":"example","cell":"15500000000"}'),
            '{"password":"[REDACTED]","access_token":"[REDACTED]","cell":"[REDACTED]"}',
        )
        self.assertEqual(qa_browser.redact_data({"phone": 15500000000, "label": "name@example.test"}),
                         {"phone": "[REDACTED]", "label": "[REDACTED]"})
