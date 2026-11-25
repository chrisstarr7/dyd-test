# PRD 验收、浏览器采集与发布准入

新版任务由 `qa.py init` 建立。旧报告可按原格式查阅，新一轮验收使用新版采集和门禁。

## 1. 审查原文与测试风险

在任务目录保存授权范围内的 PRD 原文或可复核的 UTF-8 文本导出。保留章节、表格、附件出处与版本。按原文行号在 `prd-scope.json` 记录每段处置。遇到规则、边界、状态、角色、迁移说明矛盾时登记 `pending`，同时记录待确认点。独立沿原文、关键状态流转和角色矩阵复查一次缺口。

```json
{
  "document": "prd.md",
  "revision": "PRD v3",
  "sections": [
    {"from": 1, "to": 1, "status": "context", "reason": "文档标题"},
    {"from": 2, "to": 5, "status": "in_scope", "requirements": ["REQ-003"]},
    {"from": 6, "to": 8, "status": "excluded", "reason": "此次仅测列表和搜索", "decision": "本次用户指定范围"}
  ]
}
```

文件中的有效行必须全部处置，范围不能重叠。将可独立失败的规则拆成独立验收点。用 `risk` 标记 `high`、`medium` 或 `low`，用 `scenarios` 明确哪些场景必须覆盖。按语义选择正常、边界、异常、角色、回归。有计算时在执行前固定公式、来源、单位和精度。高风险验收点只安排一种场景须提供 `risk_note`。非必测验收点需要 `required: false`、`scope_note` 和 `scope_decision`。

`requirements.json` 示例：

```json
[
  {
    "id": "REQ-003",
    "source": "PRD v3 第 2–5 行",
    "criteria": [
      {"id": "AC-031", "page": "PAGE-001", "rule": "姓名查询精确命中", "risk": "medium", "scenarios": ["normal", "negative"]}
    ]
  }
]
```

## 2. 发现页面并冻结计划

沿应用的真实菜单做只读发现。为同一路由上的列表、弹窗、不同角色或租户分别声明可观察的页面状态。优先采用稳定的测试标识或语义控件。用两个不同且各自唯一的 CSS 锚点，再加角色、租户和就绪控件。处理中遮罩可登记 `busy`。就绪条件必须独立于业务断言。过滤、弹窗回填或排序等操作可在用例中加 `result_ready`，指向请求完成后的可见控件。超时或身份不明记阻塞并留证。单租户应用可用已确认的实例标识。

`pages.json` 示例：

```json
[
  {
    "id": "PAGE-001",
    "status": "confirmed",
    "entry": "登录 → 物料列表",
    "url": "http://127.0.0.1:8765/",
    "role": "admin",
    "tenant": "local-mock",
    "anchors": ["[data-testid='list-heading']", "[data-testid='search']"],
    "ready": "[data-testid='list-ready']",
    "busy": "[data-testid='loading']",
    "identity": {"role": {"selector": "#current-role"}, "tenant": {"selector": "#current-tenant"}}
  }
]
```

`test-cases.json` 中每条用例声明 `scenario`（`normal | boundary | negative | role | regression`）、`operation`、逐验收点的预期与对应采集方式。写入用例另填 `fixture`。以下仅展示正常查询，对同一验收点还需登记异常查询用例，预期可见匹配行数为 0。

```json
{
  "cases": [
    {
      "id": "TC-031",
      "req": "REQ-003",
      "page": "PAGE-001",
      "title": "按唯一测试名称查询",
      "scenario": "normal",
      "operation": "read",
      "steps": ["输入已预留的唯一测试名称并提交查询", "等待列表更新并读取匹配行数"],
      "expected": {"AC-031": 1},
      "checks": {"AC-031": {"selector": "tbody tr[data-testid='owned-test-row']", "read": "count"}}
    }
  ]
}
```

`checks` 支持可见节点 `count`、单节点 `visible`、`text`、表单 `value` 和 `texts`。凭据、私人姓名等禁止采集。对隐私字段用脱敏稳定标识、业务状态和数量验证。分页或虚拟列表的可见条数只代表本次渲染，用于存在性和删除断言前，先做唯一标识查询并等筛选完成。验收点和用例默认 `layer: "online_ui"`。仅断网客户端校验的条目同时声明 `layer: "offline_ui"`，执行时另用隔离浏览器断网并拦截请求。后端拒绝、保存和持久化需要各自的在线验收点及刷新回查。

