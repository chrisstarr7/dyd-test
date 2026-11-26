# 非确定功能测试矩阵

召回、排序、搜索、推荐和 AI 生成的结果会随措辞、上下文预算、数据负载与模型决策变化。
一条自然语言指令成功或失败，只能证明该次采样，不能直接推出整个功能可用或不可用。

这类用例必须声明 `test_kind`，并使用可复核的查询矩阵。没有矩阵、矩阵不完整或重复结果
不稳定时，`qa.py` 不会允许该用例通过。

## 适用类型

- `recall`：记忆、知识、上下文召回
- `ranking`：排序、优先级、推荐位
- `search`：搜索命中、相关性、过滤
- `generative`：AI 生成、改写、总结及结构化输出

普通确定性页面、表单和接口校验不声明 `test_kind`。

## 触发条件适用性

召回测试前先确认产品定义的触发条件，并把“是否适用”与“召回结果”分开记录：

- 记忆抽取/保存触发不等于生成前召回触发。不要把抽取触发表当成召回规则。
- 只有实际进入目标阶段的请求才要求召回。澄清、闲聊、能力边界拒绝和非生成消息
  不能作为召回失败证据。
- 正向对照前先确认目标对象确实存在可召回数据，并记录对象、版本和数据状态。
- 负向对照必须是“按产品规则不应触发检索”的请求，而不是“主题无关但仍会触发
  检索”的生成请求。

只验触发差异时可声明 `matrix.objective: "trigger"`。至少安排触发和不触发两种意图、
各重复至少两次，每条用例固定 `search_expected: true | false`。短长对照用于验证长度影响，
与本轮规定的触发差异无关时可省略。零命中不能证明没有检索。

只有浏览器页面时用 `matrix.recall_evidence: "display"`，用例声明与 `pages.json` 一致的
`recall_url`。每个样本引用独立的已登记采集文件，绑定运行、构建、页面、查询、重复序号、
采样时间、实际展示和实际执行标识。页面有明确检索状态时填 `search_invoked`。页面无信号时
记录无法判定，避免填作 false。此模式不支持用 UI 数据替填检索、注入或引用计数证据。

用已登录的浏览器读取本次消息实际展示，可复用 `qa_browser.write_capture` 登记脱敏
JSON。文件至少包含 `source:"agent-browser"`、`run_id`、`version`、`case_id`、
`query_id`、`repeat`、`observed_at`、`url`、`execution_id`、`actual` 和
`search_invoked`。只出现“记忆召回”“思考中”等过程占位时继续等待，直到最终回复或
明确终态可见。生成阶段未成立的样本可留作诊断，不因此判为生产功能失败。
- 证据同时记录 `trigger_applicable`、`trigger_met`、`search_invoked` 和
  `retrieved`；未适用时结论写“不适用”，不写“未召回”。

## 用例声明

```json
{
  "id": "TC-010",
  "test_kind": "recall",
  "matrix": {
    "evidence": "evidence/TC-010-matrix.json",
    "repeats": 2,
    "environment": {
      "id": "ENV-001",
      "url": "https://example.test/project/P-001",
      "principal_ref": "QA-ROLE-A",
      "tenant": "TENANT-TEST",
      "version": "BUILD-20261125",
      "data_state": "project-fact=1,personal-fact=2,index=warm"
    },
    "controls": {
      "positive": ["Q-001"],
      "negative": ["Q-004"]
    },
    "comparisons": [
      {"base": "Q-001", "variant": "Q-003", "factor": "prompt_length"}
    ],
    "queries": [
      {
        "id": "Q-001",
        "role": "positive",
        "prompt": "写一条产品脚本，突出卖点",
        "factors": {
          "intent": "script",
          "prompt_length": "short",
          "personal_load": "2",
          "theme": "keyboard"
        },
        "expect": {"project": {"min": 1}},
        "citation_min": 1
      },
      {
        "id": "Q-002",
        "role": "sample",
        "prompt": "整理一段产品培训提纲，列出三个要点",
        "factors": {
          "intent": "training",
          "prompt_length": "medium",
          "personal_load": "2",
          "theme": "keyboard"
        },
        "expect": {"project": 0}
      },
      {
        "id": "Q-003",
        "role": "sample",
        "prompt": "写一条30秒产品口播脚本，主题：日常场景，明确人群、渠道和结尾要求。",
        "factors": {
          "intent": "script",
          "prompt_length": "long",
          "personal_load": "2",
          "theme": "keyboard"
        },
        "expect": {"project": {"min": 1}},
        "citation_min": 1
      },
      {
        "id": "Q-004",
        "role": "negative",
        "prompt": "解释报销发票的验真流程",
        "factors": {
          "intent": "finance",
          "prompt_length": "short",
          "personal_load": "2",
          "theme": "invoice"
        },
        "expect": {"project": 0}
      }
    ]
  }
}
```

`environment` 必须固定本次账号身份、租户、版本、入口和可核对的数据状态。不得把密码、
Token 或 Cookie 写入矩阵。`principal_ref` 使用角色或不透明编号。

`expect` 比较冻结期望中声明的指标。整数表示精确值，`{"min": n}` 和 `{"max": n}`
表示计数边界。其他 JSON 值按规范化内容比较。

## 最低覆盖

- 至少 `2` 次重复；结果随模型或缓存变化时应提高到 `3` 次以上。
- 至少 `1` 个正向对照和 `1` 个负向对照。
- 至少 `3` 种业务意图。
- 同时覆盖 `short`、`medium`、`long` 三种指令长度。
- 至少一组只改变 `prompt_length` 的 short/long 对照。
- 对照项的前后查询必须有相同因子集合，且只能有一个因子不同。
- 每条查询保留原始 prompt、业务意图、长度、对象负载和主题。
- 功能受 token、top-k 或上下文预算影响时，另建固定数据的空载、基线、超载矩阵；
  不同负载不得混在同一矩阵中再跨样本比较。

