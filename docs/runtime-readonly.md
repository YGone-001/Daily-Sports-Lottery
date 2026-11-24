# 安全只读启动与显式刷新

## 默认启动

应用默认 `RUNTIME_WRITES_ENABLED=false`、`BACKGROUND_REFRESH_ENABLED=false`、
`LOCAL_UI_REFRESH_ENABLED=false`，提供商网络默认关闭（`DSL_ENABLE_NBA_SCHEDULE_NETWORK`
只认显式环境变量，不再从本地 UI 开关或凭据存在推导）。
必须另获操作者启动许可才可在真实主机运行 `python app.py`；本文不是启动授权。
安全只读配置使用 `DSL_RUNTIME_WRITES_ENABLED=false`、`DSL_BACKGROUND_REFRESH_ENABLED=false`、
`DSL_LOCAL_UI_REFRESH_ENABLED=false`、`DSL_ENABLE_NBA_SCHEDULE_NETWORK=false`。
DEBUG=False 或 Werkzeug 子进程都不会自动获得写权限。

## 启动脚本与显式激活

`start.bat` / `start.sh` 默认只读：二者都会把主写开关与本地界面开关**显式设为 false**，
不会因为「启动了应用」而静默开启生产写入。只有显式 `start.bat --refresh` 或
`./start.sh --refresh` 才为**本次启动**同时开启这两个开关；未知参数只提示、不激活。

> `--refresh` 开启的是**持续到该应用进程结束**的写能力，**不是**一次性生产批准。
> 该进程内每一次 `POST /api/refresh` 仍是一次独立的真实刷新，需要操作者当下的明确授权；
> 关闭进程或重新以只读方式启动即撤销该能力。不得把「上次用 `--refresh` 启动过」
> 当作本轮刷新的批准。

首次缺少 `data/daily_matches.json` 时，脚本只有在显式 `--refresh` 下才执行
`python -m utils.scraper`；只读启动会打印可操作提示并以非零退出，不会自动抓取。
脚本从不设置 `DSL_BACKGROUND_REFRESH_ENABLED`；后台刷新仍须单独显式配置。

## 操作者可见的运行状态

后端 `/api/status.runtime` 是权威来源（前端按钮与请求头都不是授权边界），暴露
`mode`（`READ_ONLY_APPLICATION` / `LOCAL_REFRESH_AVAILABLE` / `TOKEN_REQUIRED`）、
`operating_state` 与 `last_refresh`（状态、代号、起止时间、错误、标记错误）。

| `operating_state` | 来源 |
| --- | --- |
| `READ_ONLY_APPLICATION` | 主写开关关闭 |
| `LOCAL_REFRESH_AVAILABLE` | 主写 + 本地界面开关同时开启 |
| `TOKEN_REQUIRED` | 主写开启但本地界面同步关闭（程序化调用需令牌） |
| `REFRESH_IN_PROGRESS` / `REFRESH_COMPLETED` / `REFRESH_FAILED` | 刷新代标记（`.refresh_state.json`）的当前状态 |

刷新标记是**顾问性观测**：`REFRESH_COMPLETED` 不代表七个库是事务快照；所有状态都不替代
操作者批准。界面在 `REFRESH_IN_PROGRESS` 期间轮询状态，完成后自动更新。

## GET 与既有数据

所有 GET/HEAD 页面和 API 仅呈现数据、计算临时视图，不再调用 `ensure_snapshot()`。
球队档案查询在请求局部的只读上下文内估算，既不写盘，也不污染共享球队缓存；公式不变。
`/api/match/<id>/snapshots` 保持读取已有不可变预测。历史结算、评估及赔率历史保持可见。
`GET /api/refresh` 返回 405；OPTIONS 不执行刷新。

## 显式 POST

`POST /api/refresh` 默认拒绝。**主写开关是总门**：`DSL_RUNTIME_WRITES_ENABLED` 未显式为 true 时，
任何请求（包括本地回环 + 浏览器头）都返回 403 `RUNTIME_WRITES_DISABLED`——
回环地址与 `X-Requested-With` / `Sec-Fetch-Site` / Referer / Origin 等浏览器可伪造头不构成操作者授权。

主写开关开启后仍分两路：

| 通道 | 要求 | 拒绝码 |
| --- | --- | --- |
| 程序化调用（默认通道） | 服务端非空 `DSL_RUNTIME_WRITE_TOKEN` 且请求头 `X-Refresh-Token` 恒定时间匹配 | 403 `REFRESH_AUTHORIZATION_REQUIRED` |
| 本地 Web UI 通道（免令牌） | `DSL_LOCAL_UI_REFRESH_ENABLED=true` **且**主写开关开启**且**回环地址**且**同源上下文头**，四者同时成立 | 不满足任一即退回令牌校验 |

