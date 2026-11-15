# NBA 真实球队实力与隔离影子模型 V2

本文件说明 `basketball-margin-2-shadow` 实验模型如何由**真实历史完赛**推导球队实力，
并与生产模型 `basketball-margin-1` 做只读对比。生产模型、历史快照、结算与 Canonical
身份**一律不改动**。

## 一、模块与职责

| 模块 | 职责 | 写边界 |
| --- | --- | --- |
| `utils/nba_history.py` | BALLDONTLIE 历史完赛解析、确定性数据集装配、有界分页获取 | 只读分析数据集，**不**写七库 |
| `utils/nba_team_strength.py` | 由真实完赛推导 30 支球队实力特征与历史 Elo | 纯函数，无 I/O |
| `models/basketball_model_v2.py` | 消费实力特征的隔离影子胜负概率 | 纯函数，`writes_production_stores=False` |
| `utils/nba_model_evaluation.py` | 指标与按时间切分的走查回测 | 纯函数，无 I/O |
| `tools/nba_model_comparison.py` | 只读对比工作流（CLI） | 只读 `data/daily_matches.json`，报告写 `reports/` |

> **红线**：不改 `basketball-margin-1` 数学、不改 `same_event()`、不改 `normalize_competition_number()`、
> 不改 Canonical ID 与彩票编号、不写七个受保护存储、不从 HTTP GET 物化影子预测。

## 二、历史数据采集（Workstream B/C）

数据源为 `balldontlie_nba_games`（`GET https://api.balldontlie.io/v1/games`），
是**第三方文档化赛程 API**，不是 NBA 官方直连。复用 `utils/nba_schedule` 的 30 支球队
显式映射、UTC→北京转换与**进程内共享频控**，但使用独立的「带比分」解析器
（赛程补全解析器不提取比分）。

仅接受**已确认完赛**：`status_state == final`、`postponed is False`、双方整数非负比分、
`period >= 4`、主客不同、已知 NBA 球队 ID、唯一提供商比赛身份、合法赛季与带时区 UTC 时间戳。
拒绝 `scheduled` / `in_progress` / `postponed` / `canceled` / `unknown`、畸形比分、
冲突重复 ID。每条观测保留 `provider_game_id`、双方球队 ID、赛季、`scheduled_start_utc`、
`source_observed_at`、`source_reference`、`source_content_digest`。

**时间完整性**：`source_observed_at` 是本次检索时刻，**不**证明该赛果在历史预测时刻已被
本应用观测。前瞻预测只允许 `scheduled_start_utc < cutoff` 的输入；回溯研究显式标注
`RETROSPECTIVE_WALK_FORWARD_RESEARCH`，绝不冒充原始实时快照。

### 分页与频控

跟随 `meta.next_cursor` 直到明确结束；后页失败、冲突、预算耗尽一律 `PAGINATION_INCOMPLETE`
且**返回零条可用比赛**。Free 层每分钟 5 请求，进程内共享间隔至少 12.1 秒；每次获取默认
最多 10 请求 / 120 秒；429 每页最多重试 2 次。默认拒绝网络（`allow_network=False`），
缺凭据返回 `disabled_missing_credentials`，绝不回退 ESPN 或付费端点。

> **频控只在进程内生效**，不保证多进程共享账户的全局额度。凭据只从环境变量
> `DSL_BDL_API_KEY` 读取，绝不进入源码、报告、命令参数、日志或 Git。

## 三、真实球队实力（Workstream D）

`compute_team_strength(games, as_of=...)` 是确定性纯函数：

- **共同先验** `prior_elo=1500`，**不**用队名哈希作为实力信号；
- **严格时序**：仅 `game_end_utc < as_of` 的比赛可更新；按开赛时间升序逐场更新；
- **显式 Elo**：`K=20`、主场加成 `home_advantage_elo=50`、`elo_scale=400`；
- **赛季过渡收缩** `season_shrinkage=0.25`（处理休赛期阵容变化）；
- **去重**：同一 `provider_game_id` 绝不重复更新；平局（`hs==aws`）拒绝；
- **小样本**：场次不足保留 `fallback_status`，由 V2 再做样本量收缩。

