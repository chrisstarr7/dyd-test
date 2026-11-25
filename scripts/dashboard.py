#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dyd-test 测试结果看板：读取任务目录结构化结果，生成本地 HTML 看板并启动服务。

只监听 127.0.0.1，只输出生成的 dashboard.html，不暴露任务目录内其他文件。
Python 3.8+，仅标准库。
"""
from __future__ import annotations

import argparse
import html
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def pct(value) -> str:
    if value is None:
        return "无有效统计数据"
    return f"{value * 100:.2f}%"


def read_execution(path: Path) -> list:
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def last_state(records: list, case_id: str) -> str:
    states = [r.get("state") for r in records if r.get("case_id") == case_id]
    return states[-1] if states else "planned"


def build_html(d: Path) -> str:
    run = load_json(d / "run.json")
    stats = load_json(d / "stats.json") if (d / "stats.json").exists() else {}
    gate = load_json(d / "gate.json") if (d / "gate.json").exists() else {}
    defects = load_json(d / "defects.json") if (d / "defects.json").exists() else []
    cases_data = load_json(d / "test-cases.json") if (d / "test-cases.json").exists() else {}
    cases = cases_data.get("cases", [])
    records = read_execution(d / "execution.jsonl")

    verdict = gate.get("verdict", "未执行")
    verdict_class = "pass" if verdict == "pass" else "fail"

    stat_cards = [
        ("用例总数", stats.get("planned_cases", len(cases))),
        ("通过", stats.get("passed", 0)),
        ("失败", stats.get("failed", 0)),
        ("阻塞", stats.get("blocked", 0)),
        ("跳过", stats.get("skipped", 0)),
        ("用例执行率", pct(stats.get("case_execution_rate"))),
        ("执行通过率", pct(stats.get("executed_case_pass_rate"))),
        ("需求设计覆盖", pct(stats.get("requirement_design_coverage"))),
        ("需求执行覆盖", pct(stats.get("requirement_execution_coverage"))),
        ("需求通过率", pct(stats.get("requirement_pass_rate"))),
    ]
    cards = "".join(
        f'<div class="card"><div class="num">{esc(v)}</div><div class="label">{esc(k)}</div></div>'
        for k, v in stat_cards
    )

    case_rows = []
    for c in cases:
        cid = c.get("id") or ""
        st = last_state(records, cid)
        case_rows.append(
            f"<tr><td>{esc(cid)}</td><td>{esc(c.get('title', ''))}</td>"
            f"<td>{esc(c.get('req', ''))}</td><td class='state {esc(st)}'>{esc(st)}</td></tr>"
        )
    case_table = "".join(case_rows) or "<tr><td colspan='4'>无用例数据</td></tr>"

    defect_rows = []
    for x in defects:
        defect_rows.append(
            f"<tr><td>{esc(x.get('id', ''))}</td><td>{esc(x.get('title', ''))}</td>"
            f"<td>{esc(x.get('severity', ''))}</td><td>{esc(x.get('status', ''))}</td></tr>"
        )
    defect_table = "".join(defect_rows) or "<tr><td colspan='4'>无缺陷</td></tr>"

    reasons = gate.get("reasons") or []
    reason_html = "".join(f"<li>{esc(r)}</li>" for r in reasons) or "<li>无</li>"

    report_text = ""
    report_path = d / "report.md"
    if report_path.exists():
        report_text = esc(report_path.read_text(encoding="utf-8"))

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dyd-test 测试看板</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #f5f6f8; color: #1f2328; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
.meta {{ color: #6a737d; font-size: 13px; margin-bottom: 16px; }}
.banner {{ padding: 14px 16px; border-radius: 8px; font-size: 16px; font-weight: 600; margin-bottom: 16px; }}
.banner.pass {{ background: #dafbe1; color: #116329; }}
.banner.fail {{ background: #ffebe9; color: #cf222e; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.card {{ background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: 12px 14px; }}
.num {{ font-size: 20px; font-weight: 600; }}
.label {{ color: #6a737d; font-size: 12px; margin-top: 2px; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d0d7de; border-radius: 8px; overflow: hidden; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid #d0d7de; text-align: left; font-size: 13px; }}
th {{ background: #f6f8fa; }}
h2 {{ font-size: 15px; margin: 22px 0 8px; }}
.state.passed {{ color: #116329; font-weight: 600; }}
.state.failed {{ color: #cf222e; font-weight: 600; }}
.state.blocked, .state.skipped {{ color: #9a6700; }}
pre {{ background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: 12px; white-space: pre-wrap; font-size: 13px; }}
</style>
</head>
<body>
<h1>dyd-test 测试看板</h1>
<div class="meta">项目：{esc(run.get('project'))} ｜ 运行编号：{esc(run.get('run_id'))} ｜ 模式：{esc(run.get('mode'))} ｜ 环境：{esc(run.get('url') or '未提供')} ｜ 版本：{esc(run.get('version') or '未提供')}</div>
<div class="banner {verdict_class}">质量门禁：{esc(verdict)}</div>
<div class="grid">{cards}</div>
<h2>门禁原因</h2>
<ul>{reason_html}</ul>
<h2>用例执行</h2>
<table><tr><th>用例</th><th>标题</th><th>需求</th><th>最终状态</th></tr>{case_table}</table>
<h2>缺陷</h2>
<table><tr><th>编号</th><th>标题</th><th>等级</th><th>状态</th></tr>{defect_table}</table>
<h2>报告原文</h2>
<pre>{report_text}</pre>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    page = b""

    def do_GET(self):
        if self.path in ("/", "/index.html"):
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
