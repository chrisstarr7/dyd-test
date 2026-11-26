# -*- coding: utf-8 -*-
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import qa_matrix  # noqa: E402
from qa_browser import write_capture


URL = "https://example.test/project/P-001"
ENVIRONMENT = {
    "id": "ENV-001",
    "url": URL,
    "principal_ref": "QA-ROLE-A",
    "tenant": "TENANT-TEST",
    "version": "BUILD-1",
    "data_state": "project-fact=1,personal-fact=2,index=warm",
}


def matrix_plan():
    return {
        "id": "TC-010",
        "test_kind": "recall",
        "matrix": {
            "evidence": "evidence/matrix.json",
            "repeats": 2,
            "environment": copy.deepcopy(ENVIRONMENT),
            "controls": {"positive": ["Q-001"], "negative": ["Q-004"]},
            "comparisons": [
                {"base": "Q-001", "variant": "Q-003", "factor": "prompt_length"},
            ],
            "queries": [
                {
                    "id": "Q-001", "role": "positive", "prompt": "写一条产品脚本，突出卖点",
                    "factors": {"intent": "script", "prompt_length": "short",
                                "personal_load": "2", "theme": "keyboard"},
                    "expect": {"project": {"min": 1}}, "citation_min": 1,
                },
                {
                    "id": "Q-002", "role": "sample", "prompt": "整理产品培训提纲",
                    "factors": {"intent": "training", "prompt_length": "medium",
                                "personal_load": "2", "theme": "keyboard"},
                    "expect": {"project": 0},
                },
                {
                    "id": "Q-003", "role": "sample", "prompt": "写一条30秒产品口播脚本，明确人群和结尾要求",
                    "factors": {"intent": "script", "prompt_length": "long",
                                "personal_load": "2", "theme": "keyboard"},
                    "expect": {"project": {"min": 1}}, "citation_min": 1,
                },
                {
                    "id": "Q-004", "role": "negative", "prompt": "解释发票验真流程",
                    "factors": {"intent": "finance", "prompt_length": "short",
                                "personal_load": "2", "theme": "invoice"},
                    "expect": {"project": 0},
                },
            ],
        },
    }


def default_row(query, repeat):
    project = 1 if query["expect"].get("project") in (1, {"min": 1}) else 0
    return {
        "query_id": query["id"],
        "repeat": repeat,
        "search_invoked": True,
        "retrieved": {"project": project, "personal": 2},
        "injected": {"project": project, "personal": 2},
        "dropped": {},
        "citation_delta": 1 if project else 0,
        "actual": {"project": project},
        "observed_at": "2026-11-25T10:30:00+08:00",
        "evidence": f"evidence/{query['id']}-{repeat}.json",
    }


