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


URL = "http://127.0.0.1:8765/"


def acceptance_run(root, strict=False):
    run_qa("init", "--project", "p", "--run-id", "r1", "--dir", str(root),
           "--url", URL, "--version", "v1")
    run = root / "p" / "r1"
    config = qa.load_json(run / "run.json")
    if not strict:
        config.pop("qa_contract")
    config.pop("assurance_contract")
    config.pop("evidence_contract")
    qa.dump_json(run / "run.json", config)
    (run / "page.txt").write_text("物料列表 | 新增物料 | 管理员", encoding="utf-8")
    qa.dump_json(run / "requirements.json", [{
        "id": "REQ-001", "source": "PRD §2",
        "criteria": [{"id": "AC-001", "rule": "列表刷新后包含新记录", "page": "PAGE-001"}],
    }])
    qa.dump_json(run / "test-cases.json", {"cases": [{
        "id": "TC-001", "req": "REQ-001", "page": "PAGE-001",
        "title": "<查看物料>", "operation": "read", "steps": ["刷新后核对列表"], "expected": {"AC-001": True},
    }]})
    qa.dump_json(run / "pages.json", [{
        "id": "PAGE-001", "status": "confirmed", "entry": "登录 → 物料列表",
        "url": URL, "role": "admin", "role_marker": "管理员",
        "anchors": ["物料列表", "新增物料"], "snapshot": "page.txt",
    }])
    qa.dump_json(run / "preflight.json", {
        "smoke": "passed", "url": URL, "version": "v1", "sql": "not_applicable",
        "sql_note": "内存模拟系统", "evidence": "page.txt",
    })
    return run


