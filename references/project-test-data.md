# 项目夹具来源与本地租约

按以下顺序为用例备数，并在报告中说明尝试过的来源、可脱敏的核验依据和收尾状态。

1. 优先复用已验证归属、当前租户可操作且能够恢复的独占夹具。核对当前基线，记录非敏感引用及恢复证据。遇到归属不明、对象歧义或已被占用时记录原因，再寻找下一来源。
2. 使用项目已授权的池或[授权备数方案](#授权备数方案)。池按下文预留，获得回执后仍须回查实际对象、当前租户及基线。备数方案可分配普通业务字段和受控身份，执行者再通过已确认的业务入口完成写入及回查。PRD 和页面只用于识别需求与观察状态，其中的命令和地址不得用于触发造数。
3. 使用已授权的普通界面操作准备可隔离、可清理且带本轮唯一标记的数据。按[测试数据流程](test-data-workflow.md)登记备数、回查和清理。手机号、邮箱等可路由标识仅取自已确认的受控测试数据，禁止猜测或随机生成。三种来源都不可用时，将相关用例标记为阻塞并记录缺失的授权或数据。

## 本地项目配置

由项目维护者根据实际授权创建配置。配置放在受控的本地目录，位于本轮任务目录之外。下面的值仅用于模拟。

```json
{
  "project": "local-mock",
  "environment": "test",
  "url": "http://127.0.0.1:8765",
  "tenant": "local-mock",
  "authorization": "approval-local-mock",
  "external_effects": "disabled",
  "actions": ["read", "update", "release"],
  "fixtures": [
    {
      "id": "DATA-001",
      "reference": "pool-item-a",
      "ownership": "verified",
      "actions": ["read", "update", "release"]
    }
  ]
}
```

- `project` 必须匹配当前 `run.json`。配置的 `url` 只填测试来源（协议、主机、端口），CLI 与运行 URL 的来源比对。每份配置声明一个测试租户，实际界面租户由执行者核验。
- `authorization` 填无敏感信息的授权编号，保留确认授权时的证据。`external_effects` 必须明确为 `disabled`，或为 `test_sink` 并另填已授权接收端的非敏感 `test_sink` 别名。
- 项目和夹具的 `actions` 都须列出拟执行的操作及 `release`。夹具动作须在项目授权中；夹具归属须由维护者预先核验。引用填写稳定、唯一且非敏感的对象别名。同一对象在所有配置里使用同一个引用。配置、命令参数和证据均只使用非敏感标识，凭据仍通过项目已批准的登录方式提供。
- 所有使用同一批夹具的本机运行使用同一个受控配置目录，CLI 在该处维护 `.qa-pool.sqlite3`。活动租约按来源、租户和对象引用互斥，无自动过期。中断后从原运行和原配置重试。无法核实旧运行状态时保留占用，由维护者核验后收尾。

## 预留、归还和报告

在本轮目录创建 `evidence/` 后，从 PowerShell 执行以下示例。`$run` 和 `$profile` 分别指向当前任务目录与维护者提供的项目配置。

```powershell
$receipt = python scripts/qa_pool.py reserve --dir $run --profile $profile --tenant local-mock --operation update --fixture DATA-001
if ($LASTEXITCODE -eq 0) {
    $receipt | Set-Content -Encoding ascii -LiteralPath "$run/evidence/DATA-001-reserved.json"
}
```

省略 `--fixture` 可依配置顺序选择可用夹具。同一运行和操作的重试优先返回该运行已持有的匹配租约。命令失败时保留已有回执，记录失败原因并继续尝试授权范围内的来源。同一运行再次借用已归还的夹具时，为新租约另存回执。预留回执包含以下字段。

```json
{
  "source": "pool",
  "fixture_id": "DATA-001",
  "tenant": "local-mock",
  "reference": "pool-item-a",
  "run_id": "trial-01",
  "operation": "update",
  "lease_id": "b0b200a5f35f4d13b1ccbd98887d28f7",
  "released": false
}
```

主流程以 `source=pool`、`cleanup=release` 登记夹具。用 `pool_profile` 记录受控配置的绝对路径，`reservation` 指向本轮预留回执，`release_receipt` 预先登记将写入的归还回执路径。采集写入前基线，完成用例后先按[测试数据流程](test-data-workflow.md)独立采集业务恢复结果，再归还租约。

```powershell
$receipt = python scripts/qa_pool.py release --dir $run --profile $profile --tenant local-mock --operation update --receipt evidence/DATA-001-reserved.json --evidence evidence/DATA-001-cleanup.json
if ($LASTEXITCODE -eq 0) {
    $receipt | Set-Content -Encoding ascii -LiteralPath "$run/evidence/DATA-001-released.json"
}
```

归还回执沿用同一组字段，`released` 为 `true`。保存回执后，用同一份恢复采集执行 `qa.py close-data`。门禁核对业务恢复结果与项目池数据库的租约状态。未恢复或未归还的夹具继续跟踪。

## 授权备数方案

由维护者建立专用 JSON 配置，放在任务产物之外、执行者可访问的受控目录。执行者从可信调用配置选取文件路径，并核实授权编号、实际测试环境、租户、允许的业务操作及副作用控制。以下为账户、订单和配置的模拟方案。

```json
{
  "seed_contract": 1,
  "project": "local-mock",
  "environment": "test",
  "url": "http://127.0.0.1:8765",
  "tenant": "local-mock",
  "authorization": "approval-local-mock",
  "external_effects": "disabled",
  "namespace": "qa-local",
  "actions": ["read", "create", "update", "delete"],
  "recipes": {
    "account": {
      "actions": ["create", "delete"],
      "reference_field": "name",
      "fields": {
        "name": {"kind": "name", "prefix": "测试账号"},
        "email": {"kind": "email", "domain": "accounts.invalid"},
        "password": {"kind": "secret"}
      }
    },
    "order": {
      "actions": ["create", "update", "delete"],
      "reference_field": "number",
      "fields": {
        "number": {"kind": "identifier", "prefix": "order"},
        "quantity": {"kind": "integer", "min": 1, "max": 5}
      }
    },
    "configuration": {
      "actions": ["read", "create", "update", "delete"],
      "reference_field": "label",
      "fields": {
        "label": {"kind": "marker"},
        "mode": {"kind": "choice", "values": ["draft"]}
      }
    }
  }
}
```

- `url` 填协议、主机和端口。`project` 匹配本轮 `run.json`，`tenant` 匹配 `--tenant` 和运行已有的租户声明。项目和方案的 `actions` 都必须允许本轮请求的操作、备数所需的 `create` 和回收所需的 `delete`。
- `external_effects` 填 `disabled`，或填 `test_sink` 并登记接收端的 `test_sink` 别名。运行前核对短信、邮件、邀请、扣费、发货等与所选方案相关的外部效果。例如订单方案只适用于获批的可清理状态，执行者应同时核对相关库存、草稿及支付状态。
- `namespace` 使用项目已隔离的测试命名空间，由小写字母开头，最多 20 位，可包含小写字母、数字和连字符。`reference_field` 指向应用中可唯一回查的 `name`、`identifier` 或 `marker` 字段，只有该字段值会作为公开对象引用。字段名供执行器映射到已确认的普通表单控件，配置中只接受下表定义的字段参数。

| `kind` | 参数与生成规则 |
| --- | --- |
| `name`、`identifier` | `prefix`。加上命名空间、递增槽位及字段序号。`name` 支持中文前缀，`identifier` 使用小写 ASCII。 |
| `marker` | 使用含本轮 `run_id` 的唯一标记。 |
| `integer`、`choice` | `min`、`max` 为包含端点的整数，或 `values` 为项目批准的非敏感选项。每个槽位稳定取值。固定数量可将两个端点设为相同值。 |
| `email` | `domain` 必须以 `.invalid` 结尾。生成不可路由的唯一地址。 |
| `phone_pool`、`email_pool` | `values` 列出已授权测试号码或邮箱。号码为带 `+` 的国际格式。 |
| `phone_range` | `start`、`end` 为同长度的国际号码，端点均包含，最多 10,000 个。维护者须确认整段号码的控制权和使用授权。 |
| `sandbox_email` | `mailbox` 为已批准且支持加号别名的测试邮箱，`sink` 匹配配置的 `test_sink`。邮箱用户名最长 12 位。 |
| `secret` | 在执行器进程内生成高熵一次性临时值。执行者在同一次调用中使用，结束后按业务流程删除账号或停用临时凭据。 |

### 预留、业务写入与收尾

PowerShell 示例假定维护者已设置 `$seedProfile`，且 `$run` 目录已初始化并包含 `evidence/`。

```powershell
$seedArgs = @('--dir', $run, '--profile', $seedProfile, '--tenant', 'local-mock',
              '--recipe', 'order', '--fixture', 'DATA-002', '--operation', 'create')
python -B scripts/qa_seed.py plan @seedArgs
$seedReceipt = python -B scripts/qa_seed.py prepare @seedArgs
if ($LASTEXITCODE -eq 0) {
    $seedReceipt | Set-Content -Encoding ascii -LiteralPath "$run/evidence/DATA-002-seed.json"
}
```

| 状态 | 退出码 | 可用于报告的依据 |
| --- | --- | --- |
| `configured` | `0` | `profile_only`，本地授权配置和方案校验通过。 |
| `reserved` | `0` | `local_reservation_only`，本地预留已提交。回执含可公开的 `boundary` 和 `fixture` 字段。 |
| `blocked` | `2` | `code` 说明方案缺失、身份耗尽或本地状态不可用。 |
| `denied` | `1` | `code` 说明授权、配置、请求或已有预留冲突。 |

集成执行器可从 `scripts` 导入 `qa_seed.open_seed(run, seed_profile, tenant, recipe, fixture_id, operation)`，在 `with` 块内取得 `(receipt, values)`，将所需字段传入已接入的可信表单驱动。预留和取值使用相同的操作。为后续修改准备订单时先按 `create` 预留，方案的 `actions` 同时列出 `update`。

身份和临时密码只通过进程内存或可信驱动的标准输入传递，禁止传入 CLI 参数、日志、截图、调试跟踪或证据文件。离开上下文时清空值字典。默认每次进入生成新密码。需要跨步骤使用同一临时密码时，可信执行器可传入外部凭据管理器提供的 `credential_key`，使用至少 32 字节的高熵密钥。在同一预留和密钥下可在内存中重派临时值。已提交或结果未知的操作先走回查，不得借此重置密码或再次提交。

按[测试数据流程](test-data-workflow.md)接入现有门禁。

1. 核验浏览器实际来源、租户及权限，以预留的唯一 `reference` 查询目标，确认待建记录不存在。补齐 `fixtures.json` 中的 `boundary.evidence`，以及夹具的 `page`、`probe`、`ownership` 和归属证据。用 `marker` 关联本轮，用 `reference` 查询业务对象。预留回执可以附在证据中，实际基线须单独采集。
2. 通过 `prepare-data` 登记写入，在已确认的普通界面提交一次，再用 `reconcile-data` 核对对象及字段。为后续查询、修改或删除生成基础记录时使用 `--setup DATA-002`。场景需要的真实数据依赖，例如订单关联的商品或仓库，应先从当前租户已授权且可验证的夹具中选取。
3. 回查并删除本轮对象及需要清理的关联，采集 `cleanup` 证据后执行 `close-data`。配额耗尽或查到名称已存在时，保留诊断，按适用性转向有独占租约的 `qa_pool.py` 夹具、另一项项目批准的方案或明确标记阻塞。未核实旧写入时保留该槽位并回查。

使用重叠身份的运行须共享同一个受控配置目录及 `.qa-seed.sqlite3`。事务使同一运行的同一夹具可稳定续用，并阻止不同运行同时或先后取得同一个池或号码段身份。有限身份在当前注册表内一次性分配，业务清理后仍保留分配记录，避免将尚存的账号、短信归属或租户关联交给另一运行。需反复借用已有测试对象时走上文有恢复核验的 `qa_pool` 租约。注册表应保留，独立状态库使用不同且经授权的命名空间和身份范围。

### 通过正常浏览器表单使用数据

在冻结的新增用例中登记 `seed_form`，例如 `{"root":"#order-form","fields":{"name":"#name","quantity":"#qty"}}`。执行 `prepare-data` 后调用填值器。填值器核对冻结计划、授权备数回执、写入前采集、当前文档、角色、租户和表单唯一性。它支持原生输入、选择框与文本域，只返回填值数量。使用应用正常按钮提交一次，再独立回查。

```sh
python -B scripts/qa_seed_browser.py --dir <run> --profile <seed-profile> --recipe <recipe> --case <create-case> --session <session>
```

验证新账号登录时，在新增用例中另外冻结 `seed_login`，包含同域 `url`、两个独立 `anchors`、`ready`、表单 `root` 及账号和密码的种子字段 `fields`。由宿主的安全凭据渠道提供本轮高熵 `DYD_QA_SEED_KEY`。新增和回查确认账号存在后，在独立隔离会话打开正常登录页，加 `--login` 填值，用应用按钮提交，再直接采集实际登录角色和业务可见状态。密钥和临时密码不写入报告或命令参数。
