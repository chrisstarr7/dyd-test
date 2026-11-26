# 分层执行

先按 [业务风险](enterprise-testing.md) 确认适用层、独立判据与授权，再沿 [验收契约](acceptance-contract.md) 建立运行并冻结。界面、接口、存储和下游分别取证。只观察到上游时，在 `risk-review.json` 的 `blocked_layers` 登记缺失能力及高风险关联。

## HTTP

1. 用 `qa.py init` 建立新运行。API 入口在 `pages.json` 声明 `id: "API-001"`、`layer: "api"`、`status: "confirmed"`、`entry`、`role`、`tenant` 和已授权的同源根地址 `url`。每个关联验收点和用例的 `page` 填该入口，`layer` 均填 `api`。应用主体和租户从已确认身份契约核对，配置里的角色是计划标签。角色验收应独立断言真实主体/组织及业务响应、对象可见性和副作用。
2. 审核原文、预期和风险。将下例中的标识、URL、值和请求改为项目实际受控测试契约，执行 `qa.py freeze-plan --dir <run>`。

```json
{
  "id": "TC-001",
  "req": "REQ-001",
  "page": "API-001",
  "layer": "api",
  "scenario": "normal",
  "operation": "read",
  "steps": ["按测试夹具名称找到物料", "读取物料金额并和 PRD 期望比较"],
  "http": [
    {"method":"GET","path":"/find","query":{"name":"测试物料"},"extract":{"item":"/id"}},
    {"method":"GET","path":"/item/{item}","auth":{"Authorization":"DYD_QA_TEST_ROLE"}}
  ],
  "expected": {"AC-001":200,"AC-002":true,"AC-003":21},
  "checks": {
    "AC-001":{"step":1,"read":"status"},
    "AC-002":{"step":1,"read":"schema","types":{"/amount":"integer"}},
    "AC-003":{"step":1,"read":"field","path":"/amount"}
  }
}
```

3. 从授权凭据渠道注入环境变量（示例 `DYD_QA_TEST_ROLE` 的值为完整认证头）。按身份分别设置环境变量，命令和计划中保留变量名。执行 `qa.py api --dir <run> --case TC-001 --allow-origin <入口根地址> --output evidence/TC-001.json`。

API 步骤索引从零开始。字段用 JSON Pointer；`exists` 可验证字段未暴露。类型契约检查冻结的必需路径与类型，`number` 兼容整数。它是字段类型契约，不支持完整 JSON Schema 词汇。预期来自确认规则与已知输入。变量提取用于链接请求，不能用实际响应生成独立预期。外部地址需 HTTPS，按已授权正常只读业务入口执行 GET/HEAD。HTTP 不跟随重定向，不记录请求凭证、原始响应或自由文本错误体。响应超过 1 MiB 停止取证。不同来源的页面、数据、环境或身份不互为证明。

`api` 返回 0（用例通过）、1（实测不符合）、2（已记录执行阻塞）；计划或授权配置错误会提前拒绝。报告和看板可复算已登记采集文件。注册表可发现普通手写、错用、旧运行及采集修改，信任边界是运行主机和采集器。能同时控制采集文件及登记表的本机进程仍能改写本地证据；需要更高保证时应接入受管 CI 产物和服务端审计。

## 独立数据、并发和回收

SQLite 只读入口限本机测试服务。冻结 `{"read":"sqlite_count","file":"fixtures.sqlite","table":"items","key":"name","value":"fixture-key","within":2}` 核对实际数据库的精确键行数，使用参数绑定，不接受任意 SQL。数据文件应是被测服务在本轮直接使用的测试库，不能复制历史快照来核对当前写入。`within` 为最终一致性最长轮询秒数，默认单查。

仅对拥有独立数据、无真实外发的本机测试服务，在同一用例设置 `operation: "create"`、`local_fixture: {"path":"/fixtures/{fixture}"}`、一个完全相同路径的 POST/PUT 请求及至少一条以 `"{fixture}"` 为值的 SQLite 终态断言。按需传 `json` 或 `form`。SQLite 计数判据来自 PRD。显式使用 `--allow-local-write` 前，确保该临时资源同步创建、支持按同一标识读取和删除，未创建时 GET 返回 404。工具生成夹具键，回查 HTTP 与 SQLite 空基线后登记写入，最后独立验证 GET 及数据行均已清理。

对该创建步骤设置 `parallel: 2`–`8` 执行同步起跑的有限批次。用 `statuses` 比对排序后的响应状态，另核对 SQLite 终态。请求失败、超时或在途写入状态不确定时禁止重放。待项目确认隔离服务已完成在途写入后，带确认依据执行 `qa.py api --dir <run> --case TC-001 --allow-origin <入口根地址> --allow-local-write --recover <终结凭据>`。回收只验证清理。原阻塞仍需新运行复测。

外部 MySQL、MongoDB、Redis、RocketMQ、Qdrant、Elasticsearch、PowerJob 和 Trace 由对应项目确认只读工具、身份、准确的对象或任务标识、环境、时间、构建和原始证据引用。当前没有这些系统的内置执行与核验接口。必要业务断言无法取证时用现有 `blocked_layers` 和阻塞类型；成功 API 响应不能证明外部结果。