每支球队输出 `games_played`、`wins`/`losses`、`win_percentage`、场均得分/失分/净胜分、
近 5/10 场战绩、近 10 场净胜分、主客战绩、`days_since_last_completed_game`、
`back_to_back_indicator`、`historical_elo` 与 `source_evidence`（所用比赛 ID + 摘要）。

> **统计口径**：`average_point_differential` 是**场均净胜分**，**不是**每百回合净效率。
> Free Games 不含回合数输入，任何指标都**不**标注为 possession-adjusted。
> `game_end_utc` 用「开赛 + 150 分钟 + 15 分钟×加时」估算（Free 无精确结束时刻），
> 是**显式假设**，仅用于保守时序门禁。

30 支球队全部出现在清单中（即使 0 场），`supported_team_count` 如实报告有真实证据的球队数。
**空表或合成表不构成真实覆盖。**

## 四、隔离影子模型 V2（Workstream E）

`basketball-margin-2-shadow` 消费实力特征，保持三层概率语义：
`raw_model_probabilities`（融合前，评估权威）、`display_probabilities`（与市场融合展示）、
`market_implied_probabilities`（去水市场隐含）。

预期分差 = Elo 差（样本量收缩后）+ 主场优势 + 近期状态项 + 休息/背靠背项；
经正态 CDF 转胜负概率，`0≤p≤1` 且 `home+away=1`。任一队 `games_used_before_cutoff==0`
→ 显式 `INSUFFICIENT_REAL_TEAM_DATA`，绝不伪造改进。参数为 documented 固定先验，
**不**从原始八场队列拟合；任何未来拟合必须用按时间切分的历史训练区间。

## 五、只读对比工作流（Workstream F/H）

```bash
# 真实历史数据集 + 当前合格赛事的 V1/V2/市场对比
python -m tools.nba_model_comparison \
    --dataset reports/phase3x/historical-game-coverage/dataset.json \
    --as-of 2026-11-14T12:00:00+08:00 \
    --output reports/phase3x/v1-v2-comparison/comparison.json \
    --markdown reports/phase3x/v1-v2-comparison/comparison.md

# 仅导出 30 支球队真实实力清单
python -m tools.nba_model_comparison --dataset <path> --strength-only --output <path>

# 按时间切分的走查回测（回溯研究）
python -m tools.nba_model_comparison --dataset <path> --backtest --output <path>
```

对比表逐场给出 `canonical_match_id`、日期、主客、已核验开赛时间、`prediction_as_of`、
V1/V2 主客概率、市场主客概率、V1/V2 对市场差距、主客真实所用场次、覆盖状态与影子资格。
**不硬编码视觉差异**；V2 未改进时如实报告。

## 六、评估与防泄漏（Workstream G）

指标：`sample_count`、`direction_accuracy`、`binary_brier_score`、`binary_log_loss`、
`market_comparable_count`、去水市场 Brier/LogLoss、模型-市场绝对/ signed 差距，
始终在**同一批合格比赛**上计算。走查回测按**北京比赛日**分块、扩张窗口
（`as_of = D 当日 00:00`），被预测比赛自身与同日比赛互不 informing，
`same_date_leakage=False`、`future_result_leakage=False`、`random_shuffled_cv=False`。

> 前瞻目标 ≥100 场未来赛果是**未来目标**，不是当前结论。八场队列**不**支撑任何
> 长期校准、投注盈利或正 EV 声明。不自动创建任何周期性生产写入者。

## 七、真实采集授权门

开发任务本身**不构成**真实网络采集批准。新的真实提供商请求须由操作者明确：
提供商、批准的赛季/日期范围（优先 `season=2025` 与 `season=2026` 常规赛）、
最大 HTTP 预算、本机证据目的地（`reports/phase3x/`）、只读网络、不写运行库、
不未授权回退。无批准时真实采集记 `NOT_EXECUTED_AUTHORIZATION_REQUIRED`，
**绝不**以虚构实力数据替代缺失证据。

