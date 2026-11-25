# -*- coding: utf-8 -*-
import json
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
            self.assertIn("质量门禁", html)
            self.assertIn("pass", html)
            self.assertNotIn("secret123", html)


if __name__ == "__main__":
    unittest.main()
