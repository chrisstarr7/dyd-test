"""Run cross-domain strict QA checks through agent-browser on a loopback mock.

Usage: python -B tests/enterprise_browser_benchmark.py [--output-dir PATH]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

from browser_benchmark import HOST, ROOT, probe, read_json, require, write_json


PROJECT = "enterprise-browser-benchmark"
PAGE, REQUIREMENT = "PAGE-001", "REQ-001"
ROLE = "Operations agent"
SUPPORT, INVENTORY = "support", "inventory"
ROLE_CASE, EMPTY_CASE, BALANCED, RECOUNT = "TC-001", "TC-002", "TC-003", "TC-004"
ROLE_AC, EMPTY_AC, BALANCE_AC = "AC-001", "AC-002", "AC-003"
FOCUS_CASE, NAVIGATION_CASE = "TC-005", "TC-006"
FOCUS_AC, NAVIGATION_AC = "AC-004", "AC-005"
MAX_NAVIGATION_MS = 6000
PROOF = "evidence/page.json"
SUPPORT_UI = """
<label for="account">Active account</label>
<select id="account"><option>Casey</option><option>Riley</option></select>
<form id="filter">
  <label for="query">Ticket title</label><input id="query" type="search">
  <button>Filter</button>
</form>
<table id="tickets">
  <caption>Support queue</caption>
  <thead><tr><th>Ticket</th><th>Assigned role</th></tr></thead><tbody></tbody>
</table>
<p id="empty" role="status" hidden>No matching tickets</p>
<template id="ticket"><tr><td>Address correction</td><td id="role">Operations agent</td></tr></template>
<script>
  const account = document.querySelector('#account');
  account.addEventListener('change', () => {
    document.querySelector('#principal').textContent = account.value;
  });
  const filter = document.querySelector('#filter');
  const ticket = document.querySelector('#ticket');
  function render() {
    const title = ticket.content.querySelector('td').textContent.toLowerCase();
    const query = document.querySelector('#query').value.trim().toLowerCase();
    const rows = document.querySelector('#tickets tbody');
    const matches = title.includes(query);
    rows.replaceChildren();
    if (matches) rows.append(ticket.content.cloneNode(true));
    document.querySelector('#empty').hidden = matches;
  }
  filter.addEventListener('submit', event => { event.preventDefault(); render(); });
  render();
</script>
"""
INVENTORY_UI = """
<p>Current role <strong id="role">Operations agent</strong></p>
<dl id="ledger">
  <dt>Reported on hand (kg)</dt><dd id="on-hand">1,001.37</dd>
  <dt>Available (kg)</dt><dd id="available">1,000.27</dd>
  <dt>Reserved (kg)</dt><dd id="reserved">1.10</dd>
</dl>
<form id="recount">
  <label for="weight">Recounted reserved weight (kg)</label>
  <input id="weight" type="number" min="0" step="0.01" required>
  <button>Apply recount</button>
</form>
<p id="recounted" role="status" hidden>Recount applied</p>
<script>
  document.querySelector('#recount').addEventListener('submit', event => {
    event.preventDefault();
    document.querySelector('#reserved').textContent = document.querySelector('#weight').value;
    document.querySelector('#recounted').hidden = false;
  });
