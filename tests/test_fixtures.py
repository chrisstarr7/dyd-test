import tempfile
import unittest
from pathlib import Path

from test_qa import URL, acceptance_run, observation, qa, run_qa


class TestFixtureWorkflow(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run = acceptance_run(Path(folder.name))
        self.path = str(self.run)
        self.plan = qa.load_json(self.run / "test-cases.json")
        self.plan["cases"][0].update(operation="create", fixture="DATA-001")
        qa.dump_json(self.run / "test-cases.json", self.plan)
        self.manifest = {
            "boundary": {"environment": "test", "url": URL, "tenant": "qa-sandbox",
                         "authorization": "仅可操作隔离物料", "external_effects": "disabled",
                         "actions": ["create", "update", "delete", "release"], "evidence": "scope.txt"},
            "fixtures": [{"id": "DATA-001", "tenant": "qa-sandbox", "source": "generated",
                          "reference": "隔离物料", "marker": "qa-r1-item", "ownership": "verified",
                          "actions": ["create", "update", "delete"], "cleanup": "delete",
                          "evidence": "ownership.txt"}],
        }
        self.save_manifest()
        for filename in ("scope.txt", "ownership.txt", "before.txt", "after.txt", "cleanup.txt"):
            (self.run / filename).write_text(filename, encoding="utf-8")

    def save_manifest(self):
        qa.dump_json(self.run / "fixtures.json", self.manifest)

    def step(self, command, name="TC-001", tenant="qa-sandbox", state="absent", evidence="before.txt"):
        target = ("--fixture", name) if command == "close-data" else ("--case", name, "--state", state)
        return run_qa(command, "--dir", self.path, "--tenant", tenant, "--evidence", evidence, *target)

    def record(self):
        return run_qa("record", "--dir", self.path, "--case", "TC-001",
                      "--state", "passed", "--result", observation(self.run))

    def test_write_needs_reconciliation_and_verified_cleanup(self):
        self.assertNotEqual(self.record()[0], 0)
        self.assertEqual(self.step("prepare-data")[0], 0)
        self.assertNotEqual(self.record()[0], 0)
        self.assertNotEqual(self.step("reconcile-data", state="present")[0], 0)
        self.assertEqual(self.step("reconcile-data", state="present", evidence="after.txt")[0], 0)
        self.assertEqual(self.record()[0], 0)
        self.assertIn("测试数据未验证清理：DATA-001", qa.assess(self.run)["gate"]["reasons"])
        self.assertNotEqual(self.step("close-data", name="DATA-001")[0], 0)
        self.assertEqual(self.step("close-data", name="DATA-001", evidence="cleanup.txt")[0], 0)
        review = qa.assess(self.run)
        self.assertEqual(review["gate"]["verdict"], "pass")
        self.assertIn("DATA-001：已核验回收", qa.format_report(review))
        (self.run / "cleanup.txt").write_text("edited", encoding="utf-8")
        self.assertEqual(qa.assess(self.run)["gate"]["verdict"], "fail")

    def test_unknown_ownership_or_out_of_scope_write_refused(self):
        self.assertNotEqual(self.step("prepare-data", tenant="other-tenant")[0], 0)
        self.assertNotEqual(self.step("prepare-data", state="present")[0], 0)
        self.manifest["fixtures"][0]["ownership"] = "unverified"
        self.save_manifest()
        self.assertNotEqual(self.step("prepare-data")[0], 0)
        self.manifest["fixtures"][0]["ownership"] = "verified"
        self.manifest["boundary"]["external_effects"] = "unknown"
        self.save_manifest()
        self.assertNotEqual(self.step("prepare-data")[0], 0)
        self.manifest["boundary"]["external_effects"] = "disabled"
        self.manifest["boundary"]["url"] = "http://127.0.0.1:9999/"
        self.save_manifest()
        self.assertNotEqual(self.step("prepare-data")[0], 0)
        self.assertFalse((self.run / "fixture-actions.json").exists())

    def test_pending_write_blocks_repeat_and_dependent_mutation(self):
        self.plan["cases"].append({**self.plan["cases"][0], "id": "TC-002", "operation": "update"})
        qa.dump_json(self.run / "test-cases.json", self.plan)
        self.assertEqual(self.step("prepare-data")[0], 0)
        self.assertNotEqual(self.step("prepare-data")[0], 0)
        self.assertNotEqual(self.step("prepare-data", name="TC-002", state="present")[0], 0)
        run_qa("record", "--dir", self.path, "--case", "TC-001", "--state", "blocked", "--note", "响应超时")
        self.assertTrue(any("写入未闭环" in reason for reason in qa.assess(self.run)["gate"]["reasons"]))
        self.assertNotEqual(self.step("close-data", name="DATA-001", evidence="cleanup.txt")[0], 0)
        self.assertEqual(self.step("reconcile-data", evidence="after.txt")[0], 0)
        self.assertNotEqual(self.step("prepare-data", name="TC-002", state="present")[0], 0)

    def test_unused_pool_reservation_also_requires_release(self):
        self.plan["cases"][0]["operation"] = "read"
        qa.dump_json(self.run / "test-cases.json", self.plan)
        self.manifest["fixtures"][0].update(source="pool", cleanup="release", actions=["release"])
        self.save_manifest()
        self.assertEqual(self.record()[0], 0)
        self.assertEqual(qa.assess(self.run)["gate"]["verdict"], "fail")
        self.assertEqual(self.step("close-data", name="DATA-001", evidence="cleanup.txt")[0], 0)
        self.assertEqual(qa.assess(self.run)["gate"]["verdict"], "pass")

    def test_setup_creates_fixture_without_claiming_prd_coverage(self):
        self.plan["cases"][0]["operation"] = "update"
        qa.dump_json(self.run / "test-cases.json", self.plan)
        self.assertNotEqual(self.step("prepare-data", state="present")[0], 0)
        setup = ("--dir", self.path, "--tenant", "qa-sandbox", "--setup", "DATA-001")
        self.assertEqual(run_qa("prepare-data", *setup, "--state", "absent", "--evidence", "before.txt")[0], 0)
        self.assertNotEqual(self.step("prepare-data", state="present")[0], 0)
        self.assertEqual(run_qa("reconcile-data", *setup, "--state", "present", "--evidence", "after.txt")[0], 0)
        self.assertEqual(self.step("prepare-data", state="present")[0], 0)
        self.assertNotEqual(self.record()[0], 0)
        review = qa.assess(self.run)
        self.assertEqual(review["stats"]["planned_cases"], 1)
        self.assertEqual(review["stats"]["executed"], 0)
        self.assertTrue(any("setup:DATA-001" in entry for entry in review["fixtures"]))

    def test_new_run_requires_classification_but_old_runs_are_readable(self):
        self.plan["cases"][0].pop("operation")
        qa.dump_json(self.run / "test-cases.json", self.plan)
        self.assertNotEqual(self.record()[0], 0)
        self.assertEqual(qa.assess(self.run)["gate"]["verdict"], "fail")
        run = qa.load_json(self.run / "run.json")
        run.pop("fixture_contract")
        qa.dump_json(self.run / "run.json", run)
        self.assertEqual(self.record()[0], 0)
        self.assertEqual(qa.assess(self.run)["gate"]["verdict"], "pass")