## 八、竞赛类型资格与校正回测（Phase 3-X-R）

### 竞赛类型分类

`classify_nba_competition(raw, regular_season_end_date=...)` 判定单场常规赛资格，优先级：
显式 `season_type` → `postseason` 布尔 → `ist_stage` → 常规赛末日日期边界 → 默认常规赛。

| 类型 | 计入常规赛 | 处理 |
| --- | --- | --- |
| `NORMAL_REGULAR` | 是 | 保留 |
| `NBA_CUP_REGULAR`（小组/淘汰赛，除决赛） | 是 | 保留 |
| `NBA_CUP_CHAMPIONSHIP` | 否 | 排除，保留证据 |
| `PLAY_IN` | 否 | 排除，保留证据 |
| `PLAYOFF` | 否 | 排除，保留证据 |
| `UNSUPPORTED_COMPETITION` / `AMBIGUOUS_CLASSIFICATION` | — | fail closed 拒绝 |

BALLDONTLIE Games 响应**不含** `season_type` 字段（仅查询参数），实际依赖
`postseason` + `ist_stage` + 常规赛末日（2025 赛季 = `2026-04-12`，已文档化）。
Play-In 落在常规赛末日之后且无 IST/postseason 标记。`assemble_historical_dataset`
默认 `regular_season_only=True`，分离合格/排除集合并保留 `excluded_competitions`；
`compute_team_strength` 与 `walk_forward_backtest` 对 `regular_season_eligible is False` 防御性跳过。

> **不得**为凑 1230 场任意删除比赛，**不得**改写原始比分或身份。2025-26 的 7 场额外比赛
> 经分类确认为 1 场 NBA Cup Championship + 6 场 Play-In（见 `reports/phase3x-r/`）。

### 获取完整性边界

四个属性互不等价：`CAPTURED_PAGE_CURSOR_CHAIN_CONTIGUOUS` / `CAPTURED_RECORDS_VALID` /
`CAPTURED_WINDOW_COMPLETE` / `PROVIDER_PAGINATION_COMPLETE`。本地提供商契约**未文档化日期排序保证**，
「最大捕获日期超出截止日」**不**足以证明截止窗口完整。2025 常规赛窗口由**独立结构不变式**
（30 队 × 82 场 = 1230，每队恰好 82 场）认证，不依赖分页；2026 进行中赛季无不变式且分页未完成 →
`UNRESOLVED`。整体 `HISTORICAL_CUTOFF_WINDOW_COMPLETE = UNRESOLVED`。有界日期界定重抓需新鲜授权，
无授权记 `LIVE_REACQUISITION = NOT_EXECUTED`。

### 真实前瞻门禁与对比语义

`classify_fixture_eligibility(m, as_of)` 要求身份/方向已确立、带时区权威开赛时间已知、
`prediction_as_of < verified_kickoff`。六类：`STORED_UPCOMING` / `VERIFIED_FUTURE_FIXTURE` /
`STALE_STORED_UPCOMING` / `KICKOFF_UNKNOWN` / `IDENTITY_UNRESOLVED` / `POSTGAME_RETROSPECTIVE_COMPARISON`。
只有 `VERIFIED_FUTURE_FIXTURE` 可作前瞻对比。对比三分类：`PROSPECTIVE_SHADOW_COMPARISON` /
`RETROSPECTIVE_MODEL_DIAGNOSTIC` / `PROSPECTIVE_COMPARISON_BLOCKED`。**计算出 V2 概率不足以标记前瞻**。
`500lq` 显示第一位方向未决（`DISPLAY_ORDER_UNVERIFIED`），绝不自动交换主客或归并。

### 校正回测与消融

走查回测保持按北京比赛日分块扩张窗口、无同日/未来/目标泄漏，标注
`RETROSPECTIVE_WALK_FORWARD_RESEARCH`。V1/V2 在**同一批合格比赛**上对比，报告固定 50% 与
扩张主胜先验基线；历史无赔率时市场对照 `N/A`，绝不伪造。数据集整体未认证 → 回测 **PROVISIONAL**。
既有 1404 样本研究值作 `SUPERSEDED_RESEARCH_ESTIMATES`，不强行匹配。

