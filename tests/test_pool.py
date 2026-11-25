import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier


CLI = Path(__file__).resolve().parents[1] / "scripts" / "qa_pool.py"
ORIGIN = "https://qa.example.test"


class TestFixturePool(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.profile = self.root / "trusted" / "project.json"
        self.profile.parent.mkdir()
        self.config = {
            "project": "mock", "url": ORIGIN, "environment": "test", "tenant": "qa-tenant",
            "authorization": "approval-qa", "external_effects": "disabled",
            "actions": ["read", "update", "release"],
            "fixtures": [
                {"id": "DATA-001", "reference": "pool-item-a", "ownership": "verified",
                 "actions": ["read", "release"]},
                {"id": "DATA-002", "reference": "pool-item-b", "ownership": "verified",
                 "actions": ["update", "release"]},
            ],
        }
        self.write_json(self.profile, self.config)
        self.first = self.new_run("run-a")
        self.second = self.new_run("run-b")

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def new_run(self, name, **fields):
        run = self.root / name
        run.mkdir()
        self.write_json(run / "run.json", {
            "project": "mock", "run_id": name, "url": ORIGIN + ":443/demo#/items", **fields,
        })
        return run

    def call(self, operation, run, *options, command="reserve", tenant="qa-tenant", profile=None):
        return subprocess.run(
            [sys.executable, "-B", str(CLI), command, "--dir", str(run),
             "--profile", str(profile or self.profile), "--tenant", tenant, "--operation", operation,
             *options],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=15,
        )

    def receipt(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def rejected(self, result, reason):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(reason, result.stderr)

    def release(self, run, receipt):
        self.write_json(run / "reserved.json", receipt)
        return self.call("update", run, "--receipt", "reserved.json", command="release")

    def test_selects_authorized_fixture_and_retries_same_lease(self):
        self.rejected(self.call("update", self.first, "--fixture", "DATA-001"), "没有授权")
        first = self.receipt(self.call("update", self.first))
        self.assertEqual(first, {
            "source": "pool", "fixture_id": "DATA-002", "tenant": "qa-tenant",
            "reference": "pool-item-b", "run_id": "run-a", "operation": "update",
            "lease_id": first["lease_id"], "released": False,
        })
        self.assertRegex(first["lease_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(first, self.receipt(self.call("update", self.first)))
        self.config["fixtures"].append({
            "id": "DATA-003", "reference": "pool-item-c", "ownership": "verified",
            "actions": ["update", "release"],
        })
        self.write_json(self.profile, self.config)
        fallback = self.receipt(self.call("update", self.second))
        self.assertEqual(fallback["fixture_id"], "DATA-003")
        self.receipt(self.release(self.first, first))
        self.assertEqual(self.receipt(self.call("update", self.second)), fallback)

    def test_competing_processes_cannot_reserve_same_fixture(self):
        ready = Barrier(2)

        def compete(run):
            ready.wait(timeout=5)
            return self.call("update", run)

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(compete, (self.first, self.second)))
        winner, loser = sorted(results, key=lambda result: result.returncode)
        self.assertFalse(self.receipt(winner)["released"])
        self.rejected(loser, "均已预留")

    def test_release_requires_owner_and_cannot_close_new_lease(self):
        original = self.receipt(self.call("update", self.first))
        self.rejected(self.release(self.second, original), "归属不符")
        copied = self.new_run("copied", run_id="run-a")
        self.rejected(self.release(copied, original), "归属不符")
        self.rejected(self.release(self.first, {**original, "reference": "other-item"}), "归属不符")
        self.rejected(self.call("update", self.second), "均已预留")
        closed = self.receipt(self.release(self.first, original))
        self.assertEqual(closed, {**original, "released": True})
        fresh = self.receipt(self.call("update", self.second))
        self.assertNotEqual(fresh["lease_id"], original["lease_id"])
        self.assertEqual(self.receipt(self.release(self.first, original)), closed)
        self.rejected(self.call("update", self.first), "均已预留")
        self.assertTrue(self.receipt(self.release(self.second, fresh))["released"])

    def test_rejects_unconfigured_scope_and_unverified_fixture(self):
        outside = self.new_run("other-origin", url="https://qa.example.test:444/demo")
        self.rejected(self.call("update", outside), "未经配置授权")
        outside = self.new_run("other-project", project="unconfigured")
        self.rejected(self.call("update", outside), "未经配置授权")
        self.rejected(self.call("update", self.first, tenant="other-tenant"), "未经配置授权")
        self.rejected(self.call("create", self.first), "未经配置授权")
        untrusted = self.first / "profile.json"
        self.write_json(untrusted, self.config)
        self.rejected(self.call("update", self.first, profile=untrusted), "须来自任务目录之外")
        self.config["authorization"] = ""
        self.write_json(self.profile, self.config)
        self.rejected(self.call("update", self.first), "未经配置授权")
        self.config["authorization"] = "approval-qa"
        self.config["external_effects"] = "test_sink"
        self.write_json(self.profile, self.config)
        self.rejected(self.call("update", self.first), "未经配置授权")
        self.config["test_sink"] = "mock-sink"
        self.config["fixtures"][1]["ownership"] = "unverified"
        self.write_json(self.profile, self.config)
        self.rejected(self.call("update", self.first), "池夹具引用、归属或授权动作无效")
        self.assertFalse((self.profile.parent / ".qa-pool.sqlite3").exists())
        self.config["fixtures"][1]["ownership"] = "verified"
        self.write_json(self.profile, self.config)
        self.assertFalse(self.receipt(self.call("update", self.first))["released"])

    def test_shared_profiles_cannot_relabel_an_active_fixture(self):
        original = self.receipt(self.call("update", self.first))
        other = self.profile.with_name("other.json")
        self.config["project"] = "other"
        self.config["fixtures"][1].update(id="DATA-003", reference="Pool-item-B")
        self.write_json(other, self.config)
        run = self.new_run("other", project="other")
        self.rejected(self.call("update", run, profile=other), "均已预留")
        self.receipt(self.release(self.first, original))
        self.assertEqual(self.receipt(self.call("update", run, profile=other))["fixture_id"], "DATA-003")


if __name__ == "__main__":
    unittest.main()
