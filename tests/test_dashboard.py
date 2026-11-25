# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dashboard  # noqa: E402
import qa  # noqa: E402


class TestDashboard(unittest.TestCase):
    def test_build_html_from_run(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            qa.main(["init", "--project", "demo", "--run-id", "r1", "--dir", str(out)])
            run = out / "demo" / "r1"
            qa.dump_json(run / "requirements.json", [{"id": "REQ-001"}])
            qa.dump_json(run / "test-cases.json", {"cases": [
                {"id": "TC-001", "req": "REQ-001", "steps": ["1"], "expected": ["x"]}]})
            qa.dump_json(run / "preflight.json", {"smoke": "passed"})
            (run / "screenshots").mkdir()
            ev = run / "screenshots" / "a.png"
            ev.write_text("png", encoding="utf-8")
            qa.main(["record", "--dir", str(run), "--case", "TC-001",
                     "--state", "passed", "--evidence", str(ev)])
            qa.main(["stats", "--dir", str(run)])
            qa.main(["gate", "--dir", str(run)])
            qa.main(["report", "--dir", str(run)])

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
            self.assertIn("门禁待判定", html)
            self.assertIn("暂无用例记录", html)
            self.assertIn("无数据", html)
            self.assertNotIn("100.00%", html)

    def test_latest_case_state_and_safe_display(self):
        with tempfile.TemporaryDirectory() as directory:
            qa.main(["init", "--project", "demo", "--run-id", "mixed", "--dir", directory])
            run = Path(directory) / "demo" / "mixed"
            qa.dump_json(run / "test-cases.json", {"cases": [
                {"id": "TC-001", "title": "回归通过"},
                {"id": "TC-002", "title": "<测试标题>"},
                {"id": "TC-003", "title": "尚未执行"},
            ]})
            (run / "execution.jsonl").write_text(
                '{"case_id":"TC-001","state":"failed"}\n'
                '{"case_id":"TC-001","state":"passed"}\n'
                '{"case_id":"TC-002","state":"failed","note":"金额 < 预期","defect_ids":["BUG-001"]}\n',
                encoding="utf-8",
            )
            qa.dump_json(run / "gate.json", {"verdict": "fail", "reasons": ["缺少 <审批>"]})
            (run / "report.md").write_text("原文 <测试>", encoding="utf-8")
            html = dashboard.build_html(run)
            self.assertIn("门禁未通过", html)
            self.assertIn("&lt;审批&gt;", html)
            self.assertIn("原文 &lt;测试&gt;", html)
            self.assertIn('data-state="planned"', html)
            self.assertEqual(html.count('data-state="failed"'), 1)
            self.assertLess(html.index('data-state="failed"'), html.index('data-state="passed"'))
            self.assertIn("&lt;测试标题&gt;", html)
            self.assertIn("金额 &lt; 预期", html)
            self.assertIn("BUG-001", html)
            self.assertNotIn("<测试标题>", html)


if __name__ == "__main__":
    unittest.main()
