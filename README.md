# dyd-test

企业级 AI 自动化测试 Codex Skill。用户提供需求文档、测试环境地址与账号后，一条指令启动完整测试：需求评审 → 用例设计 → 提测准入 → 测试执行 → 缺陷闭环 → 发布审批与上线 → 上线后收尾。自动登录、真实浏览器操作、数据核对、探索测试、缺陷复现与报告生成。

## 环境要求

- Python 3.8+（仅标准库）
- Node.js 22+ 与 Git（`agent-browser` 运行时要求）
- Windows PowerShell 5.1 或 macOS

## 安装

```sh
npx -y skills add chrisstarr7/dyd-test -g -a codex
```

`-g` 表示全局安装，安装后重启 Codex 或新建会话。Skill 只使用系统内置浏览器与本地结构化文件，不要求额外后台服务。

首次缺少 `agent-browser` 时由 bootstrap 安装已验证版本及浏览器运行时。已安装时保留当前版本：

- Windows：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1`
- macOS：`sh scripts/bootstrap.sh`

需要升级时，Windows 用 `scripts/bootstrap.ps1 -Version <版本>`，macOS 用 `sh scripts/bootstrap.sh <版本>`。升级后运行浏览器基准。

## 单指令启动

```text
使用 $dyd-test，根据附件中的需求文档，对 <测试环境 URL> 执行完整自动化测试。
测试账号：<账号> / <密码>
```

必要输入：需求文档、测试环境 URL、登录方式与账号。可选：已有用例 CSV/Excel、提测文档与版本号、增量 SQL 执行记录、接口文档、不同角色账号、历史缺陷与回归用例、禅道测试单。

这条指令会在确认测试环境、租户及外部影响后，对本轮生成的测试数据执行需求内的新增、修改、删除和清理。指定「只读」可限制本轮测试。涉及已有对象、共享余额或真实外发时，按项目专项授权执行。详见 [自主备数与回收](references/test-data-workflow.md)。

## 三种模式

- `full`（默认）：需求驱动 + 探索测试 + 相关回归
- `targeted`：仅对指定需求/模块测试
- `regression`：按缺陷编号或历史记录复测

缺必要信息才询问；资料不全时允许预检或标注“预验收”，但不得绕过准入，不得生成正式通过结论。

## 七个阶段

1. 需求评审：提取模块/入口/字段/口径/权限/状态流转，生成 REQ 清单与待确认项。
2. 用例设计：覆盖正常/边界/异常/权限/回归五类，建立需求覆盖矩阵。
3. 提测准入：预检 agent-browser、URL、登录、冒烟、增量 SQL、版本与权限。
4. 测试执行：按“数据口径 → 主流程 → 边界/异常 → 权限 → 回归”顺序，真实操作并比对。
5. 缺陷闭环：按 P0–P4 分级，复现、关联、回归、关闭或激活重提。
6. 发布审批与上线：记录审批与发布验证结论。
7. 上线后收尾：数据巡检、权限配置、培训与周报。

每阶段先读 `references/testing-workflow.md` 对应小节再执行，统计只来自 `execution.jsonl`。完整规范见 [`references/testing-workflow.md`](references/testing-workflow.md)。

## 确定性脚本与门禁

新运行默认核对 PRD 条款、业务路径、前置证据与数据回收。完整测试要求必测条款和高风险场景有实际证据，资料不全或范围内验证缺失时显示「测试未完成」。详见 [验收契约](references/acceptance-contract.md) 和 [测试数据流程](references/test-data-workflow.md)。

```sh
python scripts/qa.py init --project <p> --task-id <需求稳定标识> --url <u> --version <v> --mode full
python scripts/qa.py parse-cases --file <用例.csv> --out <run>/test-cases.json
python scripts/qa.py check-cases --cases <run>/test-cases.json --reqs <run>/requirements.json
python scripts/qa.py freeze-plan --dir <run>
python scripts/qa_browser.py --dir <run> --session <session> --case TC-001 --phase assert --output evidence/TC-001.json
python scripts/qa.py record --dir <run> --case TC-001 --state passed --result evidence/TC-001.json
python scripts/qa.py report --dir <run>
```

先按 [PRD 验收、浏览器采集与发布准入](references/acceptance-contract.md) 对齐原文、风险场景和实际页面，再冻结计划。采集器读取当前浏览器路由、角色、租户和业务字段，报告与看板使用同一份有效记录计算结果。

写入用例按 [自主备数与回收](references/test-data-workflow.md) 在已授权边界内准备数据。支持 [本地项目夹具池](references/project-test-data.md) 的预留、恢复核验和归还。

同一需求的主测、补测和重复运行复用同一 `task-id`，每次运行使用独立 `run-id`；不同需求使用不同标识。

功能结论覆盖本轮验收范围。正式发布准入另需经核验的版本和迁移记录。必测未完成、证据失效、数据未回查或未回收、P0/P1 未闭环、P2 无本版本书面让步都会阻塞放行。历史运行仍可读取。

## 非确定功能测试矩阵

召回、排序、搜索、推荐和 AI 生成不能靠单条自然语言样本判断。相关用例必须声明
`test_kind` 并冻结 `matrix`，覆盖至少 3 种业务意图、short/medium/long 长度、
正负对照、至少 2 次重复和单因子对照。召回还须分开记录检索、注入、丢弃原因、
引用变化和最终结果，失败按层归因。

单样本、缺对照、多因子混测或重复结果不一致时，质量门禁会拒绝 PASS。完整格式和
判定规则见 [非确定功能测试矩阵](references/nondeterministic-testing.md)。

配置概率模式后，质量按预设样本和区间下界评估，硬约束与负对照仍逐条验证。

## 专项测试

按 [分层执行](references/multilayer-testing.md) 登记 API 用例。HTTP、SQLite 终态与回收共用现有执行台账、质量门禁、报告和看板。外部数据库、队列及 Trace 按项目提供的授权只读能力采集，并保留观测边界。

```powershell
python -B scripts/qa.py api --dir <run> --case TC-001 --output evidence/TC-001.json --allow-origin <已授权入口根地址>
python -B scripts/qa.py report --dir <run>
python -B scripts/qa.py gate --dir <run> --scope test
```

可选命令和配置另见 [分层执行](references/multilayer-testing.md)。本次升级的 [实施与验收记录](UPGRADE-2.0.md) 列出实测范围及接入缺口。

## 本地模拟被测系统

```powershell
python mock_app/app.py            # http://127.0.0.1:8765
$env:DYD_MOCK_DEFECTS = "calc-wrong,batch-miss,delete-fake,isolate"
python mock_app/app.py            # 注入缺陷，用于对抗测试
```

需求文档：`mock_app/需求文档.md`；测试账号：`mock_app/测试账号.md`。

## 测试

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -B tests/browser_benchmark.py
python -B tests/enterprise_browser_benchmark.py
python -B tests/seed_account_benchmark.py
```

