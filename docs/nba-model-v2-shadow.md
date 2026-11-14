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