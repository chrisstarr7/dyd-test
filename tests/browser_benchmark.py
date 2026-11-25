"""Run the strict functional QA benchmark with a local mock and agent-browser.

Usage: python -B tests/browser_benchmark.py [--output-dir PATH]
"""

import argparse
import json
import os
import runpy
import shutil
import subprocess
import sys
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
ROLE = "admin(admin)"
PAGE = "PAGE-001"
FIXTURE = "FIX-001"
CREATE, DELETE = "TC-001", "TC-002"
ROWS = "#table tbody > tr"
FIRST_ROW = f"{ROWS}:first-child"
QTY, PRICE = "4", "2.37"
BASE_COUNT = 2
PRD = """# 物料管理模拟系统的测试物料
管理员登录后可在物料管理页维护本轮测试物料。
从两条预置物料开始，提交一个新名称、正数量和非负单价。重新载入页面并登录后，列表总数增加一条，并显示新物料名称。
新物料金额等于数量乘以单价，四舍五入保留两位小数。
管理员点击该物料行的删除按钮后，重新载入页面并登录时该物料消失，列表恢复为两条预置物料。
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def probe(selector, read):
    return {"selector": selector, "read": read}


def write_plan(directory, url, tenant, marker, fixture_row):
    (directory / "prd.md").write_text(PRD, encoding="utf-8")
    write_json(directory / "prd-scope.json", {
        "document": "prd.md", "revision": "mock-fixture-v1",
        "sections": [
            {"from": 1, "to": 2, "status": "context", "reason": "说明操作角色与页面"},
            {"from": 3, "to": 4, "status": "in_scope", "requirements": ["REQ-001"]},
            {"from": 5, "to": 5, "status": "in_scope", "requirements": ["REQ-002"]},
        ],
    })
    write_json(directory / "pages.json", [{
        "id": PAGE, "status": "confirmed", "entry": "管理员通过首页登录后进入物料管理页",
        "url": url, "role": ROLE, "tenant": tenant,
        "identity": {"role": {"selector": "#me"}, "tenant": {"selector": "h1"}},
        "anchors": ["#table", "#name"], "ready": FIRST_ROW, "busy": "#login",
    }])

    requirements, cases = [], []
    # 金额按 PRD 和输入独立计算。已知缺陷值只用于核对实测。
    amount = Decimal(QTY) * Decimal(PRICE)
    for case_id, req_id, source, action, steps, points in (
        (CREATE, "REQ-001", "prd.md:3-4", "create",
         ["输入本轮唯一名称、数量和单价，点击新增", "重新载入并登录，读取列表"], [
             ("AC-001", "列表总数增加一条", BASE_COUNT + 1, probe(ROWS, "count")),
             ("AC-002", "新物料名称在列表中可见", [marker], probe(f"{fixture_row} td:nth-child(2)", "texts")),
             ("AC-003", "显示金额等于数量乘以单价", [f"{amount:.2f}"],
              probe(f"{fixture_row} td:nth-child(5)", "texts")),
         ]),
        (DELETE, "REQ-002", "prd.md:5", "delete",
         ["点击本轮物料行的删除按钮", "重新载入并登录，复核物料和列表总数"], [
             ("AC-004", "删除后列表总数恢复为两条", BASE_COUNT, probe(ROWS, "count")),
             ("AC-005", "删除后本轮物料行消失", 0, probe(fixture_row, "count")),
         ]),
    ):
        requirements.append({"id": req_id, "source": source, "criteria": [
            {"id": key, "rule": rule, "page": PAGE, "risk": "medium", "scenarios": ["normal"]}
            for key, rule, _, _ in points
        ]})
        cases.append({
            "id": case_id, "req": req_id, "page": PAGE, "scenario": "normal", "steps": steps,
            "operation": action, "fixture": FIXTURE, "result_ready": FIRST_ROW, "verify_reload": True,
            "expected": {key: expected for key, _, expected, _ in points},
            "checks": {key: check for key, _, _, check in points},
        })
        if action == "create":
            cases[-1]["seed_form"] = {"root": "#new-item-form", "fields": {"name": "#name", "quantity": "#qty"}}
    write_json(directory / "requirements.json", requirements)
    (directory / "local-fixture.txt").write_text(
        f"Benchmark-owned in-memory mock at {url}\nSingle instance: {tenant}\nGenerated item: {marker}\n",
        encoding="utf-8",
    )
    write_json(directory / "fixtures.json", {
        "boundary": {"url": url, "environment": "test", "tenant": tenant,
                     "authorization": "benchmark-inventory", "external_effects": "disabled",
                     "actions": ["create", "delete"], "evidence": "local-fixture.txt"},
        "fixtures": [{"id": FIXTURE, "tenant": tenant, "marker": marker, "reference": marker,
                      "ownership": "verified", "source": "generated", "cleanup": "delete",
                      "actions": ["create", "delete"], "evidence": "local-fixture.txt", "page": PAGE,
                      "probe": {"selector": fixture_row, "fields": {"name": probe("td:nth-child(2)", "text")}}}],
    })
    return cases


def run_workflow(output, browser, env, url, tenant, admin_password, *, label, profile):
    defective = label == "defects"
    unsaved = label == "unsaved"
    suffix = uuid4().hex[:12].translate(str.maketrans("0123456789", "ghijklmnop"))
    run_id = f"{label}-{suffix}"
    directory = output / "browser-benchmark" / run_id
    session = f"qa-benchmark-{run_id}"
    version = f"mock-{label}"

    def cli(command, subject, *, code=0, contains="", script=None):
        result = subprocess.run(command, env=env, cwd=ROOT, input=script, capture_output=True,
                                encoding="utf-8", timeout=35)
        detail = (result.stderr or result.stdout).strip().replace(admin_password, "[REDACTED]")
        require(result.returncode == code and contains in detail,
                f"{run_id} {subject}: expected exit {code} {contains!r}, got {result.returncode}: {detail}")
        return result.stdout.strip()

    def qa(action, *args, **expected):
        return cli([sys.executable, "-B", "scripts/qa.py", action, "--dir", str(directory), *args], action, **expected)

    def agent(action, *args, script=None):
        return cli([browser, "--session", session, action, *args], f"browser {action}", script=script)

    def capture(name, phase, *target, **expected):
        cli([sys.executable, "-B", "scripts/qa_browser.py", "--dir", str(directory), "--session", session,
             "--phase", phase, "--output", name, *target], f"capture {name}", **expected)
        if expected.get("code"):
            require(not (directory / name).exists(), f"Rejected capture wrote {name}")
            return None
        return read_json(directory / name)

    def sign_in(wait_for_rows=True):
        agent("wait", "#username")
        agent("fill", "#username", "admin")
        agent("fill", "#password", admin_password)
        agent("click", "#login button")
        if wait_for_rows:
            agent("wait", FIRST_ROW)

    def fixture_action(action, evidence, *target):
        qa(action, "--tenant", tenant, "--evidence", evidence, *target)

    def before(case, state):
        name = f"{case}-before.json"
        capture(name, "before", "--case", case)
        fixture_action("prepare-data", name, "--case", case, "--state", state)

    def after(case, state, defect=None):
        agent("reload")
        sign_in()
        name = f"{case}-after.json"
        measured = capture(name, "after", "--case", case)
        fixture_action("reconcile-data", name, "--case", case, "--state", state)
        if defect:
            qa("defect", "--id", defect, "--severity", "P3", "--case", case,
               "--title", ("Created item did not persist" if unsaved else
                           {CREATE: "Persisted amount is incorrect", DELETE: "Deleted item persists"}[case]))
        qa("record", "--case", case, "--state", "failed" if defect else "passed", "--result", name,
           *(["--defect", defect] if defect else []))
        return measured

    cli([sys.executable, "-B", "scripts/qa.py", "init", "--dir", str(output), "--project", "browser-benchmark",
         "--run-id", run_id, "--mode", "targeted", "--url", url, "--version", version], "init")
    require(read_json(directory / "run.json").get("qa_contract") == 2, "QA init did not enable the strict contract")
    generated = json.loads(cli([sys.executable, "-B", "scripts/qa_seed.py", "prepare", "--dir", str(directory),
                                "--profile", str(profile), "--tenant", tenant, "--operation", "create",
                                "--fixture", FIXTURE, "--recipe", "inventory"], "prepare test data"))
    marker = generated["fixture"]["marker"]
    fixture_row = f'{ROWS}[data-item-name="{marker}"]'
    write_json(directory / "seed-receipt.json", generated)
    cases = write_plan(directory, url, tenant, marker, fixture_row)
    if label == "clean":
        incomplete = dict(cases[0], **{
            key: {point: value for point, value in cases[0][key].items() if point != "AC-003"}
            for key in ("checks", "expected")
        })
        write_json(directory / "test-cases.json", {"cases": [incomplete, cases[1]]})
        qa("freeze-plan", code=1, contains="必测验收点无用例：AC-003")
        require(not (directory / "frozen-plan.json").exists(), "Incomplete plan was frozen")
    write_json(directory / "test-cases.json", {"cases": cases})
    qa("freeze-plan")

    try:
        agent("open", url)
        if label == "clean":
            agent("wait", "#username")
            require(agent("get", "url") == url, "Mock login did not use the planned URL")
            capture("login.json", "page", "--page", PAGE, "--timeout", "1", code=1, contains="未采集到")
        sign_in(wait_for_rows=label != "clean")
        if label == "clean":
            agent("wait", "#me:not(:empty)")
            capture("loading.json", "page", "--page", PAGE, "--timeout", "1", code=1, contains="未采集到")
        capture("smoke.json", "page", "--page", PAGE)
        qa("check-page", "--page", PAGE, "--url", url, "--role", ROLE, "--snapshot", "smoke.json")
        write_json(directory / "preflight.json", {
            "url": url, "version": version, "smoke": "passed", "evidence": "smoke.json",
            "sql": "not_applicable", "sql_note": "The local mock stores all data in memory.",
        })

        before(CREATE, "absent")
        cli([sys.executable, "-B", "scripts/qa_seed_browser.py", "--dir", str(directory),
             "--profile", str(profile), "--recipe", "inventory", "--case", CREATE,
             "--session", session], "fill prepared test data")
        agent("fill", "#price", PRICE)
        agent("click", "#app button[onclick^=add]")
        agent("wait", "#add-msg:not(:empty)")
        added = after(CREATE, "absent" if unsaved else "present", "BUG-001" if defective or unsaved else None)
        require(added["count"] == 0 if unsaved else added["fields"]["name"] == marker,
                f"{run_id}: fixture name did not match the persisted result")
        if defective:
            wrong_amount = f"{Decimal(QTY) * Decimal(PRICE) + 1:.2f}"
            require(added["observed"] == {**cases[0]["expected"], "AC-003": [wrong_amount]},
                    f"{run_id}: expected only the injected calculation mismatch")

        if unsaved:
            require(added["observed"] == {"AC-001": BASE_COUNT, "AC-002": [], "AC-003": []},
                    f"{run_id}: expected a successful save notice but no persisted item")
            qa("record", "--case", DELETE, "--state", "blocked", "--note", "新建记录未持久化，缺少删除对象")
        else:
            before(DELETE, "present")
            # 删除没有完成提示，等待原列表被替换后再独立重载。
            agent("eval", "--stdin", script=f"window.benchmarkPreviousRow = document.querySelector({json.dumps(FIRST_ROW)})")
            agent("click", f"{fixture_row} button")
            agent("wait", "--fn", "window.benchmarkPreviousRow.isConnected === false")
            deleted = after(DELETE, "present" if defective else "absent", "BUG-002" if defective else None)
            if defective:
                require(deleted["fields"]["name"] == marker and deleted["observed"] == {
                    "AC-004": BASE_COUNT + 1, "AC-005": 1,
                }, f"{run_id}: delete did not produce the injected persistence mismatch")
                capture("retained.json", "cleanup", "--fixture", FIXTURE)
                qa("close-data", "--tenant", tenant, "--fixture", FIXTURE, "--evidence", "retained.json",
                   code=1, contains="夹具仍存在")
                # 重置只作用于此处独立启动的内存实例。
                agent("click", "#app button[onclick^=reset]")
                agent("wait", "--state", "hidden", fixture_row)
                agent("reload")
                sign_in()
        capture("cleanup.json", "cleanup", "--fixture", FIXTURE)
        fixture_action("close-data", "cleanup.json", "--fixture", FIXTURE)

        qa("gate", code=1)
        gate = read_json(directory / "gate.json")
        expected_reasons = {f"用例失败未闭环：{case}" for case in (CREATE, DELETE)} if defective else set()
        if unsaved:
            expected_reasons = {f"用例失败未闭环：{CREATE}", f"用例阻塞：{DELETE}"}
        require(gate["test_verdict"] == ("pass" if label == "clean" else "fail")
                and set(gate["test_reasons"]) == expected_reasons
                and gate["release_verdict"] == gate["verdict"] == "fail"
                and any("定向或回归测试" in reason for reason in gate["release_reasons"]),
                f"{run_id}: unexpected gate result {gate}")
        qa("report")
        print(f"{label}: functional={gate['test_verdict']}, release={gate['release_verdict']}, cleanup=verified",
              flush=True)
    finally:
        agent("close")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Keep evidence in this directory instead of a temporary one")
    args = parser.parse_args()
    browser = shutil.which("agent-browser")
    if not browser:
        parser.exit(1, "agent-browser must be installed and on PATH.\n")

    with TemporaryDirectory(prefix="qa-browser-benchmark-") as temporary:
        scratch = Path(temporary)
        config = scratch / "agent-browser.json"
        write_json(config, {})
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("AGENT_BROWSER_")}
        if os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH"):
            env["AGENT_BROWSER_EXECUTABLE_PATH"] = os.environ["AGENT_BROWSER_EXECUTABLE_PATH"]
        env.update(PYTHONIOENCODING="utf-8", AGENT_BROWSER_CONFIG=str(config),
                   AGENT_BROWSER_SOCKET_DIR=str(scratch), AGENT_BROWSER_NO_VERSION_CHECK="1",
                   AGENT_BROWSER_ARGS="--disable-background-networking,--no-proxy-server",
                   AGENT_BROWSER_ALLOWED_DOMAINS=HOST)
        output = args.output_dir.resolve() if args.output_dir else scratch / "evidence"
        for label, defects in (("clean", set()), ("defects", {"calc-wrong", "delete-fake"}), ("unsaved", {"noop"})):
            mock = runpy.run_path(str(ROOT / "mock_app" / "app.py"))
            mock["DEFECTS"].clear()
            mock["DEFECTS"].update(defects)
            class Handler(mock["Handler"]):
                first_list = True

                def do_GET(self):
                    if label == "clean" and self.path.split("?")[0] == "/api/items" and self.first_list:
                        type(self).first_list = False
                        sleep(4)
                    super().do_GET()

            with ThreadingHTTPServer((HOST, 0), Handler) as server:
                host_port = f"{HOST}:{server.server_port}"
                tenant = f"mock-{server.server_port}"
                # 给单实例 mock 的可见标题附加本地监听地址，供租户选择器读取。
                handler_globals = mock["Handler"].do_GET.__globals__
                handler_globals["HTML"] = mock["HTML"].replace("<h1>物料管理系统</h1>", f"<h1>{tenant}</h1>")
                profile = scratch / f"{label}-seed.json"
                write_json(profile, {"seed_contract": 1, "project": "browser-benchmark",
                                     "url": f"http://{host_port}/", "environment": "test", "tenant": tenant,
                                     "authorization": "benchmark-inventory", "external_effects": "disabled",
                                     "actions": ["create", "delete"], "namespace": "inventory",
                                     "recipes": {"inventory": {"actions": ["create", "delete"],
                                                 "reference_field": "name", "fields": {
                                                     "name": {"kind": "marker"},
                                                     "quantity": {"kind": "integer", "min": int(QTY), "max": int(QTY)},
                                                 }}}})
                worker = Thread(target=server.serve_forever)
                worker.start()
                try:
                    run_workflow(output, browser, env, f"http://{host_port}/", tenant,
                                 mock["USERS"]["admin"]["password"], label=label, profile=profile)
                finally:
                    server.shutdown()
                    worker.join()
        if args.output_dir:
            print(f"Evidence saved in {output / 'browser-benchmark'}")


if __name__ == "__main__":
    main()