```sh
python scripts/qa.py freeze-plan --dir <run>
python scripts/qa_browser.py --dir <run> --session <browser-session> --page PAGE-001 --phase page --output evidence/smoke.json
python scripts/qa_browser.py --dir <run> --session <browser-session> --case TC-031 --phase assert --output evidence/TC-031.json
python scripts/qa.py record --dir <run> --case TC-031 --state passed --result evidence/TC-031.json
```

使用同一获授权的浏览器会话。在操作前确认页面，在业务动作完成后采集。若逐项比对不符，命令会提示用例计算为失败，按该状态记录。采集文件及索引由脚本写入，每次采集用新路径。冻结后变更需求、风险、预期或页面计划要开启新运行。感知到采集过早时等待真正就绪，重新采集。将失败改判通过须带原因 `--retry-reason observation_invalid|transient|fix_verified` 和 `--note`。偶发且未定位的失败继续阻塞，尝试记录保留在报告。

### 业务判据与空态采集

- 比较多个量时可用 `derive: "sum" | "difference" | "product" | "equal"`，以及 2–8 条 `operands`，每条给出 `selector` 和 `read`。例如用 `difference` 读取总额、可用额和冻结额，计算「总额减去其余各项」，预期为字符串 `"0.00"`。金额可设 `scale` 和 `rounding: "half_up" | "half_even"`。数值输入用可见的标准十进制文本或节点个数，公式须有独立的规则出处。
- 写入用例允许给操作数加 `source: "before"`，其值来自同一用例登记的写入前采集。其他操作数默认取当前采集。例如库存增加量取 `difference`，第一个操作数为当前库存，第二个为基线库存，预期来自预先确定的入库量。可用 `equal` 验证拒绝后数据保持不变。
- `focused` 可验证真实键盘操作后的焦点，`label` 可核对常见 ARIA 或原生标签。已有性能阈值时使用 `{"read":"navigation_ms","metric":"loadEventEnd","max_ms":3000}`，用布尔预期核对单次浏览器导航，并保留原始耗时。也支持 `domContentLoadedEventEnd`。性能容量与完整无障碍覆盖仍各需相应环境、方案和证据。
- 筛选或空结果可能移除列表行内的角色元素。在页面声明 `identity.principal.selector`，指向独立、稳定、能唯一核对登录主体的可见标识。先在不筛选的页面上用 `--phase page` 直接采集角色，再对同一文档内的 `assert`、写入 `before/after` 或 `cleanup` 采集传 `--identity-proof <page证据>`。当前租户和主体仍须可见；重载或切换页面后重新直接采集角色。凭据只证明同一页面、同一文档、同一会话的登录身份，不替代业务结果和权限用例。
- 真实结果不符记失败。资源不足、产品规则未定或工具无法取证记阻塞。需要分类时给 `record --state blocked --note ... --blocker-kind ... --unblock ...`，解除条件写清能验证的前置能力。

## 3. 业务结论、发布凭据与缺陷关闭

报告分别给出本次范围功能结论和正式发布准入。预验收可以继续执行安全且独立的测试。定向或回归模式报告当前范围结论。正式准入另外要求本轮真实冒烟和来自授权发布系统的版本资料：

- `preflight.json` 记录 `smoke: "passed"`、任务的 `url` 和 `version`，以及 `sql: "verified" | "not_applicable"`、`sql_note`、指向页面采集的 `evidence` 和独立的 `release_evidence`。
- 发布证据 JSON 包含 `type: "release_record"`、`source`、同版同环境 `url`、`version`、`deployment_id` 或 `build_id`、`scope: "frontend" | "backend" | "fullstack"`，以及范围内的 `components`（如 `{"frontend":"web-481","backend":"api-217"}`）。
- 发布证据 `migration` 包含 `status` 与 `basis`。已执行迁移另填 `execution_id`。页面快照和前端版本不足以证明后端部署或迁移执行。保留原始发布记录出处，脚本校验本地记录的结构与关联。
- 用 `qa.py defect` 登记缺陷。通过复测时用 `qa.py record --defect <DEFECT-ID>` 关联，然后运行 `qa.py close-defect --dir <run> --id <DEFECT-ID> --fix-version <current-version> --retest-case <case-id>`。P2 书面让步需本版本授权凭据 `type: "defect_waiver"`，并核对来源、决定、缺陷、版本、审批人及理由。

用 `qa.py report` 从同一批证据重算结果。未采集到的数据项、未覆盖的高风险场景和残留夹具继续显示在结论中。
