import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from test_qa import qa, run_qa
import dashboard
import qa_benchmark
import qa_experience
import qa_impact
import qa_performance


class Service(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        status = 200
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/item/demo")
            self.end_headers()
            return
        if self.path == "/invalid":
            self.send_response(200)
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"<html>")
            return
        if self.path == "/slow":
            time.sleep(0.25)
        if self.path.startswith("/fixtures/"):
            with self.server.lock, closing(sqlite3.connect(self.server.database)) as db, db:
                count = db.execute("SELECT count(*) FROM items WHERE name = ?", (self.path.rsplit("/", 1)[1],)).fetchone()[0]
            status, body = (200, {"present": True}) if count else (404, {})
        elif self.path.startswith("/find?"):
            body = {"id": "demo"}
        elif self.path == "/item/demo":
            body = {"ok": self.server.good, "amount": 21 if self.server.good else "bad",
                    "secret": "never retain this"}
            if self.headers.get("Authorization") != "Bearer fixture-credential" and self.server.mode != "permission":
                status, body = 403, {"ok": False}
        else:
            body = {}
        self.send_object(status, body)

    def send_object(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def do_POST(self):
        name = self.path.rsplit("/", 1)[1]
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.server.mode == "accepted":
            self.send_object(202, {"ok": True})
            return
        if self.server.mode == "timeout":
            time.sleep(0.3)
        if self.server.mode != "noop":
            with self.server.lock, closing(sqlite3.connect(self.server.database)) as db, db:
                exists = db.execute("SELECT 1 FROM items WHERE name = ?", (name,)).fetchone()
                if self.server.mode == "duplicate" or not exists:
                    db.execute("INSERT INTO items VALUES (?)", (name,))
        self.send_object(200, {"ok": True})

    def do_DELETE(self):
        name = self.path.rsplit("/", 1)[1]
        if self.server.mode != "cleanup-failed":
            with self.server.lock, closing(sqlite3.connect(self.server.database)) as db, db:
                db.execute("DELETE FROM items WHERE name = ?", (name,))
        self.send_object(200, {"ok": True})


class TestHttp(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.server = server
        self.server.requests = []
        self.server.good = True
        self.server.mode = "clean"
        self.server.lock = threading.Lock()
        self.origin = f"http://127.0.0.1:{server.server_port}/"
        root = Path(folder.name)
        self.directory = root / "demo" / "R1"
        self.assertEqual(run_qa("init", "--dir", str(root), "--project", "demo", "--run-id", "R1",
                                "--url", self.origin, "--version", "v1", "--mode", "targeted")[0], 0)
        self.rule = "按已知夹具核对定价结果与类型"
        self.case = {
            "id": "TC-001", "req": "REQ-001", "page": "API-001", "operation": "read", "layer": "api",
            "title": "测试物料两步读取与独立金额校验",
            "scenario": "normal", "steps": ["按名称查询夹具", "按标识读取并比对业务数据"],
            "http": [{"method": "GET", "path": "/find", "query": {"name": "测试 物料"}, "extract": {"item": "/id"}},
                     {"method": "GET", "path": "/item/{item}", "auth": {"Authorization": "DYD_QA_FIXTURE"}}],
            "checks": {"AC-001": {"step": 1, "read": "status"},
                       "AC-002": {"step": 1, "read": "field", "path": "/ok"},
                       "AC-003": {"step": 1, "read": "schema", "types": {"/amount": "integer"}},
                       "AC-004": {"step": 1, "read": "field", "path": "/amount"}},
            "expected": {"AC-001": 200, "AC-002": True, "AC-003": True, "AC-004": 21},
        }
        (self.directory / "prd.md").write_text(self.rule, encoding="utf-8")
        self.write("pages.json", [{"id": "API-001", "layer": "api", "status": "confirmed", "entry": "定价接口",
                                  "role": "fixture", "tenant": "isolated", "url": self.origin}])
        self.write("prd-scope.json", {"document": "prd.md", "revision": "v1", "sections": [
            {"from": 1, "to": 1, "status": "in_scope", "requirements": ["REQ-001"]}]})
        self.write("risk-review.json", {
            "schema_version": 1, "run_id": "R1", "version": "v1", "source": "prd.md",
            "dimensions": {"data": {"status": "in_scope"}},
            "risks": [{"id": "RISK-001", "dimension": "data", "severity": "high", "source": self.rule,
                       "invariant": self.rule, "impact": "金额不符合约定", "scenarios": [
                           {"id": "SCN-001", "scenario": "normal", "layer": "api", "criterion": "AC-001"}]}],
        })

    def write(self, name, value):
        qa.dump_json(self.directory / name, value)

    def cli(self, command, *args):
        return run_qa(command, "--dir", str(self.directory), *args)

    def freeze(self, upstream=None):
        self.write("test-cases.json", {"cases": ([upstream] if upstream else []) + [self.case]})
        self.write("requirements.json", [{"id": "REQ-001", "source": "prd.md:1", "criteria": [
            {"id": key, "page": "API-001", "layer": "api", "rule": self.rule,
             "risk": "medium", "scenarios": [self.case["scenario"]]} for key in self.case["expected"]]}])
        self.assertEqual(self.cli("freeze-plan")[0], 0)

    def execute(self, *, output="evidence/result.json", credential="Bearer fixture-credential", origin=None, flags=()):
        with patch.dict(os.environ, {"DYD_QA_FIXTURE": credential}):
            return self.cli("api", "--case", "TC-001", "--output", output, "--allow-origin", origin or self.origin, *flags)

    def test_http_chain_and_report(self):
        self.freeze()
        self.assertEqual(self.execute()[0], 0)
        self.assertIn("%E6%B5%8B%E8%AF%95+", self.server.requests[0])
        self.assertEqual(self.server.requests[1], "/item/demo")
        evidence = (self.directory / "evidence/result.json").read_text(encoding="utf-8")
        self.assertNotIn("fixture-credential", evidence)
        self.assertNotIn("never retain", evidence)
        self.assertEqual(qa.assess(self.directory)["gate"]["test_outcome"], "passed")
        self.assertEqual(self.cli("report")[0], 0)
        self.assertIn("HTTP/API", dashboard.build_html(self.directory))
        self.case["expected"]["AC-004"] = 22
        self.write("test-cases.json", {"cases": [self.case]})
        self.assertEqual(qa.assess(self.directory)["gate"]["test_outcome"], "incomplete")

    def test_real_200_business_and_schema_failure(self):
        self.server.good = False
        self.freeze()
        self.assertEqual(self.execute()[0], 1)
        result = qa.assess(self.directory)["results"]["TC-001"]
        self.assertEqual(result["state"], "failed")
        assertions = result["observation"]["assertions"]
        self.assertEqual(assertions["AC-001"]["actual"], 200)
        self.assertIs(assertions["AC-002"]["actual"], False)
        self.assertIs(assertions["AC-003"]["actual"], False)
        self.assertEqual(self.cli("defect", "--id", "DEF-001", "--title", "定价不匹配",
                                  "--severity", "P1", "--case", "TC-001", "--impact", "金额错误",
                                  "--hypothesis", "待查定价接入")[0], 0)
        self.assertEqual(self.cli("report")[0], 0)
        self.assertIn("DEF-001", dashboard.build_html(self.directory))
        self.assertIn("根因疑似", dashboard.build_html(self.directory))
        artifact_root = os.environ.get("DYD_QA_TEST_ARTIFACTS")
        if artifact_root:
            (self.directory / "dashboard.html").write_text(dashboard.build_html(self.directory), encoding="utf-8")
            destination = Path(artifact_root) / "http-prd-defect"
            shutil.copytree(self.directory, destination)
            print(f"本机验收产物：{destination}")

    def test_timeout_and_missing_identity_are_blocked(self):
        self.case["http"][0] = {"method": "GET", "path": "/slow", "timeout": 0.05}
        self.freeze()
        self.assertEqual(self.execute(credential="")[0], 2)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.execute(output="slow.json")[0], 2)
        self.assertEqual(qa.load_json(self.directory / "slow.json")["error"], "HTTP 请求超时")
        self.assertEqual(qa.assess(self.directory)["gate"]["test_outcome"], "incomplete")

    def test_scope_checked_and_redirect_not_followed(self):
        self.case["http"][0] = {"method": "GET", "path": "/redirect"}
        self.case["http"].pop()
        self.case["checks"] = {"AC-001": {"step": 0, "read": "status"}}
        self.case["expected"] = {"AC-001": 302}
        self.freeze()
        self.assertNotEqual(self.execute(origin="http://127.0.0.1:1/")[0], 0)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.execute()[0], 0)
        self.assertEqual(self.server.requests, ["/redirect"])

    def test_unauthorized_role_does_not_yield_business_data(self):
        self.case["checks"] = {"AC-001": {"step": 1, "read": "status"},
                               "AC-002": {"step": 1, "read": "exists", "path": "/amount"}}
        self.case["expected"] = {"AC-001": 403, "AC-002": False}
        self.freeze()
        self.assertEqual(self.execute(credential="Bearer denied")[0], 0)
        self.server.mode = "permission"
        self.assertEqual(self.execute(output="permission.json", credential="Bearer denied")[0], 1)
        result = qa.assess(self.directory)["results"]["TC-001"]["observation"]["assertions"]
        self.assertEqual(result["AC-001"]["actual"], 200)
        self.assertIs(result["AC-002"]["actual"], True)

    def test_unregistered_and_tampered_captures_do_not_pass(self):
        self.freeze()
        self.write("fake.json", {"source": "http.client", "observed": self.case["expected"]})
        self.assertNotEqual(self.cli("record", "--case", "TC-001", "--state", "passed", "--result", "fake.json")[0], 0)
        self.assertEqual(self.execute()[0], 0)
        result = qa.load_json(self.directory / "evidence/result.json")
        result["observed"]["AC-001"] = 403
        self.write("evidence/result.json", result)
        self.assertEqual(qa.assess(self.directory)["results"]["TC-001"]["state"], "unknown")

    def local_fixture(self, mode, parallel=False, upstream=None):
        self.server.mode = mode
        self.server.database = self.directory / "fixtures.sqlite"
        with closing(sqlite3.connect(self.server.database)) as db, db:
            db.execute("CREATE TABLE items (name TEXT)")
        self.case.update(operation="create", local_fixture={"path": "/fixtures/{fixture}"},
                         http=[{"method": "POST", "path": "/fixtures/{fixture}", "json": {"name": "{fixture}"}}],
                         checks={"AC-001": {"step": 0, "read": "field", "path": "/ok"},
                                 "AC-002": {"read": "sqlite_count", "file": "fixtures.sqlite",
                                            "table": "items", "key": "name", "value": "{fixture}"}},
                         expected={"AC-001": True, "AC-002": 1})
        if parallel:
            self.case["http"][0]["parallel"] = 3
            self.case["checks"]["AC-001"] = {"step": 0, "read": "statuses"}
            self.case["expected"]["AC-001"] = [200, 200, 200]
        if mode == "timeout":
            self.case["http"][0]["timeout"] = 0.05
        self.freeze(upstream)

    def test_successful_http_reply_cannot_hide_missing_persistence(self):
        self.local_fixture("noop")
        self.assertEqual(self.execute(flags=("--allow-local-write",))[0], 1)
        result = qa.assess(self.directory)["results"]["TC-001"]
        self.assertIs(result["observation"]["assertions"]["AC-001"]["actual"], True)
        self.assertEqual(result["observation"]["assertions"]["AC-002"]["actual"], 0)

    def test_concurrent_deduplicated_fixture_passes_and_is_cleaned(self):
        self.local_fixture("clean", parallel=True)
        self.assertNotEqual(self.execute()[0], 0)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.execute(flags=("--allow-local-write",))[0], 0)
        review = qa.assess(self.directory)
        self.assertEqual(review["gate"]["test_outcome"], "passed")
        self.assertIn("已回收", review["fixtures"][0])
        self.assertNotEqual(self.execute(output="again.json", flags=("--allow-local-write",))[0], 0)

    def test_parallel_duplicate_detected_even_when_all_http_replies_succeed(self):
        self.local_fixture("duplicate", parallel=True)
        self.assertEqual(self.execute(flags=("--allow-local-write",))[0], 1)
        result = qa.assess(self.directory)["results"]["TC-001"]
        self.assertEqual(result["observation"]["assertions"]["AC-001"]["actual"], [200, 200, 200])
        self.assertEqual(result["observation"]["assertions"]["AC-002"]["actual"], 3)

    def test_cleanup_failure_and_uncertain_write_remain_blocked(self):
        self.local_fixture("cleanup-failed")
        self.assertEqual(self.execute(flags=("--allow-local-write",))[0], 2)
        review = qa.assess(self.directory)
        self.assertEqual(review["gate"]["test_outcome"], "incomplete")
        self.assertTrue(any("API 测试夹具未核验回收" in reason for reason in review["gate"]["test_reasons"]))
        self.server.mode = "clean"
        self.assertEqual(self.cli("api", "--case", "TC-001", "--allow-origin", self.origin,
                                  "--allow-local-write", "--recover", "隔离服务已确认无在途写入")[0], 0)
        review = qa.assess(self.directory)
        self.assertEqual(review["gate"]["test_outcome"], "incomplete")
        self.assertIn("已回收", review["fixtures"][0])

    def test_timeout_does_not_replay_write_or_assume_cleanup(self):
        self.local_fixture("timeout")
        self.assertEqual(self.execute(flags=("--allow-local-write",))[0], 2)
        self.assertIsNone(qa.load_json(self.directory / "evidence/result.json")["cleanup"])
        self.assertEqual(len(list((self.directory / ".api-intents").glob("*.json"))), 1)

    def test_missing_sqlite_blocks_before_any_write(self):
        self.local_fixture("clean")
        self.server.database.unlink()
        self.assertEqual(self.execute(flags=("--allow-local-write",))[0], 2)
        self.assertEqual(list((self.directory / ".api-intents").glob("*.json")), [])

    def test_accepted_write_and_competing_executor_cannot_certify_cleanup(self):
        self.local_fixture("accepted")
        with qa.qa_api.run_lock(self.directory):
            self.assertEqual(self.execute(flags=("--allow-local-write",))[0], 2)
            self.assertEqual(self.server.requests, [])
        self.assertEqual(self.execute(flags=("--allow-local-write",))[0], 2)
        result = qa.load_json(self.directory / "evidence/result.json")
        self.assertIn("同步终态", result["error"])
        self.assertIsNone(result["cleanup"])

    def test_invalid_json_does_not_prove_field_absence(self):
        self.case.update(http=[{"method": "GET", "path": "/invalid"}],
                         checks={"AC-001": {"step": 0, "read": "exists", "path": "/id"}},
                         expected={"AC-001": False})
        self.freeze()
        self.assertEqual(self.execute()[0], 2)
        self.assertIn("无有效 JSON", qa.load_json(self.directory / "evidence/result.json")["error"])

    def test_api_write_dependency_is_rechecked_from_api_evidence(self):
        upstream = dict(self.case, id="TC-002", http=[{"method": "GET", "path": "/find"}],
                        expected={"AC-001": 200}, checks={"AC-001": {"step": 0, "read": "status"}})
        self.case["depends_on"] = ["TC-002"]
        self.local_fixture("clean", upstream=upstream)
        self.assertEqual(self.cli("api", "--case", "TC-002", "--output", "upstream.json",
                                  "--allow-origin", self.origin)[0], 0)
        self.assertEqual(self.execute(flags=("--allow-local-write",))[0], 0)
        self.assertEqual(qa.assess(self.directory)["gate"]["test_outcome"], "passed")

    def test_benchmark_scores_verified_pass_failure_and_false_pass(self):
        self.freeze()
        plan = qa.load_json(self.directory / "test-cases.json")
        rules = qa.load_json(self.directory / "requirements.json")
        rules[0]["criteria"].append(dict(rules[0]["criteria"][0], id="AC-005"))
        plan["cases"].append(dict(self.case, id="TC-002", expected={"AC-005": 200},
                                  checks={"AC-005": {"step": 0, "read": "status"}},
                                  http=[{"method": "GET", "path": "/not-found"}]))
        truth = {"cohort": "hidden-fixture", "cases": {
            "TC-001": {"defective": True, "critical": True, "risk": "data"},
            "TC-002": {"defective": False, "risk": "routing"},
        }}
        runs = []
        for label, good in (("baseline", False), ("candidate", True)):
            root = self.directory.parents[1]
            self.assertEqual(run_qa("init", "--dir", str(root), "--project", "demo", "--run-id", label,
                                    "--url", self.origin, "--version", "v1", "--mode", "targeted")[0], 0)
            path = root / "demo" / label
            for name in ("pages.json", "prd-scope.json", "prd.md"):
                (path / name).write_bytes((self.directory / name).read_bytes())
            qa.dump_json(path / "requirements.json", rules)
            qa.dump_json(path / "test-cases.json", plan)
            self.assertEqual(run_qa("freeze-plan", "--dir", str(path))[0], 0)
            self.server.good = good
            with patch.dict(os.environ, {"DYD_QA_FIXTURE": "Bearer fixture-credential"}):
                for case_id in ("TC-001", "TC-002"):
                    status, message = run_qa("api", "--dir", str(path), "--case", case_id,
                                             "--allow-origin", self.origin, "--output", case_id + ".json")
                    self.assertEqual(status, int(case_id == "TC-001" and not good), message)
            if not good:
                self.assertEqual(run_qa("defect", "--dir", str(path), "--id", "DEF-001", "--title",
                                        "独立定价差异", "--severity", "P1", "--case", "TC-001")[0], 0)
            runs.append(path)
        compare = qa_benchmark.compare(truth, *runs)
        self.assertEqual(compare["status"], "failed")
        self.assertEqual(compare["baseline"]["detection_rate"], 1)
        self.assertEqual(compare["candidate"]["false_pass_rate"], 1)
        self.assertIsNone(compare["candidate"]["false_report_share"])
        self.assertEqual(run_qa("gate", "--scope", "test", "--dir", str(runs[0]))[0], 1)