缺写开关或令牌均返回 403，忙时仍返回 409。没有令牌回显，不把令牌放进 URL、报告、源码或浏览器构建产物。
授权操作由操作者经受控客户端携带请求头执行。配置写能力**不等于**对真实运行库操作的独立批准。
后台刷新还必须显式启用 `DSL_BACKGROUND_REFRESH_ENABLED=true`，默认保持关闭；reloader 判定仅防重复线程。

## 赛果验证等级（产品语言）

运行状态与赛果验证是两个维度。赛果语言只允许以下三档，**不得**互相代称：

| 等级 | 含义 | 来源 |
| --- | --- | --- |
| `SOURCE_REPORTED_FINAL` | 单一即时源报告终场（对应 `SOURCE_RETURNED_CONFIRMED_FINAL_RESULT`） | `live_basketball` 即时列表 |
| `INDEPENDENT_OFFICIAL_RESULT_VERIFIED` | 独立核验的终场 | NBA 官方页 + BDL 严格双源核对（`tools.nba_confirmed_result`） |
| `FULL_TWO_SOURCE_RESULT_VERIFIED` | 独立核验之外，彩票源终场比分也一致 | 上述双源 + 同一比分 |

`SOURCE_RETURNED_CONFIRMED_FINAL_RESULT` **不是**独立官方核验，**不得**用它替代
`INDEPENDENT_OFFICIAL_RESULT_VERIFIED`，也**不得**据此绕过 preview / execute 两段批准。

## 一次正常刷新的影响

正常 refresh 不是单场 NBA 更新，可能覆盖所有源、运动与合格市场比赛：

| 存储 | 可能影响 |
| --- | --- |
| daily_matches.json | 市场准入、身份预检、合并、时间补全、刷新元数据 |
| prediction_snapshots.json | 所有合格未开赛比赛新增不可变预测 |
| odds_snapshots.json | 合格赛前市场变化追加观察 |
| team_strength.json / calibrated.json | 新球队初始化、已确认完赛结果校准及去重登记 |
| settlements.json / evaluation_rows.json | 既有赛前预测与真实完赛结果结算、评估 |

另有刷新锁、快照锁与代标记。七文件不是事务，部分失败禁止自动重跑或回滚。
只批准单场不能用于执行一次广域 refresh。GET 现在不会产生这些写入。

## 提供商及真实捕获准入

[提供商条款](https://www.balldontlie.io/terms.html)（2026-10-20 更新）允许合法个人/商业数据用途，
但禁止以替代其 API 的数据 API/feed 供给；凭据滥用、虚称联盟官方背书及未经独立核验的唯一来源投注结算受到限制。
条款不提供第三方权利许可；本项目使用第三方赛程，不能冒充 NBA 官方源。
[API 文档](https://docs.balldontlie.io/)列明 Games 可用 Free 层，每分钟 5 请求。
仅核对文档不等于操作者接受条款或取得部署权限，不代注册、不代订阅。

凭据 `DSL_BDL_API_KEY`、应用网络开关、单独有界只读提供商请求批准与真实七库写授权互不替代。
无凭据/无请求许可时保持 `READ_ONLY_PROVIDER_VERIFICATION=NOT_EXECUTED`。
选定真实目标还须有合法盘口、唯一身份、双方精确 ID 映射、明确 scheduled、未来带时区开赛时间、
当前版本无既有快照，并核对实际捕获时钟小于开赛时刻。

真实执行前操作者须明确批准仓库、实际部署 SHA、绝对 DATA_DIR、BDL 提供商、正常刷新操作、
七库具体写范围、仅一次执行、禁止自动重试、禁止后台刷新及禁止 Canonical 对账。
停止实例的旧授权不授权重新启动或刷新。本机应用保持停止，部署生效须另行核验。
后续赛后写入须另获批准，完赛依赖独立核验真实结果；不能单靠时间流逝或 BDL 未核验数据结算。

## 历史事件与证据

此前热重载造成的数据漂移报告原样保留，不改写为全会话未变。
旧设备四份人工评审证据本机不可用，不制造哈希，不授权归并。
开发、隔离测试与报告始终不改真实七存储；本机报告仅在 Git 本地排除的 reports/ 中。
