import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from qa_risk import DIMENSIONS, RISK_REVIEW_FILE, review_risks


class TestRiskReview(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.run = {"run_id": "r7", "version": "v2", "mode": "targeted"}
        self.source = "重复提交同一订单只扣减一次库存。"
        (self.directory / "scope.md").write_text(self.source, encoding="utf-8")
        self.risk = {
            "id": "RISK-001", "dimension": "data", "severity": "high",
            "source": self.source, "invariant": "同一订单的库存扣减次数恰为一次",
            "impact": "重复扣减导致可用库存低于实际库存",
            "scenarios": [
                {"id": "SC-001", "scenario": "normal", "layer": "online_ui", "criterion": "AC-001"},
                {"id": "SC-002", "scenario": "negative", "layer": "online_ui", "criterion": "AC-001"},
            ],
        }
        self.plan = {"schema_version": 1, "run_id": "r7", "version": "v2", "source": "scope.md",
                     "dimensions": {"data": {"status": "in_scope"}}, "risks": [self.risk]}
        self.cases = {
            "TC-001": {"id": "TC-001", "scenario": "normal", "expected": {"AC-001": True}},
            "TC-002": {"id": "TC-002", "scenario": "negative", "expected": {"AC-001": True}},
        }
        self.results = {case_id: {"state": "passed"} for case_id in self.cases}

    def write_json(self, name, value):
        (self.directory / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def decision(self, name, subject, decision, **overrides):
        record = {"type": "risk_decision", "run_id": "r7", "version": "v2", "subject": subject,
                  "decision": decision, "by": "测试负责人", "reason": "已复核本轮范围及影响",
                  "reference": "审批单 QA-17"}
        self.write_json(name, {**record, **overrides})
        return name

    def review(self):
        self.write_json(RISK_REVIEW_FILE, self.plan)
        return review_risks(self.directory, self.run, self.cases, self.results)

    def unsupported_scenario(self):
        self.plan["blocked_layers"] = {"read_only_db": {"reason": "本轮无只读数据源"}}
        entry = self.risk["scenarios"][1]
        entry["layer"] = "read_only_db"
        return entry

    def test_optional_plan_keeps_unreviewed_dimensions_and_adds_no_blockers(self):
        reasons, summary = review_risks(self.directory, self.run, self.cases, self.results)

        self.assertEqual(reasons, [])
        self.assertEqual(summary["status"], "not_provided")
        self.assertIsNone(summary["plan_sha256"])
        self.assertEqual(set(summary["dimensions"]), set(DIMENSIONS))
        self.assertTrue(all(item["status"] == "unreviewed" for item in summary["dimensions"].values()))

    def test_valid_plan_preserves_scope_and_serializes_summary(self):
        self.plan["dimensions"]["accessibility"] = {"status": "not_applicable", "decision": self.decision(
            "na.json", "dimension:accessibility", "not_applicable",
        )}
        self.plan["dimensions"]["regression"] = {"status": "out_of_scope", "decision": self.decision(
            "scope-decision.json", "dimension:regression", "out_of_scope",
        )}
        reasons, summary = self.review()

        self.assertEqual(reasons, [])
        self.assertEqual(summary, json.loads(json.dumps(summary)))
        self.assertEqual(summary["dimensions"]["data"], {"status": "in_scope", "coverage": "verified"})
        self.assertEqual(summary["dimensions"]["performance"], {"status": "unreviewed"})
        self.assertEqual(summary["dimensions"]["accessibility"]["status"], "not_applicable")
        self.assertEqual(summary["dimensions"]["regression"]["status"], "out_of_scope")
        self.assertEqual(summary["risks"]["RISK-001"]["scenarios"]["SC-002"]["case_ids"], ["TC-002"])

    def test_omitted_high_risk_scenario_cannot_be_covered_by_a_different_tuple(self):
        for change in ({"scenario": "boundary"}, {"expected": {"AC-002": True}}, {"layer": "offline_ui"}):
            with self.subTest(change=change):
                original = self.cases["TC-002"]
                self.cases["TC-002"] = {**original, **change}
                reasons, summary = self.review()
                self.cases["TC-002"] = original

                self.assertIn("RISK-001/SC-002", reasons[0])
                risk = summary["risks"]["RISK-001"]
                self.assertEqual(risk["status"], "open")
                self.assertEqual(risk["scenarios"]["SC-002"]["status"], "missing")

    def test_high_risk_requires_a_scenario_and_explicit_scope_requires_a_risk(self):
        self.risk["scenarios"].clear()
        self.plan["dimensions"]["reliability"] = {"status": "in_scope"}
        reasons, summary = self.review()

        self.assertEqual(len(reasons), 2)
        self.assertIn("RISK-001", reasons[0])
        self.assertIn("reliability", reasons[1])
        self.assertEqual(summary["dimensions"]["reliability"]["coverage"], "unreviewed")

    def test_unsupported_layer_requires_a_valid_acceptance_to_dispose_high_risk(self):
        entry = self.unsupported_scenario()
        reasons, summary = self.review()
        self.assertIn("unsupported", reasons[0])
        self.assertEqual(summary["layers"]["read_only_db"]["status"], "unsupported")

        entry["acceptance"] = self.decision("accept.json", "scenario:RISK-001/SC-002", "accepted")
        reasons, summary = self.review()
        risk = summary["risks"]["RISK-001"]

        self.assertEqual(reasons, [])
        self.assertEqual(risk["status"], "accepted")
        self.assertEqual(risk["scenarios"]["SC-002"]["status"], "unsupported")
        self.assertEqual(risk["scenarios"]["SC-002"]["decision"]["evidence"], "accept.json")

    def test_freeze_allows_explicitly_unsupported_layer_but_blocks_missing_supported_scenario(self):
        self.unsupported_scenario()
        self.results = None
        reasons, summary = self.review()

        self.assertEqual(reasons, [])
        risk = summary["risks"]["RISK-001"]
        self.assertEqual(risk["status"], "open")
        self.assertEqual(risk["scenarios"]["SC-002"]["status"], "unsupported")
        self.assertEqual(summary["dimensions"]["data"]["coverage"], "open")

        del self.cases["TC-001"]
        reasons, summary = self.review()
        self.assertEqual(len(reasons), 1)
        self.assertIn("RISK-001/SC-001", reasons[0])
        self.assertEqual(summary["risks"]["RISK-001"]["scenarios"]["SC-001"]["status"], "missing")

    def test_bogus_acceptance_evidence_is_rejected(self):
        entry = self.unsupported_scenario()
        subject = "scenario:RISK-001/SC-002"
        for evidence in ("missing.json", "scope.md", RISK_REVIEW_FILE, self.decision(
                "wrong-subject.json", "scenario:RISK-001/SC-001", "accepted"), self.decision(
                "old.json", subject, "accepted", version="v1"), self.decision(
                "placeholder.json", subject, "accepted", by="TBD")):
            with self.subTest(evidence=evidence):
                entry["acceptance"] = evidence
                reasons, summary = self.review()

                self.assertIn(subject, reasons[0])
                self.assertEqual(summary["status"], "invalid")

    def test_not_applicable_without_matching_evidence_is_unreviewed(self):
        self.plan["dimensions"]["permission"] = {"status": "not_applicable", "decision": self.decision(
            "na.json", "dimension:permission", "out_of_scope",
        )}
        reasons, summary = self.review()

        self.assertIn("dimension:permission", reasons[0])
        self.assertEqual(summary["dimensions"]["permission"]["status"], "unreviewed")

    def test_source_must_be_local_and_contain_the_cited_text(self):
        self.risk["source"] = "没有出现在范围文档中的声明。"
        reasons, summary = self.review()
        self.assertIn("RISK-001", reasons[0])
        self.assertEqual(summary["status"], "invalid")

        self.plan["source"] = str(self.directory / "scope.md")
        reasons, summary = self.review()
        self.assertIn("相对路径", reasons[0])
        self.assertEqual(summary["status"], "invalid")

    def test_freeze_checks_design_with_same_fingerprint_as_assessment(self):
        reasons, assessed = self.review()
        self.assertEqual(reasons, [])
        reasons, design = review_risks(self.directory, self.run, list(self.cases.values()), None)

        self.assertEqual(reasons, [])
        self.assertEqual(design["risks"]["RISK-001"]["status"], "planned")
        self.assertEqual(design["plan_sha256"], assessed["plan_sha256"])
        (self.directory / "scope.md").write_text(self.source + "补充说明。", encoding="utf-8")
        reasons, updated = self.review()
        self.assertEqual(reasons, [])
        self.assertNotEqual(updated["plan_sha256"], design["plan_sha256"])

    def test_unverified_or_failed_latest_result_cannot_be_accepted_on_supported_layer(self):
        for state, expected in (("planned", "not_run"), ("unknown", "unknown"), ("failed", "failed")):
            with self.subTest(state=state):
                self.results["TC-002"]["state"] = state
                reasons, summary = self.review()
                self.assertIn("RISK-001/SC-002", reasons[0])
                self.assertEqual(summary["risks"]["RISK-001"]["scenarios"]["SC-002"]["status"], expected)

        self.risk["scenarios"][1]["acceptance"] = self.decision(
            "accept.json", "scenario:RISK-001/SC-002", "accepted",
        )
        reasons, summary = self.review()
        self.assertIn("须用实际用例核验", reasons[0])
        self.assertEqual(summary["status"], "invalid")

    def test_same_scenario_requires_every_associated_case_to_pass(self):
        self.cases["TC-003"] = {**self.cases["TC-002"], "id": "TC-003"}
        for state, expected in ((None, "not_run"), ("planned", "not_run"), ("blocked", "blocked"),
                                ("skipped", "skipped"), ("passed", "verified")):
            with self.subTest(state=state):
                if state is not None:
                    self.results["TC-003"] = {"state": state}
                reasons, summary = self.review()
                risk = summary["risks"]["RISK-001"]
                self.assertEqual(risk["scenarios"]["SC-002"]["status"], expected)
                self.assertEqual(risk["scenarios"]["SC-002"]["case_ids"], ["TC-002", "TC-003"])
                self.assertEqual(bool(reasons), state != "passed")
                self.assertEqual(risk["status"], "verified" if state == "passed" else "open")

    def test_malformed_unhashable_result_or_scenario_fails_safely(self):
        self.results["TC-002"]["state"] = ["passed"]
        reasons, summary = self.review()
        self.assertIn("最新结果状态无效", reasons[0])
        self.assertEqual(summary["status"], "invalid")

        self.cases["TC-002"]["layer"] = {"unsupported": []}
        self.results = None
        reasons, summary = self.review()
        self.assertIn("执行层或验收点格式无效", reasons[0])
        self.assertEqual(summary["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
