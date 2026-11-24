# -*- coding: utf-8 -*-
import json
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import qa  # noqa: E402


CASES_CN = '''步骤,预期
"【TC-001 从菜单进入客户录入】
入口：客户管理 > 个人客户录入（/expand/drdCustomerAccess）
前置：已登录权限测试账号；已准备 MD 标注的专用数据。
1、使用有权限访问客户录入模块的测试账号登录测试环境
2、点击一级菜单【客户管理】，再点击【个人客户录入】","1、登录成功，进入系统首页
2、打开 /expand/drdCustomerAccess，页面标题为【个人客户录入】"
"【TC-002 打开新增客户录入】
入口：客户管理 > 个人客户录入（/expand/drdCustomerAccess）
前置：已登录权限测试账号。
1、进入【个人客户录入】页面，点击【新增客户录入】按钮","1、页面右侧弹出新增录入弹窗
2、弹窗展示客户基本信息等字段，底部有【取消】和【提交】按钮"
"【TC-003 取消未提交的新增录入】
入口：客户管理 > 个人客户录入（/expand/drdCustomerAccess）
前置：已登录权限测试账号；开始前记录带唯一标记的记录数。
1、进入页面，点击【新增客户录入】，选择媒体类型 S-A，计费方式选择【非包月任务】
2、填写部分字段后点击【取消】
3、刷新查看列表，核对唯一标记记录数","1、弹窗打开，默认计费方式为【非包月任务】
2、关闭弹窗
3、未提交的取消不产生新记录，本次不新增记录"
"【TC-004 新增时选择媒体类型带入客户名称】
入口：客户管理 > 个人客户录入（/expand/drdCustomerAccess）
前置：S-A 的客户名称与媒体名称均为空，执行前记录准确值。
1、进入页面，点击【新增客户录入】
2、在【媒体类型】下拉选择 S-A","1、新增弹窗可正常选择媒体类型
2、【客户名称】带入 S-A 的客户名称，【客户媒体名称】带入 S-A 的媒体名称，与准入一致。"




'''


def write_cases(tmp: Path, text: str, encoding: str) -> Path:
    p = tmp / f"cases_{encoding.replace('-', '_')}.csv"
    p.write_bytes(text.encode(encoding))
    return p


def run_qa(*args):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            qa.main(list(args))
        return 0, buf.getvalue()
    except SystemExit as e:
        return e.code or 0, buf.getvalue()


