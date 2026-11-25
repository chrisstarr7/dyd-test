# 自主准备与回收测试数据

适用于会创建、修改或删除对象的用例，包括保存、审批、状态切换和尝试提交无效表单。只读取、打开表单且不保存的用例登记为 `read`。同一用例如果有多次独立写入，拆成不同用例，逐次核对基线和回查结果。

## 先确定边界，再自主备数

1. 用户要求在指定测试环境执行测试时，该次调用覆盖当前范围内常规用例所需的写入，以及必要的备数和清理。实际写入取用户指令、项目规则与本轮冻结用例的交集。先确认目标环境、租户、角色及外部影响。PRD 用于界定需求和验收，页面用于核实目标。使用更窄的用户限制或项目规则。短信、邮件、邀请、扣费等外部影响须确认禁用或导向获准的测试接收端，已有对象、共享余额及其他跨范围影响须有专项授权。
2. 从可验证归属的已有夹具、[已授权的项目账号池或造数入口](project-test-data.md)、正常界面生成之间依次选择。已有对象必须有创建、预留或明确允许修改的记录。界面中使用本轮唯一标记和最小权限，先核验目标不存在或确属测试用途。只有成功核验的对象才登记为已准备。发现疑似同名或多个匹配时先用只读信息消歧。
3. 规划完整生命周期和回收方式再执行。如员工手机号，分别确认平台身份和本租户成员关系。列表无记录不能证明平台账号不存在。手机号、邮箱等可路由标识只使用受控测试数据，避免尝试随机号码、关联陌生用户或重置已有账号密码。订单、审批等同样核查状态和下游副作用。
4. 如果来源失败，继续尝试授权范围内的下一种来源并记录原因。确认没有安全的数据来源、权限或副作用边界后，提出最小确认项，例如允许使用哪个测试账号池。其余用例继续。保留找不到数据的证据，用例记为 `blocked`。增量 SQL 或不适用依据单独在准入核对，来源为有权限的发布材料。

## 登记范围与夹具

新任务的 `test-cases.json` 每个用例需有 `operation`（`read`、`create`、`update`、`delete`）。写入用例关联 `fixture`，例如 `"operation":"create","fixture":"DATA-001"`。未备妥数据时可以保留计划，用例不得记通过。

在任务目录的 `fixtures.json` 记录本轮实际确认的数据。下例是 `run-id=trial-01` 的本地模拟环境。证据必须为当前任务目录内的非空文件，移除个人数据、凭据及真实联系方式。夹具 `reference` 使用非敏感 ID 或别名；证据说明如何用它识别唯一目标。单机模拟系统没有租户时，使用明确的本地隔离实例标识。

```json
{
  "boundary": {
    "environment": "test",
    "url": "http://127.0.0.1:8765/",
    "tenant": "local-mock",
    "authorization": "允许通过模拟系统界面增改删本轮物料",
    "external_effects": "disabled",
    "actions": ["create", "update", "delete"],
    "evidence": "evidence/data-scope.txt"
  },
  "fixtures": [
    {
      "id": "DATA-001",
      "tenant": "local-mock",
      "page": "PAGE-001",
      "source": "generated",
      "reference": "qa-trial-01-item",
      "marker": "qa-trial-01-item",
      "ownership": "verified",
      "actions": ["create", "update", "delete"],
      "cleanup": "delete",
      "evidence": "evidence/data-ownership.txt",
      "probe": {
        "selector": "[data-testid='qa-trial-01-item']",
        "fields": {"quantity": {"selector": "[data-field='quantity']", "read": "text"}},
        "related": {"draft": "[data-testid='qa-trial-01-draft']"}
      },
      "cleanup_counts": {"draft": 0}
    }
  ]
}
```

`external_effects` 支持 `disabled`（外部副作用禁用）和 `test_sink`（已证明输出到授权测试接收端）。回收方式与来源对应：

| 来源 | 清理 | 需要证明 |
| --- | --- | --- |
| `generated` 本轮创建 | `delete` | 删除本轮唯一标记的可清理对象，并再次查询确认无残留。 |
| `pool` 已预留测试数据 | `release` | 按池的契约恢复并归还，验证预留解除。即使用例未写入也要归还。 |
| `reused` 已验证归属的已有测试夹具 | `restore` | 核对变更前值，恢复该夹具及关联，回查与原状态一致。 |

写入及清理动作都要出现在边界与夹具的 `actions`。只登记本轮会用到的动作，在边界证据中记录本次调用或项目授权来源、涉及的用例编号和副作用核实依据。账号池归还要包含 `release`，已有夹具恢复要包含 `restore`。账号池夹具还要预先登记 `pool_profile`、`reservation` 和 `release_receipt` 路径。执行后不要覆盖已绑定的夹具信息或证据。

`probe.selector` 应以唯一标识定位对象。页面就绪探针等待数据加载。需要恢复的夹具必须采集相关原值。用 `probe.related` 登记可独立核验的关联对象，清理后按预定数量比对，默认应为零。员工场景要分别核对租户成员和平台身份。若后者只能在项目提供的管理入口复核，保留该入口的凭据与回查结果，无法核实时将相应用例标记为阻塞。