def observation(run, actual=True, **changes):
    (run / "actual.txt").write_text("刷新后观察：" + str(actual), encoding="utf-8")
    result = run / "result.json"
    qa.dump_json(result, {
        "url": URL, "role": "admin", "snapshot": "page.txt",
        "assertions": {"AC-001": {"actual": actual, "evidence": "actual.txt"}},
        **changes,
    })
    return str(result)


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

    def test_task_id_is_saved_and_resume_cannot_change_it(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self.assertEqual(run_qa("init", "--project", "p", "--run-id", "r1", "--task-id", "memory",
                                    "--dir", str(out))[0], 0)
            run_path = out / "p" / "r1" / "run.json"
            self.assertEqual(qa.load_json(run_path)["task_id"], "memory")
            self.assertEqual(run_qa("init", "--project", "p", "--run-id", "r1", "--task-id", "memory",
                                    "--dir", str(out), "--force")[0], 0)
            code, _ = run_qa("init", "--project", "p", "--run-id", "r1", "--task-id", "workspace",
                             "--dir", str(out), "--force")
            self.assertNotEqual(code, 0)
            self.assertEqual(qa.load_json(run_path)["task_id"], "memory")


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
        self.run = acceptance_run(self.d)

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
                         "--state", "passed", "--result", observation(self.run, assertions={
                             "AC-001": {"actual": True, "evidence": str(ev)},
                         }))
        self.assertNotEqual(code, 0)

    def test_gate_blocks_unexecuted_and_open_p1(self):
        run_qa("defect", "--dir", str(self.run), "--id", "BUG-001", "--title", "模拟缺陷", "--severity", "P1")
        code, _ = run_qa("gate", "--dir", str(self.run))
        self.assertNotEqual(code, 0)
        gate = json.loads((self.run / "gate.json").read_text(encoding="utf-8"))
        self.assertEqual(gate["verdict"], "fail")
        self.assertTrue(any("未执行" in r for r in gate["reasons"]))
        self.assertTrue(any("P0/P1" in r for r in gate["reasons"]))

    def test_gate_pass_after_evidence(self):
        code, _ = run_qa("record", "--dir", str(self.run), "--case", "TC-001",
                         "--state", "passed", "--result", observation(self.run))
        self.assertEqual(code, 0)
        code, _ = run_qa("gate", "--dir", str(self.run))
        self.assertEqual(code, 0)

    def test_missing_criterion_and_skipped_case_block_gate(self):
        requirements = qa.load_json(self.run / "requirements.json")
        requirements[0]["criteria"].append({"id": "AC-002", "rule": "可按名称检索", "page": "PAGE-001"})
        qa.dump_json(self.run / "requirements.json", requirements)
        run_qa("record", "--dir", str(self.run), "--case", "TC-001", "--state", "skipped", "--note", "缺数据")
        review = qa.assess(self.run)
        self.assertEqual(review["gate"]["verdict"], "fail")
        self.assertIn("必测验收点无用例：AC-002", review["gate"]["reasons"])
        self.assertIn("必测用例被跳过：TC-001", review["gate"]["reasons"])
        self.assertEqual(review["stats"]["criterion_design_coverage"], 0.5)
        self.assertIsNone(review["stats"]["executed_case_pass_rate"])

    def test_pending_or_wrong_page_criterion_cannot_be_recorded_as_passed(self):
        requirements = qa.load_json(self.run / "requirements.json")
        criterion = requirements[0]["criteria"][0]
        for change in ({"status": "pending"}, {"page": "PAGE-002"}):
            with self.subTest(change=change):
                requirements[0]["criteria"] = [{**criterion, **change}]
                qa.dump_json(self.run / "requirements.json", requirements)
                code, _ = run_qa("record", "--dir", str(self.run), "--case", "TC-001",
                                 "--state", "passed", "--result", observation(self.run))
                self.assertNotEqual(code, 0)

    def test_damaged_execution_log_replaces_old_gate_with_failure(self):
        run_qa("record", "--dir", str(self.run), "--case", "TC-001",
               "--state", "passed", "--result", observation(self.run))
        run_qa("report", "--dir", str(self.run))
        self.assertEqual(qa.load_json(self.run / "gate.json")["verdict"], "pass")
        with (self.run / "execution.jsonl").open("a", encoding="utf-8") as output:
            output.write("{\n")
        run_qa("report", "--dir", str(self.run))
        gate = qa.load_json(self.run / "gate.json")
        self.assertEqual(gate["verdict"], "fail")
        self.assertIn("执行记录无法读取或格式损坏", gate["reasons"])

    def test_empty_plan_and_smoke_only_block_gate(self):
        qa.dump_json(self.run / "test-cases.json", [])
        qa.dump_json(self.run / "preflight.json", {"smoke": "passed"})
        review = qa.assess(self.run)
        self.assertEqual(review["gate"]["verdict"], "fail")
        self.assertIn("无用例计划", review["gate"]["reasons"])
        self.assertTrue(any("版本未确认" in reason for reason in review["gate"]["reasons"]))

    def test_wrong_page_role_or_markers_rejected(self):
        arguments = ("record", "--dir", str(self.run), "--case", "TC-001", "--state", "passed", "--result")
        for changes in ({"url": URL + "#/other"}, {"url": "http://127.0.0.1:8766/"}, {"role": "user"}):
            with self.subTest(changes=changes):
                code, _ = run_qa(*arguments, observation(self.run, **changes))
                self.assertNotEqual(code, 0)
        (self.run / "wrong.txt").write_text("物料列表", encoding="utf-8")
        code, _ = run_qa(*arguments, observation(self.run, snapshot="wrong.txt"))
        self.assertNotEqual(code, 0)

    def test_failed_assertion_cannot_pass_and_missing_evidence_invalidates_run(self):
        arguments = ("record", "--dir", str(self.run), "--case", "TC-001")
        code, _ = run_qa(*arguments, "--state", "passed", "--result", observation(self.run, actual=1))
        self.assertNotEqual(code, 0)
        result = observation(self.run)
        (self.run / "actual.txt").write_bytes(b"")
        code, _ = run_qa(*arguments, "--state", "passed", "--result", result)
        self.assertNotEqual(code, 0)
        code, _ = run_qa(*arguments, "--state", "passed", "--result", observation(self.run))
        self.assertEqual(code, 0)
        (self.run / "actual.txt").unlink()
        self.assertEqual(qa.assess(self.run)["results"]["TC-001"]["state"], "unknown")
        self.assertEqual(qa.assess(self.run)["gate"]["verdict"], "fail")

    def test_p2_waiver_requires_version_and_authorization(self):
        run_qa("record", "--dir", str(self.run), "--case", "TC-001",
               "--state", "passed", "--result", observation(self.run))
        defect = {"id": "BUG-001", "severity": "P2", "status": "new"}
        for version, verdict in (("", "fail"), ("old", "fail"), ("v1", "pass")):
            defect["waiver"] = {"by": "产品负责人", "reason": "书面风险接受记录",
                                "reference": "QA-156", "version": version}
            qa.dump_json(self.run / "defects.json", [defect])
            self.assertEqual(qa.assess(self.run)["gate"]["verdict"], verdict)

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
