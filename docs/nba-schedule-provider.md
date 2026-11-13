# NBA 赛程补全与赛前捕获

## 数据源与身份

默认源为 `balldontlie_nba_games`，端点为 `https://api.balldontlie.io/v1/games`。
[提供商文档](https://docs.balldontlie.io/#games)规定 Games 可由 Free 层访问，
`status_state` 是规范生命周期字段，`status` 是展示文字。
该源是**第三方赛程 API**，不是 NBA 官方直连来源。

| 字段 | 用法 |
| --- | --- |
| `id` | 提供商比赛 ID；不改写项目 Canonical ID 或彩票编号 |
| `date` / `season` | 保留原始来源日期和赛季，不冒充北京比赛日 |
| `datetime` | 必须带时区，显式转换为北京时间 |
| `home_team` / `visitor_team` | 按 30 支球队 ID 精确匹配，保持主客方向 |
| `postponed` | 布尔值；延期或畸形标记不能成为待赛证据 |
| `status_state` | `scheduled` / `in_progress` / `final` / `postponed` / `canceled` / `delayed` / `suspended` / `abandoned` / `unknown` |

只有显式 `scheduled` 且当前时钟早于开赛时间的唯一绑定可以补全时间。
缺失/未知/未文档化状态或矛盾文字不能推断为待赛。
仅修改 `time`、`kickoff_time_known`、`kickoff_provenance`；不新增市场比赛，
不改 ID、编号、球队、主客、赔率、状态、比分或模型数学。
溯源使用 `SCHEDULE_PROVIDER_VERIFIED`，**不使用 `OFFICIAL_NBA_VERIFIED`**。
已由本提供商补全的时间不被常规市场时间悄悄覆盖；正常刷新创建新预测时，
这类时间还需本轮完整赛程提供 `scheduled` 绑定，未知状态/停机不生成新预测。
原有不可变快照始终保留。

## 分页与完整性

北京日期窗口同时查询对应日期及前一日的 `dates[]`，每页保持相同过滤条件。
`per_page` 默认 100，允许整数 1–100；跟随 `meta.next_cursor` 直到 `null` 或
已存在 `meta` 对象不含下一游标。缺失/非法 `meta` 不能证明结束。
游标只接受非负整数，拒绝布尔、字符串、负数、浮点和循环游标。

按提供商 `id` 去重，完全相同的重复行可消除，内容冲突直接拒绝整个窗口。
后页失败、解析错误、预算耗尽或冲突一律 `PAGINATION_INCOMPLETE`，
**返回零条可用于补全的观测**。保留观测数量用于诊断，不把 HTTP 200 当成覆盖证明。
失败不填缓存，完整结果保存在进程内 90 秒 TTL 缓存中；命中保留原 `observed_at`
和 `checked_at`，不产生新网络请求。

诊断字段包括 `requested_date_window`（实际请求的来源日期集合）、
`requested_page_size`、`pages_fetched`、`provider_rows_observed`、
`unique_games_observed`、`duplicates_deduplicated`、`conflicting_game_ids`、
`next_cursor_remaining`、`pagination_complete`、`pagination_failure_reason`。
未尝试网络时完整性为 false，且显式给出门禁原因；不能读作空赛程。
正常刷新返回 `nba_schedule_diagnostics` 供运营验收检查。

## 免费层频控与预算

[提供商文档](https://docs.balldontlie.io/#account-tiers)规定 Free 层为每分钟 5 请求。
同一进程使用共享锁对每次请求开始时间限速，默认至少间隔 12.1 秒，避免六次请求
落入同一 60 秒窗口。`DSL_BDL_PACING_INTERVAL` 可调大，不能调小于 12.1 秒。
分页、重试及独立调用共用频控，清空缓存不重置限流状态。

| 边界 | 默认与行为 |
| --- | --- |
| 请求次数 | 每次获取最多 10 次，含后页和重试 |
| 总耗时 | 每次获取 120 秒，包含等待；剩余预算约束 HTTP timeout |
| 单次 HTTP timeout | 默认 5 秒 |
| 429 | 每页最多重试 2 次，指数退避；同样受 12.1 秒间隔约束 |
| `Retry-After` | 支持秒数与 HTTP 日期；若等候超预算，失败退出，禁止提前重试 |
| 401 / 403 | 不重试 |
| 缓存命中 | 网络请求计数为 0 |

429 冷却时间跨调用保留，配额耗尽明确报告 `HTTP_429_RATE_LIMITED`。
网络异常与响应正文不得输出凭据；诊断不回显异常消息。
离线测试注入确定性单调时钟与 sleep，不使用真实多秒等待。

> **频控只在进程内生效**。两个独立 Python 进程可同时发起各自的首请求，
> 不能保证共享账户的全局额度。应由操作者确保同一账户只供一个请求进程使用，
> 或在进程外提供配额协调；本实现不引入分布式调度器。

## 凭据与权限

通过安全环境变量配置 `DSL_BDL_API_KEY`，不得放入源码、报告或日志。
`DSL_ENABLE_NBA_SCHEDULE_NETWORK` 默认 false；`allow_network=False` 默认拒绝网络。
缺少 Key 返回 `disabled_missing_credentials`，不会外发请求；不会回退抓取 ESPN。

| 权限 | 独立含义 |
| --- | --- |
| `PROVIDER_ACCOUNT_ACCESS` | 账户与 API Key 可用；有 Key 本身尚不等于已验证可用 |
| `PROVIDER_TERMS_COMPLIANCE` | 操作者确立提供商使用条款合规 |
| `APPLICATION_NETWORK_PERMISSION` | 应用网络开关与调用许可 |
| `RUNTIME_WRITE_AUTHORIZATION` | 对指定真实运行库执行指定刷新操作的独立批准 |
| `CANONICAL_RECONCILIATION_AUTHORIZATION` | 既有签名治理；不随赛程接入而获得 |

## 隔离验证与真实运行

`tests/test_nba_schedule_completeness.py` 在临时七存储、受控捕获时钟与离线源适配器上
执行真实 `scraper.refresh()`，验证准入/合并、赛程获取、严格绑定、时间补全、
模型预测、不可变快照落盘与重新读取；重复刷新不复制或覆盖快照。
后续合成完赛响应仍走正常刷新，验证既有结算与评估链路。
**合成闭环不是实际 NBA 赛前捕获验收**，不得倒填历史预测。

真实操作前必须独立核对部署代码、有效数据目录、当前北京时间和未来市场目标，
并由操作者明确以下全部批准范围：

```text
repository = https://github.com/YGone-001/Daily-Sports-Lottery.git
deployed_final_SHA = <实际部署并核验的最终 SHA>
absolute_RUNTIME_DATA_DIR = <绝对路径>
one_normal_refresh_only = true
approved_provider = balldontlie_nba_games
permitted_protected_store_write_scope = <明确列举允许写入的存储>
no_automatic_retry = true
no_Canonical_reconciliation = true
```

上述 `no_automatic_retry` 禁止自动重跑整次刷新；提供商级别 429 有界重试需在操作范围中说明。
**开发任务本身不构成真实刷新批准**。无批准记 `NOT_EXECUTED_AUTHORIZATION_REQUIRED`。
有批准也只能执行一次；从实际持久化文件核验 `generated_at < kickoff_at`、
Canonical ID、provider game ID、主客、盘口、快照 ID 和模型版本。
赛果未确认时只可标记 `RESULT_PENDING`；后续真实结算写入须另获授权，
不能用经过时间推断完赛或伪造全场比分。