有界消融经 `v2_params` 隔离：V1 冷启动 / V2-Elo-only（`form_weight=0` 且 rest 参数 0）/
V2-Elo+form（rest 参数 0）/ V2-complete。走查回测不传 `fixture_kickoff` → rest 分量恒 0，
故 Elo+form 与 complete 在回溯队列上指标相同；休息贡献仅在真实前瞻中激活，回溯无法度量，如实陈述。
**不为提升报告调参，不升级影子模型为生产。**
## 九、当前赛季窗口认证与首个真实前瞻影子捕获

### 获取范围（不假设 `season_type=regular` 完整）

[提供商文档](https://docs.balldontlie.io/#games)规定 Games 的 `season_type` 取值
`preseason` / `regular` / `ist` / `playin` / `playoffs`，**省略时返回全部非季前赛类型**；
`postseason` **不是**文档化参数。`regular` 是否包含计入常规赛战绩的 NBA Cup 比赛未被文档化，
因此不得假设它完整。

| 分区 | `season_type` | 作用 |
| --- | --- | --- |
| `ALL_NON_PRESEASON_TYPES` | 省略 | 主覆盖：常规赛 + NBA Cup 全阶段 + Play-In + Playoffs，由分类器排除非常规赛 |
| `NBA_CUP_IST_CROSSCHECK` | `ist` | 子集核验：每个返回 ID 必须已在主分区出现且内容一致；按 ID 去重，**只计一次** |

- 分类器新增 `PRESEASON`：显式 `season_type=preseason` 或比赛日早于已文档化揭幕日
  （`NBA_REGULAR_SEASON_START_DATES`：2025 = `2025-10-21`，2026 = `2026-10-20`），排除但保留证据。
- 本机既有授权证据（`postseason=false`、无 `season_type`）实测返回 NBA Cup 小组/淘汰/决赛与
  Play-In，且无季前赛；这与文档一致，但不再依赖未文档化参数。

### 日期边界与预算

`plan_history_window(season, cutoff)` 是纯函数，不联网：

- 起始日 = 揭幕日前一日；结束日 = cutoff 在 UTC 与北京时区下的较晚日期
  （美方当地日期不晚于 UTC 日期）。日期过滤的时区语义未文档化，因此再按
  `scheduled_start_utc < cutoff` 严格过滤，任何语义下都不漏零点附近比赛。
- 请求预算（默认 10，含重试与交叉核验分区）在所有分区间**共享**；任一分区失败后不再发起后续分区。
- 规划带 `plan_sha256`；获取结果必须携带同一摘要，否则 `PLAN_DIGEST_MISMATCH`。

### 认证与对账

`fetch_history_window(plan, allow_network=...)` 默认拒绝网络，逐页保留 `request_cursor` /
`next_cursor` / `received_at` / `payload_sha256`。`certify_history_window(plan, fetched, previous_games=...)`
只有在下列条件**全部**成立时给出 `CURRENT_SEASON_WINDOW_COMPLETE = VERIFIED`：

- 每个分区游标耗尽（`next_cursor` 缺省）且无冲突 ID；
- 截止前比赛全部确认完赛（延期单列 `pre_cutoff_postponed_ids`；其余未完赛 → `PRE_CUTOFF_GAME_NOT_FINAL`）；
- 截止前无解析拒绝（含未知竞赛类型）；
- NBA Cup 交叉核验无缺口（`NBA_CUP_SCOPE_GAP`）、无内容矛盾（`OVERLAPPING_CATEGORY_CONFLICT`）；
- 既有捕获全部被重新确认（`PREVIOUS_EVIDENCE_NOT_RECONFIRMED`）且关键字段无冲突（`PREVIOUS_EVIDENCE_CONFLICT`）。

对账输出 `old_provider_game_ids` / `new_provider_game_ids` / `missing_old_ids` /
`newly_discovered_ids` / `conflicting_ids` / `existing_scores_changed` / `new_regular_season_games`；
冲突记录**并列保留**两份证据，绝不覆盖。2025-26 已认证数据与 2026-27 数据分开陈述。

### 提供商支撑的前瞻门禁

`classify_fixture_eligibility(m, as_of, provider_observation=..., provider_binding_count=...)`：
`VERIFIED_FUTURE_FIXTURE` 要求存储 `upcoming`、`market_tracked=true` + Canonical ID + 彩票编号、
双方球队身份、有效且**唯一绑定**的 BALLDONTLIE 赛事 ID、溯源
`balldontlie_nba_games` / `SCHEDULE_PROVIDER_VERIFIED` 且开赛时刻一致、带时区开赛晚于 `as_of`、
`as_of` 之前 900 秒内取得的观测仍为 `scheduled` 且未延期、观测主客与存储主客**完全一致**、
胜负盘价格有效、无 Canonical 归属冲突。新增 `SCHEDULE_STATUS_UNVERIFIED` 与 `MARKET_UNAVAILABLE`；
`source_status` / `identity_confidence` / `temporal_eligibility` / `market_status` 分别输出。
存储的历史排期观测**不**证明比赛当前仍按期进行；缺当前观测一律不予前瞻。

### 不可变影子工件（`tools/nba_shadow_capture.py`）

```bash
python -m tools.nba_shadow_capture capture --dataset <certified-dataset.json> \
    --match-id <canonical_id> --output-dir reports/phase3y/prospective-shadow [--allow-network]
python -m tools.nba_shadow_capture verify <artifact.json>
```

- 前置：数据集 `certification.REGULAR_SEASON_DATASET_CERTIFIED` 与
  `CURRENT_SEASON_WINDOW_COMPLETE` 均为 `VERIFIED`；门禁 `VERIFIED_FUTURE_FIXTURE`；
  `prediction_as_of <= generated_at < verified_kickoff`；市场观测早于 `as_of`；V2 真实场次充足。
- 提供商绑定复用 `apply_nba_schedule_time_enrichment`，但**只作用于内存副本**，绝不回写七库。
- V2 只用 `game_end < prediction_as_of` 的完赛；工件记录 `temporal_input_verification`，
  `capture_mode = CONTEMPORANEOUS_PROSPECTIVE_CAPTURE`，与回溯研究分开。
- 工件按 `snapshot_identity = sha256(canonical_id|bdl_id|v1|v2|as_of)` **独占创建**；
  内容相同重复写入为 `IDEMPOTENT_EXISTING`，内容不同 `SHADOW_ARTIFACT_IMMUTABLE` 拒绝；
  若磁盘已有工件内容遭外部篡改（`content_sha256` 不匹配），拒绝 `EXISTING_ARTIFACT_TAMPERED`；
  输出目录位于 `DATA_DIR` 内一律拒绝。`content_sha256` 覆盖除自身外的全部字段。
- **单场赔率历史溯源**：拒绝全局刷新时间戳 `meta.last_refresh` 作为赔率证据，必须从 `odds_snapshots.json` 或独立验证的市场观测获取，严格比对 Canonical ID、彩票编号、双方球队、玩法与价格，缺失报 `MISSING_MATCH_ODDS_PROVENANCE`。
- **全量 Canonical 归属唯一性**：跨全部状态（包括完赛与已结算记录）核验 BDL 比赛 ID 拥有者数量必须唯一（`count == 1`），存在未决主客反向候选即拒绝 `REVERSED_HOME_AWAY_REJECTED`，存在重复绑定即拒绝 `CANONICAL_OWNER_CONFLICT`。
- `evaluate_shadow_artifact(artifact, final_result)` 是只读评估契约：同一 Canonical ID +
  BDL 赛事 ID 连接、要求官方确认终场、校验工件摘要；**不**结算、**不**写库、**不**自动运行。

> 前瞻队列目标 ≥100 场、跨多个比赛日，是**未来目标**；不为累积样本创建周期性生产写入者。