def run_review(plan, changes=None):
    changes = changes or {}
    folder = tempfile.TemporaryDirectory()
    root = Path(folder.name)
    (root / "evidence").mkdir()
    rows = []
    probability = plan["matrix"].get("probability")
    display = plan["matrix"].get("recall_evidence") == "display"
    if probability:
        label = b"independent labels"
        (root / probability["label_file"]).write_bytes(label)
    for query in plan["matrix"]["queries"]:
        for repeat in range(1, plan["matrix"]["repeats"] + 1):
            row = default_row(query, repeat)
            if probability:
                row.update(sample_id=f"{query['id']}/{repeat}", grade_status="scored",
                           **{key: probability[key] for key in ("grader", "rubric", "labels")})
                row["actual"].update(schema_valid=True, tenant_match=True, phrasing=repeat)
            row.update(changes.get((query["id"], repeat), {}))
            row.update(changes.get((query["id"], "all"), {}))
            rows.append(row)
            grade = {}
            if probability:
                sample_file = f"evidence/raw-{query['id']}-{repeat}.json"
                raw = json.dumps({"observation": f"independent sample {repeat}", "run_id": "EVAL-1",
                                  "sample_id": row["sample_id"], "query_id": query["id"], "source": "fixture",
                                  "execution_id": row.get("test_execution_id", row["sample_id"])}).encode()
                (root / sample_file).write_bytes(raw)
                grade = {"sample_id": row["sample_id"], "run_id": "EVAL-1", "case_id": plan["id"],
                         "query_id": query["id"], "grader": probability["grader"], "rubric": probability["rubric"],
                         "labels_sha256": hashlib.sha256(label).hexdigest(),
                         "measurement_sha256": qa_matrix.digest(row["actual"]),
                         "artifact": sample_file, "artifact_sha256": hashlib.sha256(raw).hexdigest()}
            if display:
                write_capture(root, root / row["evidence"], {
                    "source": "agent-browser", "run_id": "EVAL-1", "version": "BUILD-1", "case_id": plan["id"],
                    "url": plan["recall_url"], "execution_id": row.get("test_execution_id", row["evidence"]),
                    **{key: row[key] for key in ("query_id", "repeat", "observed_at", "actual", "search_invoked")},
                })
            else:
                (root / row["evidence"]).write_text(json.dumps(grade), encoding="utf-8")
    data = {"case_id": plan["id"], "environment": plan["matrix"]["environment"], "queries": rows}
    (root / "evidence" / "matrix.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    try:
        return qa_matrix.review(root, {"url": URL, "version": "BUILD-1", "run_id": "EVAL-1"}, plan)
    finally:
        folder.cleanup()


class TestNondeterministicMatrix(unittest.TestCase):
    def test_trigger_matrix_distinguishes_zero_hits_and_no_search(self):
        plan = matrix_plan()
        plan["recall_url"] = URL
        matrix = plan["matrix"]
        positive, _, _, negative = matrix["queries"]
        positive["search_expected"], negative["search_expected"] = True, False
        positive.pop("citation_min")
        matrix.update(objective="trigger", recall_evidence="display", queries=[positive, negative], comparisons=[])
        good = {("Q-004", repeat): {"search_invoked": False} for repeat in (1, 2)}
        result = run_review(plan, good)
        self.assertTrue(result["passed"], result["errors"])
        self.assertIn("后台检索、注入未验证", result["summary"]["evidence_scope"])
        bad = dict(good)
        bad[("Q-004", 2)] = {"search_invoked": True}
        self.assertFalse(run_review(plan, bad)["passed"])
        bad[("Q-004", 2)] = {"search_invoked": None}
        with self.assertRaisesRegex(ValueError, "触发信号"):
            run_review(plan, bad)
        bad[("Q-004", 2)] = {"search_invoked": False, "test_execution_id": "evidence/Q-004-1.json"}
        with self.assertRaisesRegex(ValueError, "独立对话"):
            run_review(plan, bad)

    def probability_plan(self):
        plan = matrix_plan()
        plan["matrix"].update(repeats=10, probability={
            "min_samples": 10, "min_lower_bound": 0.5, "grader": "fixture-grader",
            "rubric": "contract-v1", "labels": "independent-v1", "label_file": "evidence/labels.txt",
        })
        for query in plan["matrix"]["queries"]:
            query["hard_expect"] = {"schema_valid": True, "tenant_match": True}
        return plan

    def test_probability_accepts_variation_but_not_independent_hard_violations(self):
        plan = self.probability_plan()
        one_miss = {"project": 0, "schema_valid": True, "tenant_match": True}
        data = {("Q-001", 1): {"actual": one_miss, "retrieved": {"project": 0}, "injected": {"project": 0}}}
        result = run_review(plan, data)
        self.assertTrue(result["passed"], result["errors"])
        self.assertFalse(result["summary"]["flaky"])
        self.assertEqual(result["summary"]["items"][0]["matched"], 9)
        one_miss["tenant_match"] = False
        self.assertTrue(any("违反硬约束" in error for error in run_review(plan, data)["errors"]))

    def test_probability_unavailable_grader_and_small_sample_do_not_pass(self):
        plan = self.probability_plan()
        with self.assertRaisesRegex(ValueError, "有效评分"):
            run_review(plan, {("Q-001", 2): {"grade_status": "unavailable"}})
        with self.assertRaisesRegex(ValueError, "独立原始执行标识"):
            run_review(plan, {("Q-001", 2): {"test_execution_id": "Q-001/1"}})
        plan["matrix"]["probability"]["min_lower_bound"] = 0.99
        self.assertFalse(run_review(plan)["passed"])

    def test_plan_rejects_single_sample_and_missing_controls(self):
        plan = matrix_plan()
        plan.pop("matrix")
        self.assertTrue(any("缺少冻结测试矩阵" in error for error in qa_matrix.validate_plan(plan)))

        plan = matrix_plan()
        plan.pop("test_kind")
        self.assertTrue(any("未声明 test_kind" in error for error in qa_matrix.validate_plan(plan)))

        plan = matrix_plan()
        plan["matrix"]["repeats"] = 1
        self.assertTrue(any("至少为 2" in error for error in qa_matrix.validate_plan(plan)))

        plan = matrix_plan()
        plan["matrix"]["controls"]["positive"] = []
        self.assertTrue(any("positive" in error for error in qa_matrix.validate_plan(plan)))

        plan = matrix_plan()
        plan["matrix"]["controls"]["negative"] = []
        self.assertTrue(any("negative" in error for error in qa_matrix.validate_plan(plan)))

    def test_single_query_matrix_is_rejected(self):
        plan = matrix_plan()
        plan["matrix"]["queries"] = plan["matrix"]["queries"][:1]
        plan["matrix"]["controls"] = {"positive": ["Q-001"], "negative": []}
        plan["matrix"]["comparisons"] = []
        errors = qa_matrix.validate_plan(plan)
        self.assertTrue(any("3 种业务意图" in error for error in errors))
        self.assertTrue(any("short/medium/long" in error for error in errors))
        self.assertTrue(any("单因子对照" in error for error in errors))

    def test_plan_requires_three_intents_and_all_lengths(self):
        plan = matrix_plan()
        plan["matrix"]["queries"][1]["factors"]["intent"] = "script"
        self.assertTrue(any("3 种业务意图" in error for error in qa_matrix.validate_plan(plan)))

        plan = matrix_plan()
        plan["matrix"]["queries"][1]["factors"]["prompt_length"] = "short"
        self.assertTrue(any("short/medium/long" in error for error in qa_matrix.validate_plan(plan)))

    def test_plan_rejects_multi_factor_comparison(self):
        plan = matrix_plan()
        plan["matrix"]["queries"][2]["factors"]["theme"] = "other"
        self.assertTrue(any("只改变" in error for error in qa_matrix.validate_plan(plan)))

    def test_review_rejects_missing_repeat(self):
        plan = matrix_plan()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / "evidence").mkdir()
        rows = []
        for query in plan["matrix"]["queries"]:
            for repeat in (1, 2):
                if query["id"] == "Q-001" and repeat == 2:
                    continue
                row = default_row(query, repeat)
                rows.append(row)
                (root / row["evidence"]).write_text("{}", encoding="utf-8")
        (root / "evidence" / "matrix.json").write_text(json.dumps({
            "case_id": plan["id"], "environment": plan["matrix"]["environment"], "queries": rows,
        }, ensure_ascii=False), encoding="utf-8")
        result = qa_matrix.review(root, {"url": URL, "version": "BUILD-1"}, plan)
        self.assertFalse(result["passed"])
        self.assertTrue(any("未按 2 次完整重复" in error for error in result["errors"]))

    def test_stable_matrix_passes(self):
        result = run_review(matrix_plan())
        self.assertTrue(result["passed"], result["errors"])
        self.assertFalse(result["summary"]["flaky"])
        self.assertEqual(result["summary"]["observations"], 8)

    def test_flaky_results_are_not_silently_passed(self):
        result = run_review(matrix_plan(), {
            ("Q-001", 2): {
                "retrieved": {"project": 0, "personal": 2},
                "injected": {"project": 0, "personal": 2},
                "actual": {"project": 0},
                "citation_delta": 0,
            },
        })
        self.assertFalse(result["passed"])
        self.assertTrue(result["summary"]["flaky"])
        self.assertTrue(any("不稳定" in error for error in result["errors"]))

    def test_recall_failure_layers_are_separated(self):
        decision = run_review(matrix_plan(), {("Q-001", 2): {"search_invoked": False}})
        self.assertTrue(any("检索决策层" in error for error in decision["errors"]))
        ordered = run_review(matrix_plan(), {
            ("Q-001", 2): {
                "search_invoked": False,
                "retrieved": {"project": 0, "personal": 2},
                "injected": {"project": 0, "personal": 2},
                "actual": {"project": 0},
                "citation_delta": 0,
            },
        })
        self.assertTrue(any("检索决策层、检索层" in error for error in ordered["errors"]))

        retrieval = run_review(matrix_plan(), {
            ("Q-001", 2): {
                "retrieved": {"project": 0, "personal": 2},
                "injected": {"project": 0, "personal": 2},
                "actual": {"project": 0},
                "citation_delta": 0,
            },
        })
        self.assertTrue(any("检索层" in error for error in retrieval["errors"]))

        injection = run_review(matrix_plan(), {
            ("Q-001", 2): {
                "retrieved": {"project": 1, "personal": 2},
                "injected": {"project": 0, "personal": 2},
                "dropped": {"project": "token_budget"},
                "actual": {"project": 0},
                "citation_delta": 0,
            },
        })
        self.assertTrue(any("上下文注入层" in error for error in injection["errors"]))

        citation = run_review(matrix_plan(), {
            ("Q-001", 2): {"citation_delta": 0},
        })
        self.assertTrue(any("引用计数层" in error for error in citation["errors"]))

    def test_negative_zero_expectation_does_not_require_search(self):
        result = run_review(matrix_plan(), {
            ("Q-004", "all"): {"search_invoked": False},
        })
        self.assertTrue(result["passed"], result["errors"])


if __name__ == "__main__":
    unittest.main()
