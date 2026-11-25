"""Verify full PRD coverage for an isolated account lifecycle in a real browser.

Usage: python -B tests/seed_account_benchmark.py [--output-dir PATH]
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import parse_qs, quote_plus

from browser_benchmark import HOST, ROOT, probe, read_json, require, write_json


PROJECT, TENANT, VERSION = "seed-account-benchmark", "local-account-demo", "mock-account-v1"
ADMIN, MEMBER, GUEST = "PAGE-001", "PAGE-002", "PAGE-003"
CREATE, UPDATE, LOGIN, DELETE, DENIED = "TC-001", "TC-002", "TC-003", "TC-004", "TC-005"
FIXTURE, RECIPE, RECEIPT = "FIX-001", "account", "seed-receipt.json"
MAIL_DOMAIN = "accounts.invalid"
EDITED = "Updated public label"
LOGIN_FIELDS = {"alias": "#login-alias", "password": "#login-password"}
CREATE_FIELDS = {"marker": "#marker", "alias": "#alias", "password": "#password"}
ADMIN_FORM = """
<form id="create" action="/create" method="post" autocomplete="off">
  <label for="marker">Fixture label</label><input id="marker" name="marker" required>
  <label for="alias">Account email</label><input id="alias" name="alias" type="email" required>
  <label for="password">New password</label><input id="password" name="password" type="password" required>
  <button>Create account</button>
</form>
"""
LOGIN_FORM = """
<form id="login" action="/login" method="post" autocomplete="off">
  <label for="login-alias">Account email</label><input id="login-alias" name="alias" type="email" required>
  <label for="login-password">Password</label><input id="login-password" name="password" type="password" required>
  <button>Sign in</button>