## 单次写入、回查与收尾

新版 `probe` 须登记 `key: {"selector": ".object-id", "value": "本轮唯一标识"}` 和独立的 `ready`（结果加载完成控件）。只在确认无分页、截断或虚拟列表时使用 `lookup: "complete"`。筛选场景用 `lookup: "filtered"`，登记 `applied: {"selector": "#applied-filter", "read": "text", "value": "本轮唯一标识"}` 及 `total: {"selector": "#matched-count", "read": "text"}`。采集核对已应用条件和数字总数，当前可见行必须覆盖全部命中。精确匹配后才采集对象字段。工作台显示不同标识时，可登记用于比对的非敏感 `principal_value`。

`cleanup_scope` 写明关联对象。生成夹具通过 `probe.related` 和 `cleanup_counts` 登记需要回收的关联状态，无关联对象时可填空对象并说明依据。清理证据由同一授权主体在重新载入的页面或独立会话采集。若业务用例失败阻塞下游，用 `prepare-data/reconcile-data --cleanup <夹具编号>` 配合 `qa_browser.py --fixture <夹具编号> --phase before/after` 登记补偿删除，再独立采集 `cleanup` 并执行 `close-data`。补偿不计作业务用例通过。

- 修改用例冻结 `effect: {"mode": "change", "field": "display_name", "to": "新名称"}`。字段取自夹具的 `probe.fields`。写入前实测值必须与目标不同，写入后在新文档回查同一字段。预期变化之外的业务断言照常核对。
- 拒绝或幂等用例冻结 `effect: {"mode": "unchanged", "receipt": {"selector": "#result", "text": "应用预期响应"}}` 和预期对象存在状态。基线不得已有同一响应。提交后、刷新回查前执行 `qa_browser.py --phase action --case <用例> ...`，用 `reconcile-data --receipt <响应证据>` 登记。响应须是当前业务操作可观察的明确结果，优先使用该对象的结果标识。按基线比对对象字段与关联状态。响应无法获证时可登记实际回查并继续清理，业务用例保持未验证。

1. 冻结计划后，从同一浏览器会话采集实际页面、角色、租户和唯一对象。登记成功后再执行一次正常界面操作：

   ```sh
   python scripts/qa_browser.py --dir <run> --session <session> --case TC-031 --phase before --output evidence/TC-031-before.json
   python scripts/qa.py prepare-data --dir <run> --case TC-031 --tenant local-mock --state absent --evidence evidence/TC-031-before.json
   ```

   创建通常以 `absent` 为基线，修改和删除以 `present` 为基线。重复新增等异常用例可显式登记 `expected_before: "present"`。保存后的默认预期为创建和修改后存在、删除后不存在。按规则拒绝的用例可显式设置 `expected_after`。持久化场景设置 `verify_reload: true`，提交后重新载入并完成实际回查。
2. 提交后重新读取当前状态并另存证据。核对写入实际是否发生与具体字段变化，即使接口超时或提示成功也必须回查。结果仍不明时保留待回查记录，并将受影响用例标记阻塞。相同用例不得在本轮重复登记；同一夹具的待回查写入也会阻止后续写入。修复后需先回查与清理旧任务，再开启新任务复测。

   ```sh
   python scripts/qa_browser.py --dir <run> --session <session> --case TC-031 --phase after --output evidence/TC-031-after.json
   python scripts/qa.py reconcile-data --dir <run> --case TC-031 --tenant local-mock --state present --evidence evidence/TC-031-after.json
   python scripts/qa.py record --dir <run> --case TC-031 --state passed --result evidence/TC-031-after.json
   ```

   回查状态和功能断言都从同一份采集结果核验。如果仅需为编辑、删除或查询准备对象，在依赖该对象的本轮用例中登记 `fixture: "DATA-001"`，包括只读用例。采集使用 `--fixture DATA-001`，登记和回查使用 `--setup DATA-001`。备数写入计入台账和清理门禁，业务用例和覆盖率按 PRD 计算。
3. 用例结束或中断后，按计划清理本轮对象或归还专用数据，只有通过独立回查才登记完成：

   ```sh
   python scripts/qa_browser.py --dir <run> --session <session> --fixture DATA-001 --phase cleanup --output evidence/DATA-001-cleanup.json
   python scripts/qa.py close-data --dir <run> --fixture DATA-001 --tenant local-mock --evidence evidence/DATA-001-cleanup.json
   ```

   清理失败或结果不明时留证，报告将保持未闭环。不要扩大删除范围或隐去残留。恢复中断任务先读 `fixture-actions.json`，不要根据用例状态猜写入是否发生。

CLI 校验边界登记、流程顺序及浏览器采集中的业务状态。查找数据源和正常 UI 写入由 Agent 执行。任何结果不明的写入先回查。同一任务目录由一个执行者写台账。采集记录用于本地完整性校验，项目授权由实际来源确认。