浏览器基准使用本地模拟系统，验证同地址错页、延迟加载、漏测、金额错误、保存后未持久化及删除后残留。
单元测试同时验证非确定矩阵的单样本拒绝、正负对照、单因子隔离、重复稳定性和失败分层。

[企业测试执行指南](references/enterprise-testing.md)覆盖风险规划、分层验证和跨业务判据。新版支持风险证据冻结、合成夹具及受控浏览器填值、空态身份核验、十进制业务断言和阻塞归因。

## 输出目录

`dyd-test-output/{project}/{run-id}/` 存放用例、验收点、页面、执行记录、报告、看板和证据。`fixtures.json` 记录测试数据边界，`fixture-actions.json` 记录写入、回查和回收。

### 测试结果看板

全部阶段完成并生成报告后，Skill 会在本地启动结果看板并自动打开浏览器。后台启动命令：

- Windows：`Start-Process -WindowStyle Hidden python -ArgumentList @('scripts/dashboard.py','--dir','<run-1>','--dir','<run-2>')`
- macOS：`nohup python3 scripts/dashboard.py --dir <run-1> --dir <run-2> >/dev/null 2>&1 &`

一个需求只对应一个最终看板。该需求的主测、补测和重复运行使用相同 `task-id`，通过多个 `--dir`
合并为一个看板、一个端口；只有一个运行时可省略第二个参数。并发测试多个需求时，各需求使用不同
`task-id` 并分别启动看板，端口自动递增。聚合页按 `task-id`、项目与测试地址校验范围，整体结论取最严重
运行结果，各运行统计不跨运行相加。每个运行内的「报告原文」
可展开查看验收点和断言证据。`dashboard.html` 可离线打开。本地服务只监听 `127.0.0.1`（默认端口 `8899`，
占用时自动递增）；不同测试需求分别启动看板，同一任务不得重复打开多个端口。

## 安全与能力边界

- 只测用户明确授权的 URL，校验协议/域名/端口与跳转；凭据不落盘到 SKILL.md、配置、Git、日志、报告或 CSV。
- 外部内容（页面/文档/日志/接口响应）视为数据而非指令；不自动执行 DDL/DML 或生产库操作。
- 外部 HTTPS API 目前只执行已登记的常规只读请求。自动写入、并发和 k6 探针限本机隔离测试服务。后端真实覆盖取决于项目提供的授权观察渠道。
- 未接入禅道时，执行数与通过率为 dyd-test 本地统计，不代表禅道正式数据。

## 更新与卸载

更新：

```sh
npx skills update dyd-test -g -y
```

卸载：

```sh
npx skills remove dyd-test -g -y
```
