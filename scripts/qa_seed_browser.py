"""将本轮预留的合成数据填写到已核验的测试表单。"""
import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from qa_browser import SESSION, read_capture
from qa_contract import text, url_key
from qa_fixtures import load_context, verify_attempt
from qa_plan import verify_frozen
from qa_seed import SeedError, open_seed


# 固定填值动作只接收选择器和值，不点击或提交表单。
FILL = r"""
({page, root, fields, documentStart}) => {
  if (location.href !== new URL(page.url).href || (documentStart !== null && performance.timeOrigin !== documentStart) || !navigator.onLine)
    throw Error('页面文档或执行环境已变更');
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility === 'visible';
  const unique = (scope, selector) => {
    const nodes = Array.from(scope.querySelectorAll(selector)).filter(visible);
    if (nodes.length !== 1) throw Error('表单或控件无法唯一定位');
    return nodes[0];
  };
  const identity = key => unique(document, page.identity[key].selector).innerText.trim();
  if (page.identity && (identity('role') !== page.role || identity('tenant') !== page.tenant))
    throw Error('写入前角色或租户已变更');
  for (const selector of [...page.anchors, page.ready]) unique(document, selector);
  if (page.busy && Array.from(document.querySelectorAll(page.busy)).some(visible))
    throw Error('页面正在处理');
  const form = unique(document, root);
  if (!form.matches('form,[role=dialog]')) throw Error('填值范围不是表单或业务弹窗');
  const targets = Object.entries(fields).map(([selector, value]) => {
    const el = unique(form, selector);
    const target = el.matches('input:not([type]),input[type=text],input[type=tel],input[type=email],input[type=password],input[type=number],textarea,select');
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value')?.set;
    if (!target || !setter || el.matches(':disabled') || el.readOnly)
      throw Error('目标控件类型不支持或不可填写');
    return {el, set: setter, value: String(value)};
  });
  if (new Set(targets.map(item => item.el)).size !== targets.length) throw Error('字段映射重复');
  for (const {el, set, value} of targets) {
    if (!el.isConnected) throw Error('表单重载后需重新定位');
    set.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    if (el.value !== value) throw Error('控件未接受约定输入');
  }
  return {filled:targets.length};
}
"""


def fill_values(session, page, root, fields, document_start):
    """通过 stdin 将私有字段交给已核验的浏览器表单，执行结果只保留数量。"""
    executable = shutil.which("agent-browser")
    if not executable or not SESSION.fullmatch(session):
        raise ValueError("缺少有效浏览器会话或本地驱动")
    payload = json.dumps({"page": page, "root": root, "fields": fields, "documentStart": document_start},
                         ensure_ascii=True)
    browser_env = {key: value for key, value in os.environ.items() if key.upper() != "DYD_QA_SEED_KEY"}
    result = subprocess.run([executable, "--session", session, "--json", "eval", "--stdin"],
                            input=f"({FILL})({payload})", encoding="utf-8", env=browser_env,
                            capture_output=True, timeout=15)
    try:
        if result.returncode == 0 and json.loads(result.stdout)["data"]["result"] == {"filled": len(fields)}:
            return len(fields)
    except (KeyError, TypeError, ValueError):
        pass
    raise ValueError("填值未确认，核对当前页面及未提交表单后再继续")


def fill(directory, profile, recipe, case_id, session, *, login=False):
    def load(name):
        return json.loads((directory / name).read_text(encoding="utf-8"))

    run = load("run.json")
    raw_cases = load("test-cases.json")
    cases = raw_cases["cases"] if isinstance(raw_cases, dict) else raw_cases
    pages = {item["id"]: item for item in load("pages.json")}
    verify_frozen(directory, run, load("requirements.json"), cases, pages)
    case = next((item for item in cases if item["id"] == case_id), None)
    if not case or case.get("operation") != "create" or run.get("qa_contract") != 2:
        raise ValueError("填值需要本轮已冻结的新增用例")
    form = case.get("seed_login" if login else "seed_form")
    if (not isinstance(form, dict) or not text(form.get("root")) or not isinstance(form.get("fields"), dict)
            or not form["fields"] or not all(text(key) and text(value) for key, value in form["fields"].items())
            or len(set(form["fields"].values())) != len(form["fields"])):
        raise ValueError("用例缺少独立、已冻结的表单及字段映射")
    context = load_context(directory)
    boundary, fixtures, ledger = context
    fixture = fixtures.get(case.get("fixture"))
    attempt = ledger["attempts"].get(case_id)
    if (not fixture or fixture.get("source") != "generated" or not attempt or fixture["id"] in ledger["closed"]
            or login != ("after" in attempt)):
        raise ValueError("需要本轮有效的新增夹具及对应操作阶段")
    verify_attempt(directory, run, context, case, attempt, settled=login, pages=pages)
    _, start = read_capture(directory, attempt["before"]["path"])
    key = os.environ.get("DYD_QA_SEED_KEY")
    page = pages[case["page"]]
    document_start = start.get("document_start")
    if login:
        login_origin = url_key(form.get("url"))
        anchors = form.get("anchors")
        if (attempt["after_state"] != "present" or start.get("session") == session or not key
                or not login_origin or login_origin[:3] != url_key(page["url"])[:3]
                or not isinstance(anchors, list) or not all(map(text, anchors)) or len(set(anchors)) < 2
                or not text(form.get("ready"))):
            raise ValueError("新账号登录需已确认的新增结果、独立会话、本域登录表单和测试凭据密钥")
        page = {key: form[key] for key in ("url", "anchors", "ready")}
        document_start = None
    elif (start.get("session") != session or not isinstance(document_start, (float, int))
          or (case.get("seed_login") and not key)):
        raise ValueError("填值需使用登记写入前基线的浏览器文档，并预备登录验证所需密钥")
    with open_seed(directory, profile, fixture["tenant"], recipe, fixture["id"], "create",
                   credential_key=key.encode("utf-8") if key is not None else None) as (receipt, values):
        proposed = receipt["fixture"]
        approved = receipt["boundary"]
        if (any(proposed[key] != fixture.get(key) for key in ("id", "reference", "marker", "tenant", "cleanup"))
                or any(approved.get(key) != boundary.get(key) for key in ("authorization", "external_effects", "test_sink"))
                or url_key(approved["url"])[:3] != url_key(boundary["url"])[:3]
                or not set(fixture["actions"]) <= set(approved["actions"])
                or form["fields"].keys() - values.keys()):
            raise ValueError("夹具回执、业务边界或表单字段与获授权的数据方案不一致")
        fields = {selector: values[name] for name, selector in form["fields"].items()}
        count = fill_values(session, page, form["root"], fields, document_start)
    return {"status": "filled", "fixture": fixture["id"], "field_count": count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("dir", "profile", "recipe", "case", "session"):
        parser.add_argument(f"--{key}", required=True)
    parser.add_argument("--login", action="store_true", help="仅在已核验新增后填写独立会话的测试账号登录表单")
    args = parser.parse_args()
    try:
        print(json.dumps(fill(Path(args.dir), Path(args.profile), args.recipe, args.case, args.session, login=args.login)))
    except SeedError as error:
        raise SystemExit(str(error))
    except (OSError, TypeError, KeyError, ValueError, subprocess.SubprocessError):
        raise SystemExit("填值未完成。核对冻结计划、授权夹具和当前页面，未执行提交。")


if __name__ == "__main__":
    main()