业务意图、长度、负载或账号发生变化时，不得把结果混成同一个样本结论。

## 召回证据

`recall` 用例每次采样还必须记录：

下面只展示一条观测，实际每条查询必须按 `repeats` 完整重复。

```json
{
  "case_id": "TC-010",
  "environment": {
    "id": "ENV-001",
    "url": "https://example.test/project/P-001",
    "principal_ref": "QA-ROLE-A",
    "tenant": "TENANT-TEST",
    "version": "BUILD-20261125",
    "data_state": "project-fact=1,personal-fact=2,index=warm"
  },
  "queries": [
    {
      "query_id": "Q-001",
      "repeat": 1,
      "search_invoked": true,
      "retrieved": {"project": 1, "personal": 2},
      "injected": {"project": 1, "personal": 2},
      "dropped": {},
      "citation_delta": 1,
      "actual": {"project": 1},
      "observed_at": "2026-11-25T10:30:00+08:00",
      "evidence": "evidence/recall/Q-001-r1.json"
    }
  ]
}
```

必须区分以下层次，不能把“没有显示”直接归为“检索不到”：

1. `decision`：模型或流程没有发起检索。
2. `retrieval`：检索层没有返回目标内容。
3. `injection`：已检索到，但被排序、top-k 或 token 预算挤出。
4. `citation`：已注入并展示，但引用次数或使用记录未更新。
5. `assertion`：前三层均正常，最终展示或业务断言不一致。

`retrieved[group] > injected[group]` 时，`dropped` 必须写明丢弃原因，例如
`{"project": "token_budget"}`。`actual` 中参与判定的召回组数量必须与 `injected`
一致，防止手工填写与上下文证据相互矛盾。

## 判定规则

旧矩阵保持逐样本匹配。评估概率质量时，在冻结的 `matrix` 增加：

```json
{
  "probability": {
    "min_samples": 20,
    "min_lower_bound": 0.75,
    "grader": "human-or-independent-grader-id",
    "rubric": "rubric-v1",
    "labels": "held-out-labels-v1",
    "label_file": "evidence/labels.json"
  }
}
```

`repeats` 至少等于 `min_samples`（示例参数须按项目实际预设）。每个查询另登记 `hard_expect`，
例如 `{"schema_valid":true,"tenant_match":true}`；每条样本 `actual` 同时提供质量观测、硬约束值和所需召回分层记录。
样本加唯一 `sample_id`、`grade_status:"scored"`、与计划一致的 `grader`、`rubric`、`labels` 及独立证据路径。
逐样本 `evidence` 指向评分 JSON，包含 `sample_id`、`query_id`、`run_id`、`case_id`、`grader`、`rubric`、
`labels_sha256`（冻结标注文件哈希）、`measurement_sha256`（用 `qa_contract.digest(actual)` 计算）、
`artifact`（实际脱敏样本路径）和 `artifact_sha256`。重复引用同一样本、修改标注或样本原件会拒绝。
原件为采集方产生的 JSON，含 `source`、`run_id`、`query_id`、`sample_id` 和独立的
`execution_id`（例如同租户真实 Trace 标识），同批不得重复使用执行标识。来源真实性仍取决于采集方权限及审计。
逐查询计算二元质量判据符合数和单侧 95% Wilson 下界。合理的措辞变化可以通过，硬约束与负对照任何一次违规则失败；
样本缺失、Judge 不可用或标注无法关联视为证据不足。置信下界依赖独立采样；同一对话的重复答案、缓存复用与转录均不可当成独立样本。
当前脚本复核评分记录和冻结口径；采集与标注工作须由实际执行者或独立评估方完成，浏览器展示不能替代 Trace。

- 任何一条查询的重复结果不一致：标记 `flaky`，不得判通过。
- 正向对照全部未命中：功能在受控条件下不可用，用例失败。
- 负向对照出现命中：存在误召回，用例失败。
- 矩阵完整但产品缺陷仍存在：记录失败和缺陷，不得用“矩阵已执行”代替通过。
- 证据缺失、环境变化或版本不一致：判为未完成或阻塞，不得猜测通过。
- 只有单次采样：直接拒绝 PASS。

## 缺陷归因

- 同一意图短指令可命中、长指令稳定不命中：优先怀疑排序或上下文预算，定级通常为 P1，
  不是核心功能完全不可用的 P0。
- 更换查询意图后仍完全不命中，且搜索未触发或检索层为空：再评估是否为 P0/P1。
- 命中结果只差展示或引用计数：分别归到展示层或引用层，不扩大为召回失败。
- 一次命中、一次不命中：先记录 `flaky` 和采样条件，确认复现率后再定级。

## 执行前检查

1. 账号、租户、项目、版本、索引状态和已有数据量固定。
2. 正向对照的短指令在正式矩阵前至少命中一次；不命中先查环境或备数，不继续扩大样本。
3. 负向对照使用明确无关的业务主题。
4. 每条查询按相同步骤重复，使用独立对话或明确重置上下文。
5. 保存原始 prompt、召回分组、条数、注入结果、引用变化、截图或结构化响应。

## 结论写法

报告只写矩阵实际证明的范围，例如：

> 在固定账号、项目和 2 条个人记忆负载下，短指令稳定召回 1 条项目记忆；
> 长指令 2 次均未召回，未发现检索层返回后被丢弃的日志，因此判定为条件性召回问题，
> 影响范围限于长指令和当前上下文负载。

不得写“项目记忆完全不可用”或“项目记忆测试通过”，因为这两句话都超出了矩阵证据。