</form>
"""


class AccountMock(BaseHTTPRequestHandler):
    """仅在回环测试实例的内存中保留账号与登录会话。"""

    def page(self, role, contents):
        body = (f'<!doctype html><html lang="en"><meta charset="utf-8"><title>Account demo</title>'
                f'<h1 id="tenant">{TENANT}</h1><p id="role">{role}</p>'
                f'<main id="workspace">{contents}</main></html>').encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path, session=None):
        self.send_response(303)
        self.send_header("Location", path)
        self.send_header("Cache-Control", "no-store")
        if session:
            self.send_header("Set-Cookie", f"member={session}; Path=/member; HttpOnly; SameSite=Strict")
        self.end_headers()

    def do_GET(self):
        account = self.server.account
        if self.path == "/":
            row = ""
            if account:
                marker = escape(account[0], quote=True)
                label = escape(self.server.display_name)
                row = (f'<tr data-marker="{marker}"><td class="marker">{marker}</td><td>'
                       f'<span class="display">{label}</span><form id="update" method="post" action="/update">'
                       f'<input name="display_name" aria-label="Display name" value="{escape(label, quote=True)}">'
                       f'<button name="marker" value="{marker}">Save label</button></form>'
                       '<form method="post" action="/delete">'
                       f'<button name="marker" value="{marker}">Delete account</button></form></td></tr>')
            member_session = '<p id="member-session">Test member session exists</p>' if self.server.member_session else ''
            self.page("Admin", f'<p id="admin-principal">benchmark-admin</p>{member_session}'
                      f'{ADMIN_FORM}<table id="accounts"><caption>Test accounts</caption>'
                      f'<tbody>{row}</tbody></table>')
        elif self.path == "/login":
            self.page("Guest", LOGIN_FORM)
        elif self.path == "/member":
            member = SimpleCookie(self.headers.get("Cookie", "")).get("member")
            if account and member and member.value == self.server.member_session:
                self.page("Member", f'<h2 id="signed-in">Signed in</h2><p id="member-marker">{escape(account[0])}</p>')
            else:
                self.redirect("/login")
        else:
            self.send_error(404)

    def do_POST(self):
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or len(length) > 4 or int(length) > 4096:
            self.send_error(400)
            return
        try:
            fields = parse_qs(self.rfile.read(int(length)).decode("ascii"), strict_parsing=True, max_num_fields=3)
        except ValueError:
            self.send_error(400)
            return
        if any(len(values) != 1 for values in fields.values()):
            self.send_error(400)
            return
        form = {key: values[0] for key, values in fields.items()}
        account = self.server.account
        marker, alias, password = (form.get(key, "") for key in CREATE_FIELDS)
        if self.path == "/create" and not account and marker and alias.endswith(f"@{MAIL_DOMAIN}") and password:
            self.server.account = (marker, alias, password)
            self.server.display_name = "Original public label"
            self.redirect("/")
        elif self.path == "/update" and account and marker == account[0] and form.get("display_name"):
            self.server.display_name = form["display_name"]
            self.redirect("/")
        elif self.path == "/login" and account and (alias, password) == account[1:]:
            self.server.member_session = secrets.token_urlsafe(24)
            self.redirect("/member", self.server.member_session)
        elif self.path == "/delete" and account and marker == account[0]:
            self.server.account = self.server.member_session = None
            self.redirect("/")
        else:
            self.send_error(400)

    def log_message(self, *_args):
        pass


def write_plan(directory, origin, receipt):
    marker = receipt["fixture"]["marker"]
    row = f'tr[data-marker="{marker}"]'
    specs = [
        (CREATE, ADMIN, "create", "The seeded account appears in the admin list after saving.", row, "count", 1),
        (UPDATE, ADMIN, "update", "The account displays the edited public label on a new page load.",
         ".display", "text", EDITED),
        (LOGIN, MEMBER, "read", "The seeded account signs in and displays its public label.",
         "#member-marker", "text", marker),
        (DELETE, ADMIN, "delete", "The account disappears from the admin list after deletion.", row, "count", 0),
        (DENIED, GUEST, "read", "The deleted account cannot continue using its earlier member session.",
         "#login", "visible", True),
    ]
    (directory / "prd.md").write_text("\n".join(item[3] for item in specs) + "\n", encoding="utf-8")
    source = f"prd.md:1-{len(specs)}"
    write_json(directory / "prd-scope.json", {"document": "prd.md", "revision": VERSION, "source_complete": True,
        "flows": [{"id": "FLOW-001", "source": source, "cases": [spec[0] for spec in specs]}], "sections": [
        {"from": 1, "to": len(specs), "status": "in_scope", "requirements": ["REQ-001"]},
    ]})
    write_json(directory / "pages.json", [
        {"id": page, "status": "confirmed", "entry": f"Open the {role} workspace", "url": origin + route,
         "role": role, "tenant": TENANT, "ready": "#workspace", "anchors": anchors,
         "identity": {"role": {"selector": "#role"}, "tenant": {"selector": "#tenant"},
                      **({"principal": {"selector": subject}} if subject else {})}}
        for page, role, route, anchors, subject in (
            (ADMIN, "Admin", "", ["#create", "#accounts"], "#admin-principal"),
            (MEMBER, "Member", "member", ["#signed-in", "#member-marker"], "#member-marker"),
            (GUEST, "Guest", "login", ["#login-alias", "#login-password"], None),
        )
    ])
    criteria, cases = [], []
    for index, (case_id, page, action, rule, selector, read, value) in enumerate(specs, 1):
        ac = f"AC-{index:03}"
        scenario = "negative" if case_id == DENIED else "normal"
        criteria.append({"id": ac, "page": page, "rule": rule, "risk": "medium", "scenarios": [scenario],
                         "source_line": index, "source_part": 1, "source_quote": rule})
        check = probe(selector, read)
        if action == "update":
            check["scope"] = "fixture"
        case = {"id": case_id, "req": "REQ-001", "page": page, "operation": action, "scenario": scenario,
                "steps": [rule], "fixture": FIXTURE, "expected": {ac: value}, "checks": {ac: check}}
        if cases:
            case["depends_on"] = [cases[-1]["id"]]
        if action != "read":
            case.update(fixture=FIXTURE, verify_reload=True)
        if action == "update":
            case["effect"] = {"mode": "change", "field": "display", "to": EDITED}
        if case_id == LOGIN:
            case["actor"] = "fixture"
        if case_id == CREATE:
            case.update(seed_form={"root": "#create", "fields": CREATE_FIELDS},
                        seed_login={"root": "#login", "url": origin + "login", "ready": "#login",
                                    "anchors": list(LOGIN_FIELDS.values()), "fields": LOGIN_FIELDS})
        cases.append(case)
    write_json(directory / "requirements.json", [{"id": "REQ-001", "source": source, "criteria": criteria}])
    write_json(directory / "prd-review.json", {
        "revision": VERSION, "source_sha256": hashlib.sha256((directory / "prd.md").read_bytes()).hexdigest(),
        "reviewer": "Local benchmark specification", "reference": "Isolated account mock requirements",
        "obligations": [{"id": f"OB-{index:03}", "source_line": index, "source_part": 1, "excerpt": item[3],
                         "rule": item[3], "criteria": [f"AC-{index:03}"]} for index, item in enumerate(specs, 1)],
    })
    write_json(directory / "test-cases.json", {"cases": cases})
    write_json(directory / RECEIPT, receipt)
    write_json(directory / "fixtures.json", {
        "boundary": dict(receipt["boundary"], evidence=RECEIPT),
        "fixtures": [dict(receipt["fixture"], page=ADMIN, evidence=RECEIPT, ownership="verified",
                          probe={"selector": "#accounts tbody > tr", "ready": "#accounts", "lookup": "complete",
                                 "key": {"selector": ".marker", "value": marker},
                                 "fields": {"label": probe(".marker", "text"), "display": probe(".display", "text")},
                                 "related": {"session": "#member-session"}},
                          cleanup_scope="Account and its only member session", cleanup_counts={"session": 0})],
    })
    write_risk_plan(directory, specs)
    return row


def write_risk_plan(directory, specs):
    """给本地完整验收配套已知风险和不适用依据。"""
    run = read_json(directory / "run.json")
    identity = {"run_id": run["run_id"], "version": VERSION}
    dimensions = {key: {"status": "in_scope"} for key in ("functionality", "data", "permission", "regression")}
    for key, reason in {
        "integration": "The mock does not call other systems",
        "reliability": "No asynchronous or retry requirements in the mock PRD",
        "performance": "No timing budget or load target in the mock PRD",
        "accessibility": "No accessibility claims in this isolated benchmark",
    }.items():
        decision = f"decision-{key}.json"
        write_json(directory / decision, {**identity, "type": "risk_decision", "subject": f"dimension:{key}",
                   "decision": "not_applicable", "by": "Local benchmark specification", "reason": reason,
                   "reference": "Isolated account mock PRD"})
        dimensions[key] = {"status": "not_applicable", "decision": decision}
    risks = []
    for key, risk_id, points in (("functionality", "RISK-001", (1, 2)), ("data", "RISK-002", (4,)),
                                 ("permission", "RISK-003", (3, 5)), ("regression", "RISK-004", (5,))):
        risks.append({"id": risk_id, "dimension": key, "severity": "high" if key == "permission" else "medium",
                      "source": specs[points[0] - 1][3], "invariant": specs[points[0] - 1][3],
                      "impact": "The account lifecycle can produce the wrong final state",
                      "scenarios": [{"id": f"SC-{point:03}", "criterion": f"AC-{point:03}", "layer": "online_ui",
                                     "scenario": "negative" if point == 5 else "normal"} for point in points]})
    write_json(directory / "risk-review.json", {**identity, "schema_version": 1, "source": "prd.md",
                                                "dimensions": dimensions, "risks": risks})


def run_workflow(server, output, profile, browser, env):
    """核验两次提交、独立登录和最终回收，并检查产物不含私有字段。"""
    origin = f"http://{HOST}:{server.server_port}/"
    # 预留规则会拒绝类似真实号码的连续数字，运行标记采用字母编码。
    run_id = "account-" + secrets.token_hex(6).translate(str.maketrans("0123456789", "ghijklmnop"))
    directory = output / PROJECT / run_id
    admin_session, login_session = f"{run_id}-admin", f"{run_id}-member"
    key = secrets.token_urlsafe(36)
    seed_env = dict(env, DYD_QA_SEED_KEY=key)
    cli_text = []

    def cli(command, subject, *, code=0, use_key=False):
        result = subprocess.run(command, cwd=ROOT, env=seed_env if use_key else env,
                                capture_output=True, encoding="utf-8", timeout=35)
        cli_text.extend((*command, result.stdout, result.stderr))
        require(result.returncode == code, f"{subject}: expected exit {code}, got {result.returncode}")
        return result.stdout.strip()

    def script(file, *args, code=0, use_key=False):
        return cli([sys.executable, "-B", f"scripts/{file}.py", *args], f"{file} {' '.join(args[:1])}",
                   code=code, use_key=use_key)

    def qa(action, *args, code=0):
        return script("qa", action, "--dir", str(directory), *args, code=code)

    def agent(session, action, *args):
        return cli([browser, "--session", session, action, *args], f"agent-browser {action}")

    def capture(session, phase, name, *target):
        path = f"evidence/{name}.json"
        script("qa_browser", "--dir", str(directory), "--session", session, "--phase", phase, "--output", path, *target)
        require(read_json(directory / path)["session"] == session, f"Unexpected capture session for {name}")
        return path

    def sample(action, phase, case, state):
        path = capture(admin_session, phase, f"{case}-{phase}", "--case", case)
        qa(action, "--tenant", TENANT, "--case", case, "--state", state, "--evidence", path)
        return path

    def fill(session, field_count, *mode):
        result = script("qa_seed_browser", "--dir", str(directory), "--profile", str(profile), "--recipe", RECIPE,
                        "--case", CREATE, "--session", session, *mode, use_key=True)
        require(json.loads(result) == {"status": "filled", "fixture": FIXTURE, "field_count": field_count},
                "Unexpected seed fill receipt")

    script("qa", "init", "--dir", str(output), "--project", PROJECT, "--run-id", run_id,
           "--mode", "full", "--url", origin, "--version", VERSION)
    actions = ["create", "update", "delete"]
    write_json(profile, {"seed_contract": 1, "project": PROJECT, "url": origin, "environment": "test",
                         "tenant": TENANT, "authorization": "loopback-account-demo", "external_effects": "disabled",
                         "actions": actions, "namespace": "account", "recipes": {RECIPE: {
                             "actions": actions, "reference_field": "marker", "fields": {
                                 "marker": {"kind": "marker"}, "alias": {"kind": "email", "domain": MAIL_DOMAIN},
                                 "password": {"kind": "secret"},
                             },
                         }}})
    receipt = json.loads(script("qa_seed", "prepare", "--dir", str(directory), "--profile", str(profile),
                                "--tenant", TENANT, "--recipe", RECIPE, "--fixture", FIXTURE, "--operation", "create"))
    require(receipt.get("status") == "reserved", "Account fixture was not reserved")
    row = write_plan(directory, origin, receipt)
    qa("freeze-plan")

    opened = []
    try:
        for session, route in ((admin_session, ""), (login_session, "login")):
            opened.append(session)
            agent(session, "open", origin + route)
        smoke = capture(admin_session, "page", "admin", "--page", ADMIN)
        qa("check-page", "--page", ADMIN, "--url", origin, "--role", "Admin", "--snapshot", smoke)
        write_json(directory / "preflight.json", {"url": origin, "version": VERSION, "smoke": "passed",
                                                  "evidence": smoke, "sql": "not_applicable", "sql_note": "In-memory mock"})
        sample("prepare-data", "before", CREATE, "absent")
        fill(admin_session, 3)
        require(server.account is None, "Seed fill submitted the create form")
        agent(admin_session, "click", "#create button")
        agent(admin_session, "wait", row)
        created = sample("reconcile-data", "after", CREATE, "present")
        qa("record", "--case", CREATE, "--state", "passed", "--result", created)
        _, alias, password = server.account

        sample("prepare-data", "before", UPDATE, "present")
        agent(admin_session, "fill", "#update input", EDITED)
        agent(admin_session, "click", "#update button")
        edited = sample("reconcile-data", "after", UPDATE, "present")
        qa("record", "--case", UPDATE, "--state", "passed", "--result", edited)

        fill(login_session, 2, "--login")
        require(server.member_session is None, "Seed fill submitted the login form")
        agent(login_session, "click", "#login button")
        agent(login_session, "wait", "#signed-in")
        logged_in = capture(login_session, "assert", "member-login", "--case", LOGIN)
        qa("record", "--case", LOGIN, "--state", "passed", "--result", logged_in)
        session_value = server.member_session

        sample("prepare-data", "before", DELETE, "present")
        agent(admin_session, "click", f'{row} form[action="/delete"] button')
        deleted = sample("reconcile-data", "after", DELETE, "absent")
        qa("record", "--case", DELETE, "--state", "passed", "--result", deleted)
        agent(admin_session, "reload")
        cleanup = capture(admin_session, "cleanup", "cleanup", "--fixture", FIXTURE)
        qa("close-data", "--tenant", TENANT, "--fixture", FIXTURE, "--evidence", cleanup)
        agent(login_session, "reload")
        agent(login_session, "wait", "#login")
        denied = capture(login_session, "assert", "session-denied", "--case", DENIED)
        qa("record", "--case", DENIED, "--state", "passed", "--result", denied)
        require(server.account is None and server.member_session is None, "Account or session remained after cleanup")

        qa("gate", code=1)
        gate = read_json(directory / "gate.json")
        require(gate["test_verdict"] == "pass" and gate["release_verdict"] == "fail", "Unexpected QA gate verdict")
        qa("report")
    finally:
        for session in reversed(opened):
            agent(session, "close")

    # 回执只应包含公共标记。合成邮箱、密码、密钥和会话值均留在内存中。
    private = {candidate.encode("utf-8") for value in (key, alias, password, session_value)
               for candidate in (value, quote_plus(value))}
    artifacts = [*directory.rglob("*"), profile, profile.with_name(".qa-seed.sqlite3")]
    require(all(secret not in line.encode("utf-8") for line in cli_text for secret in private)
            and all(all(secret not in path.read_bytes() for secret in private) for path in artifacts if path.is_file()),
            "Private account values appeared in CLI arguments, output or benchmark artifacts")
    print("Full account PRD: created, edited, signed in separately, deleted, and session invalidated. Functional gate passed.")
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Keep the QA report and evidence under this directory")
    args = parser.parse_args()
    browser = shutil.which("agent-browser")
    if not browser:
        parser.exit(1, "agent-browser must be installed and on PATH.\n")

    with TemporaryDirectory(prefix="qa-seed-account-") as temporary:
        scratch = Path(temporary)
        config = scratch / "agent-browser.json"
        write_json(config, {})
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("AGENT_BROWSER_") and key.upper() != "DYD_QA_SEED_KEY"}
        if os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH"):
            env["AGENT_BROWSER_EXECUTABLE_PATH"] = os.environ["AGENT_BROWSER_EXECUTABLE_PATH"]
        env.update(PYTHONIOENCODING="utf-8", AGENT_BROWSER_CONFIG=str(config), AGENT_BROWSER_SOCKET_DIR=str(scratch),
                   AGENT_BROWSER_NO_VERSION_CHECK="1", AGENT_BROWSER_ALLOWED_DOMAINS=HOST,
                   AGENT_BROWSER_ARGS="--disable-background-networking,--no-proxy-server,--incognito")
        output = args.output_dir.resolve() if args.output_dir else scratch / "runs"
        with ThreadingHTTPServer((HOST, 0), AccountMock) as server:
            server.account = server.member_session = None
            worker = Thread(target=server.serve_forever)
            worker.start()
            try:
                directory = run_workflow(server, output, scratch / "profile.json", browser, env)
            finally:
                server.shutdown()
                worker.join()
        if args.output_dir:
            print(f"Evidence saved in {directory}")


if __name__ == "__main__":
    main()
