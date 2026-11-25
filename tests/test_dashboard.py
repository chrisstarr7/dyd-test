# -*- coding: utf-8 -*-
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
            self.assertIn("100.00%", html)

    def test_empty_run_has_no_passing_verdict_or_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            qa.main(["init", "--project", "demo", "--run-id", "empty", "--dir", directory])
            html = dashboard.build_html(Path(directory) / "demo" / "empty")
            self.assertIn("门禁未通过", html)
            self.assertIn("暂无用例记录", html)
            self.assertIn("无数据", html)
            self.assertNotIn("100.00%", html)

    def test_dashboard_and_report_reject_stale_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            run = acceptance_run(Path(directory))
            args = ("record", "--dir", str(run), "--case", "TC-001")
            code, _ = run_qa(*args, "--state", "passed", "--result", observation(run))
            self.assertEqual(code, 0)
            run_qa("report", "--dir", str(run))
            self.assertEqual(qa.load_json(run / "gate.json")["verdict"], "pass")
            code, _ = run_qa(*args, "--state", "failed", "--result", observation(run, actual=False),
                             "--note", "金额 < 预期", "--defect", "BUG-001")
            self.assertEqual(code, 0)
            html = dashboard.build_html(run)
            self.assertIn("门禁未通过", html)
            self.assertEqual(html.count('data-state="failed"'), 1)
            self.assertNotIn("门禁通过", html)
            self.assertIn("&lt;新增物料&gt;", html)
            self.assertIn("金额 &lt; 预期", html)
            self.assertIn("BUG-001", html)
            self.assertNotIn("<新增物料>", html)
            run_qa("report", "--dir", str(run))
            self.assertEqual(qa.load_json(run / "gate.json")["verdict"], "fail")
            self.assertIn("预期 true，实测 false", (run / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
