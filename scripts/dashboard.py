#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dyd-test 测试结果看板：读取任务目录结构化结果，生成本地 HTML 看板并启动服务。

只监听 127.0.0.1，并仅提供生成的看板页面。
Python 3.8+，仅标准库。
"""
from __future__ import annotations

import argparse
import html
import re
import threading
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template

from qa import OUTCOME_LABELS, RELEASE_LABELS, assess, format_report, redact_text


CASE_LABELS = {
    "passed": "通过", "failed": "失败", "blocked": "阻塞",
    "skipped": "跳过", "planned": "待执行", "unknown": "未知",
}
CASE_ORDER = ("failed", "blocked", "unknown", "planned", "skipped", "passed")
DEFECT_LABELS = {
    "new": "待修复", "fixed": "待回归", "closed": "已关闭",
    "activated": "已激活", "unable": "无法复现",
}
MODES = {"full": "完整测试", "targeted": "专项测试", "regression": "回归测试"}
REPORT_STATS = {
    "planned_cases": "计划用例", "passed": "通过", "failed": "失败", "blocked": "阻塞",
    "skipped": "跳过", "unknown": "未知", "case_execution_rate": "用例执行率",
    "executed_case_pass_rate": "执行通过率", "requirement_count": "需求总数", "criterion_count": "验收点总数",
    "criterion_design_coverage": "验收点设计覆盖", "criterion_execution_coverage": "验收点执行覆盖",
    "criterion_pass_rate": "验收点通过比例",
}


def esc(value) -> str:
    return html.escape(redact_text(str(value)), quote=True)


def as_percent(value):
    if type(value) in (int, float) and 0 <= value <= 1:
        return value * 100
    return None


def pct(value) -> str:
    percent = as_percent(value)
    return "无数据" if percent is None else f"{percent:.2f}%"


def metric(value, count, total):
    return f"{count}/{total} · {pct(value)}" if total else pct(value)


def outcome_chart(counts, total):
    if not total:
        return "var(--track)"
    segments = []
    start = 0
    for state in CASE_LABELS:
        count = counts[state]
        if count:
            end = start + count
            segments.append(f"var(--{state}) {start / total:.8%} {end / total:.8%}")
            start = end
    return f"conic-gradient({', '.join(segments)})"


def coverage_rows(review):
    stats = review["stats"]
    for key, label, group in (
        ("criterion_design_coverage", "设计覆盖", "covered"),
        ("criterion_execution_coverage", "执行覆盖", "completed"),
        ("criterion_pass_rate", "验收点通过", "passed_criteria"),
    ):
        ratio = stats.get(key)
        width = as_percent(ratio) or 0
        result = metric(ratio, len(review[group]), stats["criterion_count"])
        yield (
            f'<div class="coverage-row"><div><span>{label}</span><strong>{result}</strong></div>'
            f'<div class="track" aria-hidden="true"><span style="width:{width:.4f}%"></span></div></div>'
        )


def render_case(case_id, case, record, state):
    notes = []
    if record.get("note"):
        notes.append(esc(record["note"]))
    if record.get("defect_ids"):
        notes.append("关联缺陷 " + "、".join(esc(value) for value in record["defect_ids"]))
    note_html = "".join(f'<span class="row-note">{note}</span>' for note in notes)
    return (
        f'<tr data-state="{state}"><td class="identifier">{esc(case_id)}</td>'
        f'<td><strong>{esc(case.get("title") or "未命名用例")}</strong>{note_html}</td>'
        f'<td class="identifier">{esc(case.get("req") or "—")}</td>'
        f'<td><span class="badge {state}">{CASE_LABELS[state]}</span></td></tr>'
    )


def render_defect(defect):
    severity = defect.get("severity")
    status = defect.get("status")
    tone = "failed" if severity in ("P0", "P1") else "blocked" if severity == "P2" else "neutral"
    return (
        f'<tr><td class="identifier">{esc(defect.get("id") or "—")}</td>'
        f'<td><strong>{esc(defect.get("title") or "未命名缺陷")}</strong>'
        f'<span class="row-note">{"、".join(esc(value) for value in defect.get("case_ids") or [])}</span></td>'
        f'<td><span class="badge {tone}">{esc(severity or "—")}</span></td>'
        f'<td>{esc(DEFECT_LABELS.get(status, status) or "未知")}</td></tr>'
    )


def render_report_list(block, section, indent=0):
    """渲染报告生成器使用的列表层级，正文始终以纯文本处理。"""
    lead, *items = re.split(rf"(?m)^ {{{indent}}}- ", block) if indent <= 4 else [block]
    content = f'<p class="report-text">{esc(lead.strip())}</p>' if lead.strip() else ""
    if not items:
        return content
    rows = []
    for item in items:
        title, _, remainder = item.rstrip().partition("\n")
        label = esc(title)
        if section == "统计" and not indent:
            key, _, value = title.partition("：")
            if key in REPORT_STATS:
                if ("rate" in key or "coverage" in key) and re.fullmatch(r"[01](?:\.\d+)?", value):
                    value = pct(float(value))
                label = f'<span>{REPORT_STATS[key]}</span><strong>{esc(value)}</strong>'
        elif section == "PRD 验收点" and not indent:
            rule, separator, status = title.rpartition("：")
            tone = {"通过": "passed", "断言失败": "failed", "未完成": "pending", "无关联用例": "neutral"}.get(status)
            if separator and tone:
                label = f'<span>{esc(rule)}</span><span class="status {tone}">{esc(status)}</span>'
        if remainder:
            label += render_report_list(remainder, section, indent + 2)
        rows.append(f"<li>{label}</li>")
    return content + "<ul>" + "".join(rows) + "</ul>"


def render_report(review) -> str:
    """从同一核验快照生成看板内的报告正文与章节目录。"""
    report = format_report(review)
    _, *chapters = report.split("\n\n## ")
    links, sections = [], []
    for index, chapter in enumerate(chapters, 1):
        title, _, body = chapter.partition("\n")
        anchor = f"report-chapter-{index}"
        links.append(f'<a href="#{anchor}">{esc(title)}</a>')
        section_class = "paper stats" if title == "统计" else "paper"
        sections.append(f'<section class="{section_class}" id="{anchor}" aria-labelledby="{anchor}-title">'
                        f'<h2 id="{anchor}-title">{esc(title)}</h2>{render_report_list(body, title)}</section>')
    return (
        '<div class="report-reading">'
        f'<nav class="toc" aria-label="章节目录"><strong>章节目录</strong>{"".join(links)}</nav>'
        f'<div class="chapters">{"".join(sections)}'
        f'<details class="paper raw"><summary>查看 Markdown 原文</summary><pre>{esc(report)}</pre></details>'
        '</div></div>'
    )


def build_html(directory: Path) -> str:
    """将测试记录渲染为可离线打开的独立 HTML。"""
    review = assess(directory)
    run, stats, gate = review["run"], review["stats"], review["gate"]
    cases, records, defects = review["cases"], review["results"], review["defects"]

    rows = []
    counts = Counter()
    for case_id, case in cases.items():
        record = records[case_id]
        state = record["state"]
        counts[state] += 1
        rows.append((CASE_ORDER.index(state), render_case(case_id, case, record, state)))
    case_table = "".join(row for _, row in sorted(rows, key=lambda entry: entry[0]))
    if not case_table:
        case_table = '<tr><td colspan="4" class="empty">暂无用例记录</td></tr>'

    legend = "".join(
        f'<li class="{state}"><span class="dot"></span>{label}<strong>{counts[state]}</strong></li>'
        for state, label in CASE_LABELS.items()
        if state != "unknown" or counts[state]
    )
    options = "".join(
        f'<option value="{state}">{CASE_LABELS[state]} · {counts[state]}</option>'
        for state in CASE_ORDER if counts[state]
    )
    release_html = ""
    if "test_verdict" in gate:
        outcome = gate["test_outcome"]
        tone = {"passed": "passed", "failed": "failed", "incomplete": "blocked"}[outcome]
        verdict_title = OUTCOME_LABELS[outcome]
        reasons = gate["test_reasons"]
        release_title = f'正式发布准入：{RELEASE_LABELS[gate["release_verdict"]]}'
        if gate["release_reasons"]:
            release_items = "".join(f"<li>{esc(reason)}</li>" for reason in gate["release_reasons"])
            release_html = f'<details class="release"><summary>{release_title}</summary><ul>{release_items}</ul></details>'
        else:
            release_html = f'<p class="release">{release_title}</p>'
    else:
        tone, verdict_title = {
            "pass": ("passed", "历史质量门禁通过"),
            "fail": ("failed", "历史质量门禁未通过"),
        }.get(gate.get("verdict"), ("neutral", "历史质量门禁待判定"))
        reasons = gate.get("reasons") or []
    reasons_html = ""
    if reasons:
        items = [f"<li>{esc(reason)}</li>" for reason in reasons]
        reasons_html = f'<div class="reasons"><ul>{"".join(items[:4])}</ul>'
        if len(items) > 4:
            reasons_html += (
                f'<details><summary>另有 {len(items) - 4} 条原因</summary>'
                f'<ul>{"".join(items[4:])}</ul></details>'
            )
        reasons_html += "</div>"

    defect_table = "".join(render_defect(defect) for defect in sorted(
        defects, key=lambda defect: (defect.get("status") == "closed", defect.get("severity") or "Z")
    ))
    if not defect_table:
        defect_table = '<tr><td colspan="4" class="empty">暂无缺陷记录</td></tr>'
    open_defects = sum(defect.get("status") != "closed" for defect in defects)
    critical = sum(defect.get("status") != "closed" and defect.get("severity") in ("P0", "P1") for defect in defects)
    return Template(Path(__file__).with_name("dashboard-template.html").read_text(encoding="utf-8")).substitute(
        project=esc(run.get("project") or "未命名项目"),
        run_id=esc(run.get("run_id") or "未记录"),
        version=esc(run.get("version") or "未记录"),
        mode=esc(MODES.get(run.get("mode"), run.get("mode")) or "未记录"),
        environment=esc(run.get("url") or "未记录"),
        tone=tone,
        verdict=verdict_title,
        release=release_html,
        reasons=reasons_html,
        total=len(cases),
        chart=outcome_chart(counts, len(cases)),
        legend=legend,
        execution_rate=metric(stats["case_execution_rate"], stats["executed"], stats["planned_cases"]),
        pass_rate=metric(stats["executed_case_pass_rate"], stats["passed"], stats["executed"]),
        requirements=stats["requirement_count"],
        criteria=stats["criterion_count"],
        coverage="".join(coverage_rows(review)),
        options=options,
        case_table=case_table,
        defect_table=defect_table,
        defect_count=len(defects),
        open_defects=open_defects,
        critical=critical,
        report=render_report(review),
    )


class DashboardHandler(BaseHTTPRequestHandler):
    page = b""

    def do_GET(self):
        if self.path in ("/", "/index.html", "/dashboard.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self.page)))
            self.end_headers()
            self.wfile.write(self.page)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def find_free_port(start: int, tries: int = 50):
    for port in range(start, start + tries):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
            return server, port
        except OSError:
            continue
    raise SystemExit("无法在 127.0.0.1 上找到可用端口")


def main(argv=None):
    p = argparse.ArgumentParser(prog="dashboard.py", description="dyd-test 测试结果看板")
    p.add_argument("--dir", required=True)
    p.add_argument("--port", type=int, default=8899)
    args = p.parse_args(argv)

    d = Path(args.dir)
    if not (d / "run.json").exists():
        raise SystemExit(f"不是有效的任务目录（缺少 run.json）：{d}")

    page = build_html(d)
    (d / "dashboard.html").write_text(page, encoding="utf-8")

    server, port = find_free_port(args.port)
    DashboardHandler.page = page.encode("utf-8")
    url = f"http://127.0.0.1:{port}/"
    print(f"看板已启动：{url}（文件：{d / 'dashboard.html'}）")
    threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