class TestInit(unittest.TestCase):
    def test_init_and_resume_preserves_records(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            code, _ = run_qa("init", "--project", "p", "--run-id", "r1", "--dir", str(out))
            self.assertEqual(code, 0)
            d = out / "p" / "r1"
            self.assertTrue((d / "run.json").exists())
            (d / "execution.jsonl").write_text("old\n", encoding="utf-8")
            run_qa("init", "--project", "p", "--run-id", "r1", "--dir", str(out), "--force")
            self.assertEqual((d / "execution.jsonl").read_text(encoding="utf-8"), "old\n")

    def test_duplicate_run_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            run_qa("init", "--project", "p", "--run-id", "r1", "--dir", td)
            code, _ = run_qa("init", "--project", "p", "--run-id", "r1", "--dir", td)
            self.assertNotEqual(code, 0)


class TestParseCsv(unittest.TestCase):
    def test_utf8_sig_utf8_gb18030_and_blank_rows(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for enc in ("utf-8-sig", "utf-8", "gb18030"):
                csvp = write_cases(tmp, CASES_CN, enc)
                outp = tmp / f"out_{enc}.json"
                code, _ = run_qa("parse-cases", "--file", str(csvp), "--out", str(outp))
                self.assertEqual(code, 0)
                data = json.loads(outp.read_text(encoding="utf-8"))
                self.assertEqual(data["count"], 4)
                self.assertEqual([c["id"] for c in data["cases"]],
                                 ["TC-001", "TC-002", "TC-003", "TC-004"])
                for c in data["cases"]:
                    self.assertTrue(c["steps"], c["id"])
                    self.assertTrue(c["expected"], c["id"])
                self.assertIn("客户管理", data["cases"][0]["entry"])
                self.assertTrue(data["cases"][0]["precondition"].startswith("已登录"))


class TestCheckCases(unittest.TestCase):
    def test_duplicate_and_bad_link(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            cases = {"cases": [
                {"id": "TC-001", "req": "REQ-001", "steps": ["1"], "expected": ["x"]},
                {"id": "TC-001", "req": "REQ-001", "steps": ["1"], "expected": ["x"]},
                {"id": "TC-002", "req": "REQ-999", "steps": ["1"], "expected": ["x"]},
            ]}
            reqs = [{"id": "REQ-001"}]
            qa.dump_json(d / "c.json", cases)
            qa.dump_json(d / "r.json", reqs)
            code, msg = run_qa("check-cases", "--cases", str(d / "c.json"), "--reqs", str(d / "r.json"))
            self.assertNotEqual(code, 0)
            self.assertIn("重复", msg)
            self.assertIn("不存在", msg)


class TestRecordAndGate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.d = Path(self.td.name)
        run_qa("init", "--project", "p", "--run-id", "r1", "--dir", str(self.d))
        self.run = self.d / "p" / "r1"
        qa.dump_json(self.run / "requirements.json", [{"id": "REQ-001"}])
        qa.dump_json(self.run / "test-cases.json", {"cases": [
            {"id": "TC-001", "req": "REQ-001", "steps": ["1"], "expected": ["x"]}]})
        qa.dump_json(self.run / "preflight.json", {"smoke": "passed"})

    def tearDown(self):
        self.td.cleanup()

    def test_passed_without_evidence_rejected(self):
        code, _ = run_qa("record", "--dir", str(self.run), "--case", "TC-001", "--state", "passed")
        self.assertNotEqual(code, 0)

    def test_illegal_state_rejected(self):
        code, _ = run_qa("record", "--dir", str(self.run), "--case", "TC-001", "--state", "planned")
        self.assertNotEqual(code, 0)

    def test_evidence_outside_dir_rejected(self):
        ev = self.d / "outside.png"
        ev.write_text("x", encoding="utf-8")
        code, _ = run_qa("record", "--dir", str(self.run), "--case", "TC-001",
                         "--state", "passed", "--evidence", str(ev))
        self.assertNotEqual(code, 0)

    def test_gate_blocks_unexecuted_and_open_p1(self):
        code, _ = run_qa("gate", "--dir", str(self.run))
        self.assertNotEqual(code, 0)
        gate = json.loads((self.run / "gate.json").read_text(encoding="utf-8"))
        self.assertEqual(gate["verdict"], "fail")
        self.assertTrue(any("未执行" in r for r in gate["reasons"]))

    def test_gate_pass_after_evidence(self):
        (self.run / "screenshots").mkdir()
        ev = self.run / "screenshots" / "a.png"
        ev.write_text("png", encoding="utf-8")
        run_qa("record", "--dir", str(self.run), "--case", "TC-001", "--state", "passed", "--evidence", str(ev))
        run_qa("stats", "--dir", str(self.run))
        code, _ = run_qa("gate", "--dir", str(self.run))
        self.assertEqual(code, 0)

    def test_zero_denominator_not_100(self):
        qa.dump_json(self.run / "requirements.json", [])
        run_qa("stats", "--dir", str(self.run))
        stats = json.loads((self.run / "stats.json").read_text(encoding="utf-8"))
        self.assertIsNone(stats["requirement_design_coverage"])


class TestDefect(unittest.TestCase):
    def test_dedupe_and_severity(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            run_qa("init", "--project", "p", "--run-id", "r1", "--dir", str(d))
            run = d / "p" / "r1"
            code, _ = run_qa("defect", "--dir", str(run), "--id", "BUG-001",
                             "--title", "金额错误", "--severity", "P1")
            self.assertEqual(code, 0)
            code, _ = run_qa("defect", "--dir", str(run), "--id", "BUG-001",
                             "--title", "重复", "--severity", "P2")
            self.assertNotEqual(code, 0)
            code, _ = run_qa("defect", "--dir", str(run), "--id", "BUG-002",
                             "--title", "x", "--severity", "PX")
            self.assertNotEqual(code, 0)


class TestRedact(unittest.TestCase):
    def test_redact_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "report.md").write_text("password: secret123 token=abc", encoding="utf-8")
            run_qa("redact", "--dir", str(d))
            text = (d / "report.md").read_text(encoding="utf-8")
            self.assertNotIn("secret123", text)
            self.assertIn("[REDACTED]", text)


if __name__ == "__main__":
    unittest.main()