## 影响分析与定位

用 `python -B scripts/qa_impact.py --repo <源码目录> --base <基线> --head <候选> --symbol <共享符号>` 列出版本、变更路径和候选引用位置。跟读调用、配置、SQL、事件/定时任务及 Prompt/工具设置，标识已读到的依赖和仍需核实的引用，绑定回归验收点。工具只有 Python/Java 精确词匹配，没有静态解析或动态调用图。基线不可用时从契约和历史缺陷推导，并在报告说明边界。

发生差异时先核对有没有发出业务请求，再检查回执、任务或 Trace ID、最终状态和关联数据。用 `qa.py defect` 的 `--impact` 和 `--hypothesis` 记录业务影响与待验证根因，自动挂接当前已核验失败断言。根因未知或疑似时保留状态；确认因果需要真实日志、受控复现或修复对照。观察不足按发生层和缺失证据写阻塞。

授权责任方确认因果后，在关联通过复测的 `close-defect` 命令提供 `--root-cause-record <本轮JSON>`。记录 `type:"root_cause_confirmation"`、`source`、`by`、`reference`、`run_id`、`version`、`defect_id`、`retest_case`、`finding`、`method:"controlled_reproduction"|"fix_causal"` 及独立脱敏附件 `evidence`。脚本检查本轮绑定和文件完整性，真实因果由提供确认记录的责任方判断。

## 本机性能样本与非交互验收

在 PATH 提供已验证的 k6。运行以下探针前确认 URL 对应本机隔离只读服务。阈值由调用方预先批准，以下只是本机示例：

```powershell
python -B scripts/qa_performance.py --url http://127.0.0.1:8765/ --allow-origin http://127.0.0.1:8765/ --samples 100 --vus 2 --p95-ms 500 --p99-ms 800 --max-error-rate 0
python -B scripts/qa.py gate --dir <run> --scope test
```

性能 CLI 输出实际样本、P95/P99、吞吐和错误率。样本不足或无阈值退出 2；工具缺失、环境未授权或批次未完整同样退出 2 并给出不同状态。可把 JSON 作为专项附件引用。目前性能附件不参与正式发布门禁重算。若性能为必测，仍按风险账本记录未验证项。探针采用 [k6 shared iterations](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/shared-iterations/) 和 [summary](https://grafana.com/docs/k6/latest/results-output/end-of-test/custom-summary/)。

CI 默认运行 `python -B -m unittest discover -s tests -p 'test_*.py'`。在已配好浏览器运行时的安全环境可执行 README 中三个浏览器基准。非交互输出来自原 `gate.json`，`gate --scope test` 退出 0/1/2 对应通过/失败/未完成。现有 `gate` 默认发布口径与非零退出保持兼容。

## 独立基准和经验

1. 让评估方保管独立正负样本和关键风险标签，执行者仅接收原始需求、正常入口与授权。分别执行固定版本的基线和候选，使用不同运行目录及编号。评估方运行 `python -B scripts/qa_benchmark.py --truth <私有标签.json> --baseline <run> --candidate <run>`。标签形如 `{"cohort":"smoke-1","cases":{"TC-001":{"defective":true,"risk":"data","critical":true},"TC-002":{"defective":false,"risk":"data"}}}`。一条有缺陷的真值用例对应一个缺陷统计单元。报告区分误报占比与无缺陷样本误报率。未知成本、自治率、根因真值等显示为 `null`。
2. 真实项目运行在 `qa.py init --project-id <稳定32位小写十六进制项目ID>` 登记身份并在冻结时绑定。经核验的已关闭缺陷可用 `qa_experience.py candidate --run <run> --defect DEF-001 --project-id <相同项目ID> --subject <主题> --conditions <适用条件> --exclusions <反例> --method <方法>` 生成待审 JSON。候选持有证据摘要和原运行/缺陷/复测引用。拒绝本机模拟结果作为正式候选来源。
3. 独立标签用 `candidate_sha256` 绑定候选。审批记录填写 `decision:"approved"`、`by`、`reference`、`candidate_sha256` 和 `comparison_sha256`。摘要使用 `qa_contract.digest` 对 JSON 对象计算。运行 `qa_experience.py approve --proposal <json> --decision <json> --truth <json> --baseline <run> --challenger <run>` 重新计算结果并输出已审批记录。审批真实性依赖组织的可信审批与仓库权限，CLI 检查内容绑定。
4. 由维护者评审后把记录作为数组成员保存到项目治理仓库 `qa-experience/<项目ID>.json` 并受审提交。`qa_experience.py recall --repo <治理仓库> --ref <已批准提交> --project-id <ID> --version <当前适用版本> --query <相关关键词>` 读取固定提交。当前 PRD 已有同主题规则时使用 `--overridden <主题>`，同范围有冲突只显示主题。回滚选此前已批准提交，项目事实或历史缺陷建议可由维护者按同样的准确版本和出处登记。经验只能影响测试设计，不写验收点、评分器或主 Skill。
