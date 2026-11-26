# -*- coding: utf-8 -*-
import socket
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dashboard  # noqa: E402
import qa  # noqa: E402
from test_qa import acceptance_run, observation, run_qa


class TestDashboard(unittest.TestCase):
    def test_free_port_skips_occupied_port(self):
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            server, selected = dashboard.find_free_port(port, tries=20)
            try:
                self.assertNotEqual(port, selected)
            finally:
                server.server_close()

    def test_build_html_from_run(self):
        with tempfile.TemporaryDirectory() as td:
            run = acceptance_run(Path(td))
            code, _ = run_qa("record", "--dir", str(run), "--case", "TC-001",
                             "--state", "passed", "--result", observation(run))
            self.assertEqual(code, 0)

            html = dashboard.build_html(run)
            self.assertIn("dyd-test 测试看板", html)
            self.assertIn("TC-001", html)
            self.assertIn("门禁通过", html)
            self.assertIn('data-state="passed"', html)
            self.assertEqual(html.count("1/1 · 100.00%"), 5)
            self.assertIn('href="#report">报告原文', html)
            self.assertNotIn('href="report.html"', html)
            report = html.split('<details class="panel report" id="report"><summary>报告原文</summary>', 1)[1]
            self.assertRegex(report, r'<a href="#report-chapter-\d+">断言与证据</a>')
            self.assertIn('<span class="status passed">通过</span>', report)
            self.assertIn('<span>用例执行率</span><strong>100.00%</strong>', report)
            self.assertRegex(report, r'<ul><li>TC-001[^<]*<ul><li>AC-001 预期 true，实测 true')
            self.assertIn('<summary>查看 Markdown 原文</summary><pre># 测试报告', report)

    def test_empty_run_has_no_passing_verdict_or_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            qa.main(["init", "--project", "demo", "--run-id", "empty", "--dir", directory])
            html = dashboard.build_html(Path(directory) / "demo" / "empty")
            self.assertIn('<p class="gate blocked">测试未完成</p>', html)
            self.assertIn("暂无用例记录", html)
            self.assertIn("无数据", html)
            self.assertNotIn("100.00%", html)

    def test_collection_dashboard_combines_task_runs_once(self):
        with tempfile.TemporaryDirectory() as directory:
            first = acceptance_run(Path(directory))
            self.assertEqual(run_qa("record", "--dir", str(first), "--case", "TC-001",
                                    "--state", "passed", "--result", observation(first))[0], 0)
            second = first.parent / "r2"
            shutil.copytree(first, second)
            run = qa.load_json(second / "run.json")
            run["run_id"] = "r2"
            run["task_id"] = "memory-v3"
            qa.dump_json(second / "run.json", run)
            first_run = qa.load_json(first / "run.json")
            first_run["task_id"] = "memory-v3"
            qa.dump_json(first / "run.json", first_run)
            self.assertEqual(run_qa("record", "--dir", str(second), "--case", "TC-001",
                                    "--state", "failed", "--result", observation(second, actual=False),
                                    "--note", "断言失败", "--defect", "BUG-001")[0], 0)

            html = dashboard.build_collection_html([first, second])
            self.assertIn("测试任务看板", html)
            self.assertIn("2 个运行 · 1 个看板", html)
            self.assertIn("测试未完成", html)
            self.assertIn("memory-v3", html)
            self.assertIn("r1", html)
            self.assertIn("r2", html)
            self.assertIn("正式发布准入：待确认（含旧版运行）", html)
            self.assertIn('id="run-1-report-chapter-1"', html)
            self.assertIn('id="run-2-report-chapter-1"', html)

    def test_task_outcome_uses_most_severe_run(self):
        review = lambda outcome: {"gate": {"test_outcome": outcome}}
        self.assertEqual(dashboard.task_outcome([review("passed"), review("incomplete")]), "incomplete")
        self.assertEqual(dashboard.task_outcome([review("passed"), review("incomplete"), review("failed")]), "failed")
        self.assertEqual(dashboard.task_outcome([review("passed"), review("passed")]), "passed")

    def test_collection_dashboard_rejects_different_test_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            first = acceptance_run(Path(directory))
            second = first.parent / "r2"
            shutil.copytree(first, second)
            run = qa.load_json(second / "run.json")
            run["run_id"] = "r2"
            run["url"] = "https://another.example.test/"
            qa.dump_json(second / "run.json", run)
            with self.assertRaisesRegex(ValueError, "同一需求、同一项目、同一测试地址"):
                dashboard.build_collection_html([first, second])

    def test_collection_dashboard_rejects_different_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            first = acceptance_run(Path(directory))
            first_run = qa.load_json(first / "run.json")
            first_run["task_id"] = "memory"
            qa.dump_json(first / "run.json", first_run)
            second = first.parent / "r2"
            shutil.copytree(first, second)
            second_run = qa.load_json(second / "run.json")
            second_run["run_id"] = "r2"
            second_run["task_id"] = "workspace"
            qa.dump_json(second / "run.json", second_run)
            with self.assertRaisesRegex(ValueError, "task_id"):
                dashboard.build_collection_html([first, second])

    def test_dashboard_and_report_reject_stale_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            run = acceptance_run(Path(directory))
            args = ("record", "--dir", str(run), "--case", "TC-001")
            code, _ = run_qa(*args, "--state", "passed", "--result", observation(run))
            self.assertEqual(code, 0)
            run_qa("report", "--dir", str(run))
            self.assertEqual(qa.load_json(run / "gate.json")["verdict"], "pass")
            code, _ = run_qa(*args, "--state", "failed", "--result", observation(run, actual=False),
                             "--note", "金额 < 预期 <script>alert(1)</script>", "--defect", "BUG-001")
            self.assertEqual(code, 0)
            html = dashboard.build_html(run)
            self.assertIn("门禁未通过", html)
            self.assertEqual(html.count('data-state="failed"'), 1)
            self.assertNotIn("门禁通过", html)
            self.assertIn("&lt;查看物料&gt;", html)
            self.assertIn("金额 &lt; 预期", html)
            self.assertIn("BUG-001", html)
            self.assertNotIn("<查看物料>", html)
            report = html.split('<details class="panel report" id="report">', 1)[1]
            self.assertIn('<span class="status failed">断言失败</span>', report)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", report)
            self.assertNotIn("<script>alert(1)</script>", report)
            run_qa("report", "--dir", str(run))
            self.assertEqual(qa.load_json(run / "gate.json")["verdict"], "fail")
            self.assertIn("预期 true，实测 false", (run / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
