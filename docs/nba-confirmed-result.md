# NBA 官方赛果绑定（单场或同批多场）

## 适用范围

`python -m tools.nba_confirmed_result` 是**离线、显式选择目标**的结果入口，支持任意
 operator 选定的 NBA 赛事（不再限定魔术对雄鹿），也支持同一批准下的同批多场。
它不联网、不调用广域刷新。输入为已经获准获取的 NBA 官方比赛页与 BDL Games 原始响应。
自动交叉核对不是 `HUMAN_REVIEWED`，也不授权 Canonical 归并。

## 绑定契约

必须有已保存且唯一的 BDL 赛程赛事 ID，Canonical / 历史快照 / 两个源的球队 ID
必须按现有显式映射同方向一致，UTC 开球时刻必须一致，历史预测必须严格早于开球。
球队身份以 Canonical 队名为锚，经 30 队显式映射核对 `nba:` 与 `bdl:` 两侧 ID 及官方
tricode；官方引用 URL 必须由该锚定队码与官方赛事 ID 构造
（`https://www.nba.com/game/<away>-vs-<home>-<gameId>`），任意域名、错误队码、
错误赛事 ID 或附加查询串一律拒绝（`OFFICIAL_REFERENCE_MISMATCH`）。

NBA 官方与 BDL 必须均明确 Final，非平局的整数比分必须完全一致。缺失、反向、竞争绑定或
已存在的不同结果一律拒绝。不修改 `same_event()`，不解释彩票星期，不交换500侧主客或比分。

## 加时赛果危害防护

- 常规时间结束（`period == 4`）的平局**不是终场**：`REGULATION_TIE_NOT_FINAL` 拒绝；
  绝不把平局自动转换为终场比分，绝不估算加时得分。
- `period >= 5` 但任一来源未明确 Final：`OVERTIME_IN_PROGRESS` 拒绝。
- 两源 `period` 必须同为不小于 4 的整数且一致（`PERIOD_MISMATCH`）。
- 逐节对账：官方 `periods` 数组（前四节 `REGULAR`，加时 `OVERTIME`）与 BDL
  `q1..q4` / `ot1..ot3` 字段必须各自加总等于该源总分；缺节、类型错误或加总不符
  分别拒绝（`PERIOD_DATA_INCOMPLETE` / `PERIOD_SCORE_RECONCILIATION_FAILED`）。
- 结果溯源记录 `final_period` 与 `overtime`，供只读审计。

## 预览与同批多场

预览复用生命周期、结算与评估的既有函数。计划格式为
`{"targets": [...], "store_hashes", "generation", "write_scope", "preview_sha256", ...}`：
每个目标绑定 `match_id` / `snapshot_id` / `snapshot_digest` / `resolved_match` /
`settlement_id` / `evaluation_id`；整批共享一份七库字节摘要、刷新代号与预览摘要。
同批目标不得重复（`DUPLICATE_TARGET`）。单场入口是同批一场的特例，同一引擎。
默认仅输出预览；读取损坏 JSON 直接失败，不隔离或修复真实文件。

## 分离的执行批准

**代码部署、预览、网络许可均不授权真实执行**。操作者须另外批准精确仓库/SHA、绝对 DATA_DIR、
输入来源与证据、预览摘要、**完整目标集合**、一次执行和以下范围：

| 文件 | 唯一允许的影响 |
| --- | --- |
| daily_matches.json | 各目标状态、全场比分与结果溯源 |
| settlements.json | 既有快照对应的不可变结算追加 |
| evaluation_rows.json | 同一结算和快照的评估行追加 |
| 其余四库 | 字节不变 |

一次明确批准可以覆盖一个**事先完整可见**的多场目标集合；对单场的批准绝不延伸到其余场次。
执行命令除预览参数（或 `--targets-file`）外还需
`--execute --expected-preview-sha256 <摘要> --receipt <外部回执文件>`，
并在该进程临时开启 `DSL_RUNTIME_WRITES_ENABLED=true`。摘要或开关只是软件门，不替代独立批准。
不直接编辑结算 JSON，调用原有 `settle_snapshot()` 与 `capture_evaluation_row()`；不校准球队，
不重建预测，不更新盘口，不触碰其它比赛、既有历史标识符或人工评审记录。

OS 刷新锁和快照锁覆盖执行，写前重新核对七库摘要及代号，排他创建回执。
**整批预检先于任何写入**：任一目标快照摘要或身份失配即零写入失败。
一次执行 = 一次锁段 = 一次刷新代号递增 = 一份回执；这是结果绑定操作，**不是一次 scraper.refresh()**。
跨文件仍非事务，部分失败写 FAILED 并在回执中列出已完成目标，**不自动重试或回滚**。
成功后必须重新读取真实记录验收。同一回执不可复用；新预览重放只返回既有相同结算/评估，不覆盖历史。

## 验证边界

`tests/test_nba_confirmed_result.py` 使用隔离七库验证身份、时序、双源冲突拒绝、写门、
过期摘要、幂等、三库范围、加时五类判定（常规平局拒绝 / 加时进行中拒绝 / 加时终场接受 /
双源冲突拒绝 / 方向错误拒绝）、逐节对账、通用 URL 校验与同批执行（含部分失败与零写入预检）。
合成通过不等于真实结算通过；真实执行始终等待独立批准。