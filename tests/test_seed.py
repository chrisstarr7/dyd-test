import copy
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from qa_seed import SEED_DB, SeedError, open_seed
from qa_fixtures import fixture_binding, fixture_cases, load_context


ORIGIN = "https://qa.example.test"
TENANT = "qa-tenant"


class TestSeed(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.trusted = self.root / "trusted"
        self.trusted.mkdir()
        self.profile = self.trusted / "seed.json"
        self.spec = {
            "seed_contract": 1, "project": "project-qa", "url": ORIGIN, "environment": "test",
            "tenant": TENANT, "authorization": "approval-qa", "external_effects": "disabled",
            "actions": ["read", "create", "update", "delete"], "namespace": "qa-west",
            "recipes": {
                "account": self.recipe("name", {
                    "name": {"kind": "name", "prefix": "测试账号"}, "password": {"kind": "secret"},
                    "email": {"kind": "email", "domain": "accounts.invalid"},
                }),
                "order": self.recipe("number", {
                    "number": {"kind": "identifier", "prefix": "order"},
                    "quantity": {"kind": "integer", "min": 4, "max": 9},
                    "variant": {"kind": "choice", "values": ["red", "blue"]},
                }),
                "config": self.recipe("label", {
                    "label": {"kind": "marker"}, "mode": {"kind": "choice", "values": ["draft"]},
                }),
            },
        }
        self.run = self.new_run("trial-a")
        self.write(self.profile, self.spec)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def recipe(reference, fields):
        return {"actions": ["create", "update", "delete"], "reference_field": reference, "fields": fields}

    def new_run(self, run_id, **overrides):
        directory = self.root / run_id
        directory.mkdir()
        self.write(directory / "run.json", {"project": "project-qa", "run_id": run_id,
                                            "url": ORIGIN + ":443/app#/test", **overrides})
        return directory

    def use(self, recipe="account", *, run=None, profile=None, operation="create", fixture="DATA-001", tenant=TENANT,
            credential_key=None):
        return open_seed(run or self.run, profile or self.profile, tenant, recipe, fixture, operation,
                         credential_key=credential_key)

    def call(self, command, recipe="account", *, run=None, fixture="DATA-001", operation="create"):
        result = subprocess.run([
            sys.executable, "-B", str(SCRIPTS / "qa_seed.py"), command,
            "--dir", str(run or self.run), "--profile", str(self.profile), "--tenant", TENANT,
            "--recipe", recipe, "--fixture", fixture, "--operation", operation,
        ], capture_output=True, check=False, encoding="utf-8", timeout=15)
        self.assertEqual(result.stderr, "")
        return result.returncode, json.loads(result.stdout)

    def test_cli_plan_and_prepare_return_redacted_evidence_only(self):
        code, plan = self.call("plan")
        self.assertEqual((code, plan["status"], plan["evidence"]), (0, "configured", "profile_only"))
        self.assertFalse((self.trusted / SEED_DB).exists())
        self.assertNotIn("fixture", plan)

        code, receipt = self.call("prepare")
        self.assertEqual((code, receipt["status"], receipt["evidence"]), (0, "reserved", "local_reservation_only"))
        self.assertEqual(receipt["boundary"]["authorization"], "approval-qa")
        self.assertEqual(receipt["fixture"]["source"], "generated")
        self.assertEqual(receipt["fixture"]["cleanup"], "delete")
        self.assertIn("trial-a", receipt["fixture"]["marker"])
        self.assertNotIn("ownership", receipt["fixture"])
        self.assertNotRegex(json.dumps(receipt), r"password|email|accounts\.invalid")
        self.assertEqual(self.call("prepare"), (0, receipt))
        self.assertEqual(list(self.run.iterdir()), [self.run / "run.json"])

    def test_account_secret_is_ephemeral_and_ordinary_values_are_stable(self):
        with self.use() as (receipt, values):
            secret = values["password"]
            email = values["email"]
            self.assertEqual(values["name"], receipt["fixture"]["reference"])
            self.assertRegex(email, r"^qa-qa-west-\d+-\d+@accounts\.invalid$")
            self.assertGreaterEqual(len(secret), 32)
            for pattern in (r"[A-Z]", r"[a-z]", r"[0-9]", r"[^A-Za-z0-9]"):
                self.assertRegex(secret, pattern)
            self.assertNotIn(secret, json.dumps(receipt))
        self.assertEqual(values, {})

        self.spec["recipes"]["account"]["fields"] = dict(reversed(list(self.spec["recipes"]["account"]["fields"].items())))
        self.write(self.profile, self.spec)
        with self.use() as (again, retry):
            self.assertEqual(again, receipt)
            self.assertEqual(retry["email"], email)
            self.assertNotEqual(retry["password"], secret)
        self.assertNotIn(secret.encode(), (self.trusted / SEED_DB).read_bytes())
        self.assertNotIn(email.encode(), (self.trusted / SEED_DB).read_bytes())

    def test_generic_order_and_config_use_distinct_slots_and_profile_values(self):
        references = []
        for index in (1, 2):
            with self.use("order", fixture=f"DATA-00{index}", operation="update") as (receipt, values):
                references.append(values["number"])
                self.assertEqual(values["number"], receipt["fixture"]["reference"])
                self.assertIn(values["quantity"], range(4, 10))
                self.assertIn(values["variant"], {"red", "blue"})
        self.assertEqual(len(set(references)), 2)
        with self.use("config", fixture="DATA-003") as (receipt, values):
            self.assertEqual(values["label"], receipt["fixture"]["marker"])
            self.assertEqual(values["mode"], "draft")

        changed = copy.deepcopy(self.spec)
        changed["recipes"]["order"]["fields"]["quantity"]["min"] = 5
        self.write(self.profile, changed)
        with self.assertRaises(SeedError) as failure, self.use("order", operation="update"):
            self.fail("配置变更后仍重用了旧预留")
        self.assertEqual(failure.exception.code, "reservation_conflict")

    def test_authorized_credential_key_recreates_private_value_in_the_same_slot(self):
        with self.assertRaises(SeedError) as failure, self.use(credential_key=b"too short"):
            self.fail("接受了无效密钥")
        self.assertEqual(failure.exception.code, "credential_key_invalid")
        self.assertFalse((self.trusted / SEED_DB).exists())

        key = b"a bounded test-only credential key"
        with self.use(credential_key=key) as (receipt, values):
            password = values["password"]
            self.assertNotIn(password, json.dumps(receipt))
        with self.use(credential_key=key) as (_, values):
            self.assertEqual(values["password"], password)
        with self.use(credential_key=key, fixture="DATA-002") as (_, values):
            self.assertNotEqual(values["password"], password)
        state = (self.trusted / SEED_DB).read_bytes()
        self.assertNotIn(key, state)
        self.assertNotIn(password.encode(), state)

    def test_receipt_can_bind_to_existing_generated_fixture_flow(self):
        run = self.new_run("20261125-161300-qa")
        code, receipt = self.call("prepare", "order", run=run)
        self.assertEqual(code, 0)
        self.write(run / "scope.json", {"authorization": "approved test scope"})
        self.write(run / "reserved.json", receipt)
        self.write(run / "fixtures.json", {
            "boundary": dict(receipt["boundary"], evidence="scope.json"),
            "fixtures": [dict(receipt["fixture"], evidence="reserved.json", ownership="verified", page="PAGE-001")],
        })
        context = load_context(run)
        run_data = json.loads((run / "run.json").read_text(encoding="utf-8"))
        self.assertRegex(fixture_binding(run, run_data, context, "DATA-001", TENANT, "create"), r"^[a-f0-9]{64}$")
        self.assertEqual(fixture_cases({}, context[1])["setup:DATA-001"]["operation"], "create")

    def test_rejects_mismatched_scope_and_untrusted_instruction_shapes(self):
        for index, override in enumerate((
            {"project": "elsewhere"}, {"tenant": "elsewhere"}, {"environment": "production"},
            {"url": "https://qa.example.test:404"}, {"url": "https://secret@qa.example.test"},
            {"url": "https://qa.example.test:0"}, {"url": []},
        )):
            with self.subTest(override=override):
                run = self.new_run(f"mismatch-{index}", **override)
                with self.assertRaises(SeedError) as failure, self.use(run=run):
                    self.fail("未拒绝越界运行")
                self.assertEqual(failure.exception.code, "scope_denied")

        bad_run_profile = self.run / "seed.json"
        self.write(bad_run_profile, self.spec)
        with self.assertRaises(SeedError) as failure, self.use(profile=bad_run_profile):
            self.fail("在本轮目录内接受了授权配置")
        self.assertEqual(failure.exception.code, "untrusted_profile")

        code, result = self.call("prepare", operation="read")
        self.assertEqual((code, result["code"]), (1, "operation_denied"))
        for override in ({"external_effects": "live"}, {"url": ORIGIN + "/execute"},
                         {"external_effects": "test_sink"}, {"actions": ["create"]},
                         {"actions": ["create", "delete", {}]}, {"command": "PRIVATE_COMMAND"}):
            with self.subTest(override=override):
                self.write(self.profile, {**self.spec, **override})
                code, result = self.call("prepare")
                self.assertEqual((code, result["status"]), (1, "denied"))
                self.assertNotIn("PRIVATE_COMMAND", json.dumps(result))
        self.assertFalse((self.trusted / SEED_DB).exists())

    def test_denies_arbitrary_email_generation_and_requires_matching_sandbox_sink(self):
        fields = self.spec["recipes"]["account"]["fields"]
        fields["email"]["domain"] = "uncontrolled.example.com"
        self.write(self.profile, self.spec)
        self.assertEqual(self.call("prepare")[1]["code"], "invalid_profile")

        fields["email"] = {"kind": "sandbox_email", "mailbox": "qa@example.test", "sink": "mail-sandbox"}
        self.spec.update(external_effects="test_sink", test_sink="another-sandbox")
        self.write(self.profile, self.spec)
        self.assertEqual(self.call("prepare")[1]["code"], "invalid_profile")
        self.spec["test_sink"] = "mail-sandbox"
        self.write(self.profile, self.spec)
        with self.use() as (receipt, values):
            self.assertRegex(values["email"], r"^qa\+qa-qa-west-\d+-\d+@example\.test$")
            self.assertEqual(receipt["boundary"]["test_sink"], "mail-sandbox")
            self.assertNotIn("@example.test", json.dumps(receipt))

    def test_concurrent_runs_cannot_share_a_phone_across_profiles_or_tenants(self):
        phone = "+12025550101"
        self.spec["recipes"]["account"]["fields"]["mobile"] = {"kind": "phone_pool", "values": [phone]}
        self.write(self.profile, self.spec)
        other_profile = self.trusted / "other.json"
        other_tenant = "other-tenant"
        other_spec = copy.deepcopy(self.spec)
        other_spec.update(tenant=other_tenant, namespace="qa-east")
        other_spec["recipes"]["account"]["fields"]["mobile"] = {"kind": "phone_range", "start": phone, "end": phone}
        self.write(other_profile, other_spec)
        other_run = self.new_run("trial-b", tenant=other_tenant)
        ready = Barrier(2)

        def reserve(run, profile, tenant):
            ready.wait(timeout=5)
            try:
                with self.use(run=run, profile=profile, tenant=tenant) as (receipt, values):
                    self.assertNotIn(phone, json.dumps(receipt))
                    return values["mobile"]
            except SeedError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(reserve, self.run, self.profile, TENANT)
            second = workers.submit(reserve, other_run, other_profile, other_tenant)
            self.assertCountEqual((first.result(), second.result()), (phone, "identity_exhausted"))
        self.assertNotIn(phone.encode(), (self.trusted / SEED_DB).read_bytes())

    def test_exhaustion_rolls_back_partial_reservations_and_leaves_authorized_fallback(self):
        fields = self.spec["recipes"]["account"]["fields"]
        fields["a_mail"] = {"kind": "email_pool", "values": ["approved@example.test"]}
        fields["z_phone"] = {"kind": "phone_pool", "values": ["+12025550101"]}
        self.spec["recipes"]["phone_only"] = self.recipe("label", {
            "label": {"kind": "marker"}, "phone": fields["z_phone"],
        })
        self.spec["recipes"]["mail_only"] = self.recipe("label", {
            "label": {"kind": "marker"}, "email": fields["a_mail"],
        })
        self.write(self.profile, self.spec)
        other_run = self.new_run("trial-b")
        self.assertEqual(self.call("prepare", "phone_only")[0], 0)
        code, result = self.call("prepare", run=other_run)
        self.assertEqual((code, result["status"], result["code"], result["evidence"]),
                         (2, "blocked", "identity_exhausted", "none"))
        self.assertEqual(self.call("prepare", "config", run=other_run)[0], 0)
        self.assertEqual(self.call("prepare", "mail_only", run=other_run, fixture="DATA-002")[0], 0)
        self.assertEqual(self.call("prepare", "nonexistent")[1]["code"], "recipe_unavailable")


if __name__ == "__main__":
    unittest.main()
