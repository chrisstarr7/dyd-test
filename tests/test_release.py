"""用本地证据验证正式准入与缺陷闭环。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from qa_release import review_admission, review_defects  # noqa: E402


class TestReleaseGate(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.run = {"url": "http://127.0.0.1:8765/", "version": "v1"}
        self.preflight = {
            **self.run, "smoke": "passed", "evidence": "smoke.txt", "release_evidence": "release.json",
            "sql": "verified", "sql_note": "测试库增量脚本已执行",
        }
        self.release = {
            **self.run, "type": "release_record", "source": "CI 发布记录 42", "deployment_id": "deploy-42",
            "scope": "fullstack", "components": {"frontend": "web-24", "backend": "api-42"},
            "migration": {"status": "verified", "basis": "测试库增量脚本已执行", "execution_id": "sql-42"},
        }
        (self.directory / "smoke.txt").write_text("物料列表 | 管理员", encoding="utf-8")
        self.write_record("release.json", self.release)

    def write_record(self, name, record):
        (self.directory / name).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def test_release_admission_accepts_verified_sql_and_single_component_without_sql(self):
        self.assertEqual(review_admission(self.directory, self.run, self.preflight), [])
        basis = "发布记录确认本轮无数据库变更"
        self.release.pop("deployment_id")
        self.release.update(build_id="build-24", migration={"status": "not_applicable", "basis": basis})
        self.preflight.update(sql="not_applicable", sql_note=basis)
        for component in ("frontend", "backend"):
            with self.subTest(component=component):
                self.release.update(scope=component, components={component: "artifact-24"})
                self.write_record("release.json", self.release)
                self.assertEqual(review_admission(self.directory, self.run, self.preflight), [])

    def test_generic_ui_json_cannot_verify_sql(self):
        self.write_record("release.json", {
            **self.run, "smoke": "passed", "sql": "verified", "sql_note": "已核对",
            "snapshot": "页面正常", "assertions": {"AC-001": {"actual": True}},
        })
        reasons = review_admission(self.directory, self.run, self.preflight)
        self.assertTrue(any("增量 SQL" in reason for reason in reasons), reasons)

    def test_release_and_smoke_require_separate_files(self):
        self.preflight["evidence"] = "./release.json"
        self.assertTrue(review_admission(self.directory, self.run, self.preflight))

    def test_release_bindings_and_migration_evidence_must_match(self):
        migration = self.release["migration"]
        for field, invalid in (
            ("url", self.run["url"] + "another-app"), ("version", "v0"), ("deployment_id", "unknown"),
            ("components", {"frontend": "web-24"}),
            ("migration", {**migration, "execution_id": ""}),
            ("migration", {**migration, "status": "not_applicable"}),
            ("migration", {**migration, "basis": ""}),
        ):
            with self.subTest(field=field, invalid=invalid):
                self.write_record("release.json", {**self.release, field: invalid})
                self.assertTrue(review_admission(self.directory, self.run, self.preflight))

    def test_p1_closed_without_passing_linked_retest_blocks_release(self):
        defect = {"id": "BUG-001", "severity": "P1", "status": "closed", "fix_version": "v1"}
        passed = {"state": "passed", "defect_ids": ["BUG-001"]}
        self.assertTrue(review_defects(self.directory, self.run, [defect], {"TC-001": passed}))
        defect["retest_case_id"] = "TC-001"
        for retest in ({}, {**passed, "state": "failed"}, {**passed, "defect_ids": ["BUG-002"]},
                       {**passed, "defect_ids": "BUG-001"}):
            with self.subTest(retest=retest):
                reasons = review_defects(self.directory, self.run, [defect], {"TC-001": retest})
                self.assertTrue(any("通过复测" in reason for reason in reasons), reasons)
        self.assertEqual(review_defects(self.directory, self.run, [defect], {"TC-001": passed}), [])
        defect["fix_version"] = "v0"
        self.assertTrue(review_defects(self.directory, self.run, [defect], {"TC-001": passed}))

    def test_p0_must_be_closed_even_when_fix_and_retest_exist(self):
        defect = {"id": "BUG-001", "severity": "P0", "status": "fixed",
                  "fix_version": "v1", "retest_case_id": "TC-001"}
        results = {"TC-001": {"state": "passed", "defect_ids": ["BUG-001"]}}
        self.assertTrue(review_defects(self.directory, self.run, [defect], results))
        defect["status"] = "closed"
        self.assertEqual(review_defects(self.directory, self.run, [defect], results), [])

    def test_confirmed_root_cause_needs_independent_causal_proof(self):
        from qa_release import root_cause_proof

        run = {**self.run, "run_id": "R1"}
        defect = {"id": "BUG-001", "severity": "P1", "status": "closed", "fix_version": "v1",
                  "retest_case_id": "TC-001"}
        results = {"TC-001": {"state": "passed", "defect_ids": ["BUG-001"]}}
        proof = {"type": "root_cause_confirmation", "source": "授权工程师复核", "by": "reviewer",
                 "reference": "diagnostic-record-1", "finding": "条件对照 isolating a business change",
                 "run_id": "R1", "version": "v1", "defect_id": "BUG-001", "retest_case": "TC-001",
                 "method": "fix_causal", "evidence": "causal.txt"}
        self.write_record("root.json", proof)
        with self.assertRaises(ValueError):
            root_cause_proof(self.directory, run, defect, "root.json")
        artifact = self.directory / "causal.txt"
        artifact.write_text("独立因果比对证据样例", encoding="utf-8")
        defect["investigation"] = {"root_cause": "confirmed",
                                   "proof": root_cause_proof(self.directory, run, defect, "root.json")}
        self.assertEqual(review_defects(self.directory, run, [defect], results), [])
        artifact.write_text("证据已更改", encoding="utf-8")
        self.assertIn("根因确认凭据无法核验：BUG-001", review_defects(self.directory, run, [defect], results))

    def test_p2_waiver_requires_structured_approval_for_same_defect_and_version(self):
        waiver = {"version": "v1", "by": "产品负责人", "reason": "已接受筛选项延后修复",
                  "reference": "waiver.json"}
        defect = {"id": "BUG-002", "severity": "P2", "status": "new", "waiver": waiver}
        self.assertTrue(review_defects(self.directory, self.run, [defect], {}))
        self.write_record("waiver.json", waiver)
        self.assertTrue(review_defects(self.directory, self.run, [defect], {}))
        record = {key: waiver[key] for key in ("version", "by", "reason")}
        record = {**record, "type": "defect_waiver", "source": "产品审批 12",
                  "defect_id": "BUG-002", "decision": "accepted"}
        for field, invalid in (("defect_id", "BUG-001"), ("version", "v0"), ("decision", "rejected"),
                               ("by", "其他人员"), ("reason", "不同风险")):
            with self.subTest(field=field):
                self.write_record("waiver.json", {**record, field: invalid})
                self.assertTrue(review_defects(self.directory, self.run, [defect], {}))
        self.write_record("waiver.json", record)
        self.assertEqual(review_defects(self.directory, self.run, [defect], {}), [])
        defect.update(status="closed", fix_version="v1")
        self.assertTrue(review_defects(self.directory, self.run, [defect], {}))


if __name__ == "__main__":
    unittest.main()
