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