</script>
"""
UI = {SUPPORT: SUPPORT_UI, INVENTORY: INVENTORY_UI}


class MockHandler(BaseHTTPRequestHandler):
    """仅提供两个本地页面，所有业务操作由浏览器内的表单完成。"""

    def do_GET(self):
        domain = self.path.removeprefix("/")
        if domain not in UI:
            self.send_error(404)
            return
        tenant = tenant_for(domain, self.server.server_port)
        body = (f'<!doctype html><html lang="en"><meta charset="utf-8"><title>{tenant}</title>'
                f'<h1 id="tenant">{tenant}</h1><main id="workspace">'
                f'<p>Signed in as <strong id="principal">Casey</strong></p>{UI[domain]}</main></html>').encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def tenant_for(domain, port):
    return f"{domain.title()} demo at {HOST}:{port}"


def write_plan(directory, domain, url, tenant):
    if domain == SUPPORT:
        specs = [
            (ROLE_AC, "The unfiltered queue displays the assigned operations role.", [
                (ROLE_CASE, "role", "Read the assigned role in the unfiltered queue", ROLE, probe("#role", "text"), None),
            ]),
            (EMPTY_AC, "A ticket search without matches shows zero rows.", [
                (EMPTY_CASE, "normal", "Filter for an unmatched ticket and count rows", 0,
                 probe("#tickets tbody > tr", "count"), "#empty"),
            ]),
            (FOCUS_AC, "The first Tab from the document focuses the account selector.", [
                (FOCUS_CASE, "normal", "Press Tab after opening the workspace", True, probe("#account", "focused"), None),
            ]),
            (NAVIGATION_AC, f"The workspace load event completes within {MAX_NAVIGATION_MS} ms.", [
                (NAVIGATION_CASE, "normal", "Read loadEventEnd for the current navigation", True,
                 {"read": "navigation_ms", "metric": "loadEventEnd", "max_ms": MAX_NAVIGATION_MS}, None),
            ]),
        ]
        anchors = ["#filter", "#tickets"]
    else:
        balance = {"derive": "difference", "scale": 2,
                   "operands": [probe(selector, "text") for selector in ("#on-hand", "#available", "#reserved")]}
        specs = [(BALANCE_AC, "Reported on-hand weight equals available plus reserved weight.", [
            (BALANCED, "normal", "Read the opening inventory ledger", "0.00", balance, None),
            (RECOUNT, "regression", "Apply the recount and recheck all three ledger amounts", "0.00", balance, "#recounted"),
        ])]
        anchors = ["#ledger", "#recount"]

    lines = [rule for _, rule, _ in specs]
    (directory / "prd.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(directory / "prd-scope.json", {"document": "prd.md", "revision": "mock-v1", "sections": [
        {"from": 1, "to": len(lines), "status": "in_scope", "requirements": [REQUIREMENT]},
    ]})
    write_json(directory / "pages.json", [{
        "id": PAGE, "status": "confirmed", "entry": f"Open the {domain} workspace", "url": url,
        "role": ROLE, "tenant": tenant, "anchors": anchors, "ready": "#workspace",
        "identity": {key: {"selector": f"#{key}"} for key in ("role", "tenant", "principal")},
    }])
    criteria, cases = [], []
    for ac, rule, rows in specs:
        criteria.append({"id": ac, "rule": rule, "page": PAGE, "risk": "medium",
                         "scenarios": [row[1] for row in rows]})
        for case, scenario, step, expected, check, ready in rows:
            entry = {"id": case, "req": REQUIREMENT, "page": PAGE, "scenario": scenario, "steps": [step],
                     "operation": "read", "expected": {ac: expected}, "checks": {ac: check}}
            if ready:
                entry["result_ready"] = ready
            cases.append(entry)
    write_json(directory / "requirements.json", [{
        "id": REQUIREMENT, "source": f"prd.md:1-{len(lines)}" if len(lines) > 1 else "prd.md:1", "criteria": criteria,
    }])
    write_json(directory / "test-cases.json", {"cases": cases})


def run_domain(output, browser, env, port, domain):
    """冻结独立计划，检查浏览器采集与正式报告给出的实际结论。"""
    run_id = f"{domain}-{uuid4().hex[:12]}"
    directory = output / PROJECT / run_id
    url, tenant = f"http://{HOST}:{port}/{domain}", tenant_for(domain, port)
    version = f"mock-{domain}-v1"

    def cli(command, *, error=None, code=0):
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, encoding="utf-8", timeout=35)
        detail = (result.stderr or result.stdout).strip()
        require(result.returncode == (1 if error else code) and (not error or error in detail),
                f"{run_id} {command[2:]}: unexpected exit {result.returncode}: {detail}")
        return detail

    def qa(action, *args, **expected):
        return cli([sys.executable, "-B", "scripts/qa.py", action, "--dir", str(directory), *args], **expected)

    def agent(action, *args):
        return cli([browser, "--session", run_id, action, *args])

    def capture(name, phase, target, *, proof=None, error=None):
        path = f"evidence/{name}.json"
        target_flag = "--page" if phase == "page" else "--case"
        options = ["--identity-proof", proof] if proof else []
        detail = cli([sys.executable, "-B", "scripts/qa_browser.py", "--dir", str(directory), "--session", run_id,
                      "--phase", phase, "--output", path, target_flag, target, *options, "--timeout", "2"], error=error)
        if error:
            require(not (directory / path).exists(), f"{run_id}: rejected capture wrote {path}")
            return detail
        return path

    def record(case, evidence, ac, state, actual, *args):
        qa("record", "--case", case, "--state", "observed", "--result", evidence, *args)
        entry = json.loads((directory / "execution.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assertion = entry["observation"]["assertions"][ac]
        require(entry["case_id"] == case and entry["state"] == state
                and assertion["actual"] == actual and assertion["evidence"] == evidence,
                f"{run_id}: unexpected recorded result: {entry}")

    def filter_to_empty():
        agent("fill", "#query", "delivery reschedule")
        agent("click", "#filter button")
        agent("wait", "#empty")

    cli([sys.executable, "-B", "scripts/qa.py", "init", "--dir", str(output), "--project", PROJECT,
         "--run-id", run_id, "--mode", "targeted", "--url", url, "--version", version])
    write_plan(directory, domain, url, tenant)
    qa("freeze-plan")

    try:
        agent("open", url)
        capture("page", "page", PAGE)
        qa("check-page", "--page", PAGE, "--url", url, "--role", ROLE, "--snapshot", PROOF)
        write_json(directory / "preflight.json", {"url": url, "version": version, "smoke": "passed", "evidence": PROOF,
                                                  "sql": "not_applicable", "sql_note": "Browser-only mock"})
        if domain == SUPPORT:
            agent("press", "Tab")
            focus = capture("keyboard-focus", "assert", FOCUS_CASE)
            record(FOCUS_CASE, focus, FOCUS_AC, "passed", True)
            navigation = capture("navigation", "assert", NAVIGATION_CASE)
            record(NAVIGATION_CASE, navigation, NAVIGATION_AC, "passed", True)
            elapsed_ms = read_json(directory / navigation)["observed"][NAVIGATION_AC]
            role = capture("direct-role", "assert", ROLE_CASE)
            record(ROLE_CASE, role, ROLE_AC, "passed", ROLE)
            filter_to_empty()
            scope_error = "登录主体、租户、会话或页面文档已变化"
            rejections = {
                "missing_proof": capture("missing-proof", "assert", EMPTY_CASE, error="未采集到"),
                "role_scenario": capture("role-scenario", "assert", ROLE_CASE, proof=PROOF, error="仅可用于"),
            }
            empty = capture("empty", "assert", EMPTY_CASE, proof=PROOF)
            require(read_json(directory / empty).get("identity_proof") == PROOF, "Empty capture did not inherit the role")
            record(EMPTY_CASE, empty, EMPTY_AC, "passed", 0)

            agent("select", "#account", "Riley")
            agent("wait", "--text", "Signed in as Riley")
            rejections["switched_principal"] = capture("switched", "assert", EMPTY_CASE, proof=PROOF, error=scope_error)
            agent("select", "#account", "Casey")
            agent("wait", "--text", "Signed in as Casey")
            capture("restored-principal", "assert", EMPTY_CASE, proof=PROOF)

            agent("reload")
            fresh = capture("reloaded-page", "page", PAGE)
            filter_to_empty()
            rejections["reloaded_document"] = capture("reloaded", "assert", EMPTY_CASE, proof=PROOF, error=scope_error)
            capture("empty-with-fresh-proof", "assert", EMPTY_CASE, proof=fresh)
            write_json(directory / "identity-rejections.json", rejections)
            failed_cases = []
            summary = (f"Tab focus passed, loadEventEnd={elapsed_ms} ms (limit {MAX_NAVIGATION_MS} ms); "
                       "empty queue accepted with valid proof; missing proof, role case, account switch and reload rejected")
        else:
            baseline = capture("balanced", "assert", BALANCED)
            record(BALANCED, baseline, BALANCE_AC, "passed", "0.00")
            agent("fill", "#weight", "1.11")
            agent("click", "#recount button")
            changed = capture("recount", "assert", RECOUNT)
            old = read_json(directory / baseline)["observed"]
            require(old == {f"{BALANCE_AC}.0": "1,001.37", f"{BALANCE_AC}.1": "1,000.27", f"{BALANCE_AC}.2": "1.10"}
                    and read_json(directory / changed)["observed"] == {**old, f"{BALANCE_AC}.2": "1.11"},
                    "Recount should change only the reserved amount")
            qa("defect", "--id", "BUG-001", "--severity", "P3", "--case", RECOUNT,
               "--title", "Recount leaves the published ledger out of balance")
            record(RECOUNT, changed, BALANCE_AC, "failed", "-0.01", "--defect", "BUG-001")
            failed_cases = [RECOUNT]
            summary = "balanced ledger passed at 0.00; recount failed at -0.01"

        qa("gate", code=1)
        gate = read_json(directory / "gate.json")
        require(gate["test_verdict"] == ("fail" if failed_cases else "pass")
                and gate["test_reasons"] == [f"用例失败未闭环：{case}" for case in failed_cases]
                and gate["release_verdict"] == "fail", f"{run_id}: unexpected gate: {gate}")
        qa("report")
        require((directory / "report.md").stat().st_size > 0, f"{run_id}: missing report")
        print(f"{domain}: {summary}; functional={gate['test_verdict']}, release={gate['release_verdict']}", flush=True)
    finally:
        agent("close")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Keep reports and evidence under this directory")
    args = parser.parse_args()
    browser = shutil.which("agent-browser")
    if not browser:
        parser.exit(1, "agent-browser must be installed and on PATH.\n")

    with TemporaryDirectory(prefix="qa-enterprise-") as temporary:
        scratch = Path(temporary)
        config = scratch / "agent-browser.json"
        write_json(config, {})
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("AGENT_BROWSER_")}
        if os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH"):
            env["AGENT_BROWSER_EXECUTABLE_PATH"] = os.environ["AGENT_BROWSER_EXECUTABLE_PATH"]
        env.update(PYTHONIOENCODING="utf-8", AGENT_BROWSER_CONFIG=str(config), AGENT_BROWSER_SOCKET_DIR=str(scratch),
                   AGENT_BROWSER_NO_VERSION_CHECK="1", AGENT_BROWSER_ALLOWED_DOMAINS=HOST,
                   AGENT_BROWSER_ARGS="--disable-background-networking,--no-proxy-server")
        output = args.output_dir.resolve() if args.output_dir else scratch / "evidence"
        with ThreadingHTTPServer((HOST, 0), MockHandler) as server:
            worker = Thread(target=server.serve_forever)
            worker.start()
            try:
                for domain in UI:
                    run_domain(output, browser, env, server.server_port, domain)
            finally:
                server.shutdown()
                worker.join()
        if args.output_dir:
            print(f"Evidence saved in {output / PROJECT}")


if __name__ == "__main__":
    main()