class TestGovernance(unittest.TestCase):
    def git(self, repo, *args, data=None):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Local Fixture", "-c",
                               "user.email=fixture@invalid.test", *args], input=data,
                              capture_output=True, encoding="utf-8", check=True, timeout=10).stdout.strip()

    def snapshot(self, repo, parent=None):
        self.git(repo, "add", ".")
        tree = self.git(repo, "write-tree")
        args = ("-p", parent) if parent else ()
        return self.git(repo, "commit-tree", tree, *args, data="Local disposable fixture\n")

    def test_performance_rejects_nonlocal_target_before_checking_tool(self):
        with self.assertRaisesRegex(ValueError, "本机"):
            qa_performance.probe("https://remote.invalid/", "https://remote.invalid/", 20, 1, None, None, None)
        with patch("qa_performance.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "未安装"):
                qa_performance.probe("http://127.0.0.1:1/", "http://127.0.0.1:1/", 20, 1, None, None, None)

    def test_impact_degrades_without_base_and_finds_multiple_call_sites(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            self.git(repo, "init", "-q")
            service = repo / "pricing.py"
            service.write_text("def total(): return 10\n", encoding="utf-8")
            (repo / "invoice.py").write_text("from pricing import total\ninvoice = total()\n", encoding="utf-8")
            (repo / "refund.py").write_text("from pricing import total\nrefund = total()\n", encoding="utf-8")
            base = self.snapshot(repo)
            service.write_text("def total(): return 11\n", encoding="utf-8")
            head = self.snapshot(repo, base)
            with self.assertRaisesRegex(ValueError, "源码或基线"):
                qa_impact.scan(repo, "", head, [])
            result = qa_impact.scan(repo, base, head, ["total"])
            self.assertEqual(result["changed"], ["pricing.py"])
            self.assertEqual({item["path"] for item in result["unconfirmed_references"]["total"]},
                             {"pricing.py", "invoice.py", "refund.py"})

    def test_experience_requires_independent_eval_and_explicit_approval(self):
        value = {"kind": "experience", "project_id": "a" * 32, "version": "v2", "subject": "定价",
                 "conditions": "对账", "exclusions": "无账单", "method": "比较计费基准", "source": {"defect": "D1"}}
        assessment = {"status": "passed", "cohort": "held-out"}
        truth = {"candidate_sha256": qa.digest(value)}
        decision = {"decision": "approved", "candidate_sha256": qa.digest(value),
                    "comparison_sha256": qa.digest(assessment), "by": "reviewer", "reference": "approval-1"}
        with patch("qa_experience.compare", return_value={"status": "failed"}):
            with self.assertRaises(ValueError):
                qa_experience.approve(value, decision, truth, Path("old"), Path("new"))
        with patch("qa_experience.compare", return_value=assessment):
            with self.assertRaises(ValueError):
                qa_experience.approve(value, {}, truth, Path("old"), Path("new"))
            approved = qa_experience.approve(value, decision, truth, Path("old"), Path("new"))
        for project, version, overridden in (("b" * 32, "v2", []), ("a" * 32, "v1", []),
                                              ("a" * 32, "v2", ["定价"])):
            self.assertEqual(qa_experience.selected_records([approved], project, version, ["定价"], overridden)["advice"], [])
        self.assertEqual(qa_experience.selected_records([approved], "a" * 32, "v2", ["定价"], [])["advice"], [value])
        other = dict(value, method="不同验证方法")
        conflict = {"record": other, "approval": dict(decision, candidate_sha256=qa.digest(other))}
        self.assertEqual(qa_experience.selected_records(
            [approved, conflict], "a" * 32, "v2", ["定价"], []), {"advice": [], "conflicts": ["定价"]})
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            self.git(repo, "init", "-q")
            path = repo / "qa-experience" / (value["project_id"] + ".json")
            qa.dump_json(path, [approved])
            previous = self.snapshot(repo)
            qa.dump_json(path, [conflict])
            current = self.snapshot(repo, previous)
            for revision, expected in ((current, other), (previous, value)):
                result = qa_experience.recall(repo, revision, value["project_id"], "v2", ["定价"], [])
                self.assertEqual(result["advice"], [expected])
