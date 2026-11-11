# 智能体协作规范 (AGENTS.md)

本文件规定在本仓库中工作的 AI 智能体**必须遵守**的硬性约束。
分工：`README.md` 说明项目怎么用，本文件说明**怎么改**。

> 优先级：本文件 > 智能体默认习惯。用户当次明确指令 > 本文件。

---

## 一、提交规范

### 1.1 提交信息有且只有一句话

- 使用 `git commit -m "<单行>"`，提交信息**只有一行**。
- **不写** body：不写分段说明、变更清单、要点列表、动机段落、测试统计、验收矩阵。
- 变更细节写在**给用户的回复**或 `docs/` 文档中，不要塞进 commit message。
- 用户直接给出提交信息原文时，**逐字照用**，不自行扩写、不加前缀或后缀。
- 推荐格式：`<type>(<scope>): <一句话说明>`，小写祈使句，结尾不加句号。

```text
✅ fix(tools): drop machine-specific path assumptions from staging path guard
✅ feat(staging): support manifest-bound canonical snapshot adaptation
❌ fix(tools): harden path guard

   Phase 1-A correction only. No change to the Canonical identity model, ...
   * The hardcoded roots are renamed LEGACY_* ...
   * Containment is now tri-state ...
```

### 1.2 提交信息禁止出现阶段 / 分期字眼

提交信息中**不得**出现任何与「阶段、分期、批次」相关的字样，包括但不限于：

| 禁止形式 | 示例 |
| --- | --- |
| `phase` + 任意标识 | `phase1`、`phase1a`、`phase_2`、`phase 2` |
| `Phase` / `PHASE` 等大小写变体 | `Phase1`、`Phase 2`、`PHASE1`、`PhAsE1` |
| `stage` 系列 | `Stage1`、`Stage 2`、`stage1`、`STAGE2` |
| `P` / `p` + 数字系列 | `P0`、`P1`、`P2`、`P2-1`、`p1` |
| 中文分期词 | `第一阶段`、`阶段一`、`第二期`、`第一批` |
| 以上形式的任意分隔符组合 | `Phase 1-A`、`Phase-1A`、`phase.1`、`stage_2`、`P2/1` |

判定规则：**大小写不敏感**；分隔符空格、`-`、`_`、`.`、`/`、`:` 一律等价。等价正则（可直接使用）：

```regex
(?i)(?:^|[^A-Za-z0-9])(?:phase|stage)[A-Za-z0-9]*(?:$|[^A-Za-z0-9])|(?:^|[^A-Za-z0-9])p[0-9]+(?:$|[^A-Za-z0-9])|第[0-9一二三四五六七八九十百]+(?:阶段|期|批次|批|轮|步)|阶段|分期|批次
```

自检命令（命中即违规，输出为空表示通过）：

```bash
git log --format=%s | grep -nEi '(^|[^A-Za-z0-9])(phase|stage)[A-Za-z0-9]*([^A-Za-z0-9]|$)|(^|[^A-Za-z0-9])p[0-9]+([^A-Za-z0-9]|$)|第[0-9一二三四五六七八九十百]+(阶段|期|批次|批|轮|步)|阶段|分期|批次'
```

- 规则同时作用于标题行与正文；由于 1.1 已禁止正文，等价于**提交信息中完全不得出现**。
- 该判定**从严执行**，以下两类**故意**从宽命中，属于预期行为而非误报，请改写措辞：
  - 裸词 `stage` / `phase` 也命中（`fix: rewrite graph stage machine`
    → 改写为 `fix: rewrite graph state machine`）。
  - `P` + 数字一律视为分期字眼，即使本意是 `p95`、`p99` 之类的技术缩写，
    也应改写（例如 `95th percentile`）。
- 需要表达「这是第 N 批改造」时，改用**功能性描述**：

```text
✅ fix(tools): harden protected-path portability
❌ fix(tools): Phase 1-A portability hardening
```

### 1.3 其他提交约束

- 作者与提交者：`YGone <56142733+YGone-001@users.noreply.github.com>`。
  提交前核对 `git config --local --get user.email`。
- 开发分支为 `develop`；`main` 保留，**不要**自动合并。
- 默认**前向提交**，不改写历史（不 amend / rebase / squash / force-push）；
  仅当用户明确要求时，才允许 `reset --soft` 重写后重新提交。
- 推送后确认 `origin/develop` 等于本地 HEAD。

---

## 二、内容规范

- 说明性、解释性内容写在**给用户的回复**或 `docs/` 文档中，**不写进提交信息**。
- 文档风格与 `README.md` 对齐：中文正文、`##` 分章、表格做对照、`**粗体**` 强调硬约束、
  `>` 引用块放提示、代码块标注语言。
- 不得修改 `data/*.json` 中的生产数据，不得执行生产修复 `apply` / `recover`。
- 六个非 Canonical 存储必须**字节不变**：`team_strength.json`、`calibrated.json`、
  `prediction_snapshots.json`、`odds_snapshots.json`、`settlements.json`、`evaluation_rows.json`。

---

## 三、项目约束（红线）

- `data/daily_matches.json` 的 Canonical 集合**必须 id 唯一**，
  不得放宽 `utils/scraper.py` 的 fail-closed 守卫。
- 不得削弱 `tools/staging_canonical_repair.py` 的受保护路径限制，
  不得新增 `--allow-production` 之类旁路开关。
- 生产修复必须经 Ed25519 签名授权；不得在仓库或应用工作区内生成、保存生产签名私钥。
- 不得改动足球 / 篮球模型数学、Temporal Evaluation 公式与 Canonical 身份判定语义。

---

## 四、身份语义与冲突口径

### 4.1 稳定性作用域 ≠ 唯一源事件身份

| 概念 | 取值 | 语义 |
| --- | --- | --- |
| 稳定性作用域（`SourceIdentityScope` / `stability_scope`） | adapter × sport × event_date × provider_namespace × raw_identifier_type | 一个源**域**；同一天、同命名空间下的**多场不同比赛共享它** |
| 源事件身份键（`strong_number_identity_key`） | adapter × sport × event_date × `derived_source_id` | 标识**具体的那一个源事件**声明 |

> 冲突结论必须绑定到「**源事件身份**」，不能绑定到「域」。
> 同一域内的独立比赛各自拥有不同编号，**不构成**身份互斥。

### 4.2 强编号冲突必须按源事件身份判定

`utils/source_identity.detect_strong_number_conflict` 的分组契约：

- 分组键**必须**是 `strong_number_identity_key()`（含 `derived_source_id`）；
- **不得**退回 `stability_scope.as_key()`：它描述域，会把独立比赛误判为互斥（真实数据上
  曾产生 100% 假告警）；
- **不得**把 `raw_identifier_type` 纳入分组：它取决于「id 尾段是否恰等于竞彩编号」这一观测
  形态，纳入后会**漏检**同一源 ID 的真实矛盾；
- **不得**只按 `competition_number_normalized` 分组：目标是「一个源身份是否自相矛盾」，
  不是「哪些比赛共用编号」；
- 缺少 `derived_source_id` 时返回 `None`，**绝不**为缺失的源身份补全或升级证据。

### 4.3 真实的身份矛盾必须 fail closed

- 同一条派生源 ID 在同一作用域内声明两个及以上互斥强编号 → `STRONG_NUMBER_CONFLICT`，
  严重度保持 `REJECT`。
- **不得**为了让告警消失而把 `REJECT` 降级为 `AMBIGUOUS`，也不得整体屏蔽该类别。
- 跨运动共享命名空间、行位置复用、Canonical 归属冲突三类检测**彼此独立**，
  修正强编号分组时不得改变它们的语义。

### 4.4 真实数据只读，结论不得外推

- 不得自动合并重复 Canonical 归属、不得自动批准歧义对、不得改写 `calibrated.json`、
  不得修改结算/评估引用、不得引入自动队名别名策略。
- 无独立人工复核标签时，**不得**声称精度 / 召回提升。
- 真实数据结论只在**本机真实数据**上成立；CI 无该私有数据集时，只能报告合成测试结果，
  不得用合成结果冒充真实数据验收。

### 4.5 候选证据 ≠ 人工评审结论

- 重复候选的分类（`HIGH_EVIDENCE_DUPLICATE_CANDIDATE` / `AMBIGUOUS_NAME_CANDIDATE` /
  `INSUFFICIENT_SOURCE_IDENTITY` / `NEGATIVE_CONTROL_CANDIDATE`）是**诊断标签**，
  永远不是裁决。未记录人工决定时，状态保持 `UNREVIEWED`。
- 评审记录的 `label_provenance` **必须**是 `HUMAN_REVIEWED`；`AUTOMATED` /
  `MODEL_GENERATED` / `DERIVED` / `PROXY` 一律拒绝（`AUTOMATED_LABEL_REJECTED`）。
- 评审记录必须绑定 `dataset_fingerprint` + `candidate_fingerprint` + `canonical_ids`；
  任一不匹配即拒绝（`DATASET_FINGERPRINT_MISMATCH` / `STALE_CANDIDATE_FINGERPRINT` /
  `CANONICAL_IDS_MISMATCH`），不得重新解释。
- 同一候选的**矛盾决定**必须拒绝（`CONFLICTING_LABEL`），不得按"最新一条"取胜。
- 评审文件不得携带生产授权或变更语义字段（`approved` / `signature` / `apply` /
  `merge` / `delete` / `surviving_canonical_id` 等），命中即拒。
- 自述的 reviewer 只是**审计元数据**，**不**等同 Ed25519 所有者批准，
  **不**替代既有签名治理。

### 4.6 Canonical 归并对账只允许预览

- `tools/real_sports_duplicate_review.preview` 只做**影响分析**：不得选择存活 Canonical ID
  （禁止按字典序、源层级或"缺编号"来选），不得删除较弱记录、不得生成可执行修复清单。
- 预览输出必须显式声明 `executable=false`、`apply_available=false`、
  `production_write_authorization=false`、`surviving_canonical_id=null`。
- 缺少强编号的身份**不得**单独确立 Canonical 归属；派生 provider 后缀不得升级为已验证身份；
  截断队名不得单独作为同一赛事证据。
- 历史 `snapshot_id` / `settlement_id` / `evaluation_id` **不可变**：
  对未来归并的影响只做说明，绝不重写既有快照、结算或评估标识符。

### 4.7 篮球赛程、在售市场与盘口合并

- **NBA 赛程 ≠ 竞彩在售**：一场比赛可以已排期但没有被竞彩篮球市场收录。
  没有可用盘口的赛事**不得**被升级进 `market_tracked` 集合，也**不得**凭空补赔率。
- **市场准入契约**（`utils.scraper._admit_market_candidates`）：
  已跟踪事件即使本轮无盘口也保持生命周期连续；**首次出现**的新候选必须满足
  `has_usable_market_odds()` 才准入，并打上 `market_tracked = true`。
  不得为"让比赛显示出来"而放宽该判定。
- **盘口合并优先级**（`utils.scraper._attach_odds`）：同一事件的所有玩法行
  （篮球 `sf` 胜负 + `dxf` 大小分；足球 `spf` + `nspf`）都要被消费；
  合并口径是**已有值优先、只补缺失键**，绝不覆盖既有有效价格。
  不得因为"已有任意盘口"就跳过独立匹配到的另一类市场。
- **源诊断契约**（`utils.fetcher_500`）：`live_basketball` / `jclq_sf` / `jclq_dxf`
  必须分别报告 `status`（`unavailable` / `empty` / `ok`）、`rows`、`nba_rows`、
  `valid_market_rows`、`parser_errors`、`last_success_at`、`last_nba_success_at`。
  `refresh()` 的单一成功标志**不**构成数据可用性证据；命中缓存**不得**刷新
  `checked_at`，避免把旧结果伪装成刚成功。
- **运动感知日期**：`/api/dates` 提供 `dates_by_sport`；前端选择「篮球」后**不得**
  停留在该运动没有比赛的日期上，否则会把"当日无篮球"误报成"没有比赛"。
  已存储但当前日期不可见（`STORED_BUT_NOT_DISPLAYED`）必须与"数据缺失"区分开。
- **红线不变**：`same_event()` 语义、Canonical ID 唯一、Phase 2-T 强编号作用域、
  缺编号 fail-closed、归属冲突拒绝、历史快照与结算不可改写、生产映射未启用。

### 4.8 运营验收：部署版本、只读口径与真实刷新授权

- **部署版本 ≠ 提交版本**：CI 通过**不**证明运行实例已使用修复代码。
  运营验收必须独立确认：后端加载的模块路径与函数签名、前端 `dist` 产物哈希
  与 `index.html` 引用的 bundle；不得仅凭仓库 HEAD 宣称部署生效。
- **只读运营验收**：默认模式是 `READ_ONLY`。验收只允许 GET 读取端点
  （`/api/dates`、`/api/matches`、`/api/status`）与纯函数调用；
  不得为了"让数据对上"而调用 `scraper.refresh()`。
- **覆盖重叠必须用身份判定**：`live_basketball` / `jclq_sf` / `jclq_dxf` 与已存储记录的重叠
  只能按 `same_event()` 的源事件身份计算；**不得**用联赛名计数、队名相似或"同一天"推断重叠。
  竞彩编号存在两种书写形态（展示式 `周六308` 与编码式 `6308`），**不得**在未获治理批准时
  自行改写编号或规范化规则。
- **赔率有效性与合并**：合并时「已有有效值优先」；**已有非法值**（`0`、`1.0`、`NaN`、`inf`、
  非数值字符串、非法盘口线）**不得**被当作有效市场覆盖，可被独立验证过的有效值替换。
  新值本身非法时不得写入；不得为了"统一格式"覆盖有效价格，也不得凭空补齐赔率。
- **历史状态三分**：`STORED_STATUS`（落盘事实）、`DERIVED_DISPLAY_STATUS`（按时间推导）、
  `SOURCE_CONFIRMED_FINAL_STATUS`（源给出赛果）必须区分。
  派生状态**不得**产生结算、**不得**改写落盘事实；结算只接受带真实赛果的完赛记录。
- **真实刷新需显式授权**：未获操作者明确批准时，不得对真实数据执行刷新；
  未执行的验收项必须记为 `NOT_EXECUTED_AUTHORIZATION_REQUIRED`，
  **不得**把"没有批准"写成"验证通过"。
- **读取端点按设计的物化**：`/api/matches` 会物化赛前预测快照、球队库会按需加载；
  这两项属于既有设计，但 Canonical / 赔率历史 / 结算 / 评估存储**绝不**由读取路径改写。

---

### 4.9 跨源身份差异：候选证据 ≠ Canonical 身份

- **Canonical 身份 vs 候选证据**：`same_event()` 仍是唯一的运行时判等方式；
  `utils/basketball_identity_evidence` 只产出**候选级**证据（`CANDIDATE_SAME_EVENT` /
  `CANDIDATE_DIFFERENT_EVENT` / `AMBIGUOUS` / `INSUFFICIENT_EVIDENCE` /
  `CONTRADICTORY_EVIDENCE`），且 `authoritative` / `is_approval` /
  `authorizes_odds_merge` / `authorizes_event_admission` **恒为 False**。
  候选结论**不得**被当作 Canonical 归属决定。
- **编号书写形态只是假设**：展示式 `周六308` 与编码式 `6308` 的对应关系来自
  **观测一致性**，不是 provider 契约。解释函数必须同时给出方法、支持证据、矛盾证据与
  置信分类；证据不足时返回非权威结果。**不得**对四位数字统一剥离首位数字，
  也**不得**在运行时路径改写 `normalize_competition_number()`。
- **原始编号必须保留**：任何解释都要保留 provider 实际写下的原始字符串，
  禁止用解释结果覆盖 `jczq_no` / `matchnum` / `round` 或 Canonical ID 中的编号段。
- **主客方向未决**：两源主客顺序相反时只报告 `REVERSED_HOME_AWAY`，
  **不得**自动交换主客队、**不得**交换 `home_win` / `away_win`、**不得**跨未决方向
  合并大小分与胜负盘。反向本身**不构成**同一事件的证据。
- **禁止自动归并**：候选可以计数、可以排队、可以预览，但**不得**自动选择存活
  Canonical ID、**不得**删除较弱记录、**不得**生成可执行修复清单。
- **既有评审工作流兼容**：差异候选人沿用既有 `candidate_id` / `candidate_fingerprint` /
  `dataset_fingerprint` / `canonical_ids` 绑定与 `label_provenance = HUMAN_REVIEWED` 校验；
  自动化诊断**不得**写入人工决定，评审文件里**不得**出现生产授权或变更语义字段。
- **历史引用不可改写**：`prediction_snapshots` / `odds_snapshots` / `settlements` /
  `evaluation_rows` 的既有标识符只做**只读计数**，任何预览都不得改写它们指向的
  Canonical ID。
- **真实数据验收边界**：无人工复核标签时，候选数与分类数是**诊断统计**，
  不得称为"已确认重复"；本机真实数据结论不得外推到 CI 或其它环境。
  真实数据缺失时只报 `NOT_AVAILABLE`，不得编造候选数量。
- **本机报告**：审计产物写入 `reports/`（本机 Git 元数据排除）或仓库外的临时目录，
  **不得**写入 `data/`、不得提交、不得推送。
- **CI 顺序不变**：本地回归 → 前向单行提交 → 推送 → 定位**最终 SHA** 的 workflow →
  等待终态 → 核对四个 job → 才写最终报告。

---

### 4.10 NBA 复核工作台：离线、可复核、不可执行

- **工作台用法**：`tools/nba_human_review_workbench` 提供 `prepare` / `show` / `validate` /
  `summary`。`prepare` 产出优先队列、每候选 15 问卷宗、空白评审模板、独立主客证据模板与
  使用说明；全部产物写入 `reports/` 或仓库外的操作者目录，**不得**写入 `data/`。
- **候选优先级不是批准等级**：`P0`（编号/队名矛盾）→ `P1`（精确反向 + 编号对应）→
  `P2`（队名变体/音译）→ `P3`（其他），只决定复核顺序，**不得**被读作置信度，更不得
  自动转成任何决定。
- **独立主客证据**：操作者以外部队列/技术统计/文档补录证据，条目必须带
  `reference_identifier` / `publisher` / `source_reference` / `retrieved_at` /
  `actual_event_date` / `away_team` / `home_team` / `checked_by` / `verification_status`。
  只有 `SOURCE_REFERENCE_REVIEWED` 且日期与队对匹配的条目才能支持方向；URL 的存在
  **不是**内容已核验的证明；工作台**不**联网抓取；自动化/模型字段一律拒绝。
- **人工决定 provenance**：评审记录沿用既有 schema，`label_provenance` 必须是
  `HUMAN_REVIEWED`；`AUTOMATED` / `MODEL_GENERATED` / `DERIVED` / `PROXY` 一律拒绝。
  同一候选的矛盾决定**不得**按"最新一条"取胜。人工决定是**复核证据**，
  **不是**签名治理授权。
- **评审文件校验**：`validate` 复用既有校验器，拒绝未知候选、过期数据集/候选指纹、
  Canonical ID 不一致、缺失绑定与携带 `approved` / `signature` / `merge` /
  `surviving_canonical_id` 等生产语义字段的记录。
- **去重历史引用**：逐对计数会重复统计同一历史记录；受影响记录数必须以**去重**计数为准，
  且 `prediction_snapshots` / `odds_snapshots` / `settlements` / `evaluation_rows`
  的既有标识符**不可改写**。
- **候选权威标志不变式**：`CrossSourceCandidateEvidence` 与
  `LotteryNumberInterpretation` 的 `authoritative` / `authorizes_odds_merge` /
  `authorizes_event_admission` 是 `init=False` 常量字段，构造与
  `dataclasses.replace()` 都**不能**把它们置为 `True`，序列化恒为 `false`。
- **禁止自动归并、禁止交换赔率**：工作台不选存活 Canonical ID、不删除记录、不生成修复
  清单、不交换主客队与 `home_win`/`away_win`。
- **保护数据完整**：七个受保护存储在每次运行前后必须字节不变。
- **CI 先于最终报告**、**`reports/` 本地排除**：与本文件其余章节一致，不得例外。

---

### 4.11 源侧匹配 vs 跨源同一性（工作台校正）

- **匹配必须按 provider 侧分别进行**：外部记录只与**某一侧**队对一致时状态为
  `SOURCE_SIDE_MATCHED`，**不得**直接升级为 `ORIENTATION_INDEPENDENTLY_SUPPORTED`。
  旧实现把外部两队与两源四名并集比较，会把合法的单侧记录误判为 mismatch；该缺陷已修正。
- **跨源同一性必须显式**：`cross_source_identity` 默认
  `CROSS_SOURCE_IDENTITY_UNVERIFIED`；只有覆盖**全部 provider 侧**、无未决矛盾、
  主队声明一致、fixture 标识不冲突时才变为已核验。
- **别名只能由人工证明**：队名拼写差异需操作者另填
  `team_name_correspondence`（`provider_team` / `source_team` /
  `verification_status`），`SOURCE_REFERENCE_SUPPLIED` 状态**不构成**等价证据，
  任何从拼写推断别名的自动化都不得新增。
- **矛盾引用阻断结论**：`contradictory_references` 非空、多个已核验条目主队不一致、
  或 fixture 标识互相竞争时，状态必须是 `ORIENTATION_CONTRADICTED`，
  不得为了得到正向结论而丢弃冲突来源。
- **证据状态只能由人工升级**：模板默认 `SOURCE_REFERENCE_SUPPLIED` 且 `checked_by`
  为空；只有人工真正打开并阅读来源后才能改为 `SOURCE_REFERENCE_REVIEWED`。
  模板不得预填虚构 reviewer。
- **校验退出语义**：`SUCCESS = 0`、`REVIEW_REJECTED = 4`、`EVIDENCE_REJECTED = 5`、
  `MULTIPLE_REJECTIONS = 6`；证据被拒**不得**返回成功。输出必须分别暴露
  `review_rejected_count` / `evidence_rejected_count` / `total_rejected_count`。
- **首批证据试点**：`pilot` 子命令只在离线生成证据包；未联网时一律记
  `EXTERNAL_EVIDENCE_NOT_RETRIEVED`，**不得**编造 URL、fixture 标识或人工复核。
- 其余约束（禁止自动归并、禁止交换主客与赔率、历史标识符不可改写、七存储完整、
  `reports/` 本地排除、CI 先于最终报告）与 §4.9 / §4.10 一致，不得放宽。

---

### 4.12 真实 NBA 外部证据获取与人工评审准入

- **源发现 vs 人工评审**：网络检索与外部数据源获取仅产出参考证据，**绝不自动产生** `HUMAN_REVIEWED` 溯源；自动化下载与解析外部数据不构成人工核验，不得据此自动将状态升级为已核验。
- **独立 NBA 赛事实别码**：外部赛事实别码（如 NBA 官方 `0022600153` 或独立技术统计归档 `202610260DEN`）作为独立主客观佐证留存，**不得**用于改写既有 Canonical ID，亦**不得**直接触发 Canonical 合并。
- **原始时间戳与时区完整性**：外部事实证据必须完整保留原始比赛日期、本地开球时间与时区（如北美中部夏令时 CDT、山地夏令时 MDT、东部夏令时 EDT），并显式记录 UTC 与北京时间转换（UTC+8）。不得用转换后的日期覆盖源记录的原始时间字段。
- **`SOURCE_REFERENCE_SUPPLIED` 语义**：未核验的外部参考记录初始状态必须为 `SOURCE_REFERENCE_SUPPLIED`；该状态下 `checked_by` **允许留空**，记录保持未决，**不得**支持主客方向核验，**不得**建立跨源同一性，**不得**授权任何修复或归并。
- **`SOURCE_REFERENCE_REVIEWED` 硬性准入**：只有真实操作者阅读并核验外部事实源后，方可将状态置为 `SOURCE_REFERENCE_REVIEWED`；此时 `checked_by` **必填且必须归属于真实操作者**（严禁填入 `model`/`bot`/`auto`/`automated` 等虚构标识），同时必须包含非空独立源引用（URL/文档）、检索时间戳与事实摘录。
- **证据矛盾与主客反向阻断**：两源主客相反时（如 `500lq` 客队在前与 `500j` 主队在前），必须独立比对真实场地与官方主队，未决前**严禁自动交换主客或赔率**；若外部证据存在冲突引用或相互竞争的赛事实别码，状态必须保持 `ORIENTATION_CONTRADICTED`。
- **评审决定权限边界**：人工操作者录入的决定仅作为**复核证据**（Review Evidence），**绝不**等同生产修复授权，**绝不**替代 Ed25519 签名治理；未获操作者参与时保持 `UNREVIEWED` 并报告 `OPERATOR_REVIEW_REQUIRED`，严禁伪造人工决定。
- **工件本机隔离与七存储保护**：所有外部事实证据、对比矩阵与操作者卷宗仅保存在 Git 排除的本地 `reports/` 目录；七个受保护存储（`daily_matches.json` 等）在运行前后必须**字节不变**。
- **四类标识符必须分开陈述**：`NBA_OFFICIAL_GAME_ID`（如 `0022600153`，属于 NBA.com）、`THIRD_PARTY_ARCHIVE_ID`（如 Basketball-Reference 的 `202610260DEN`）、`LOTTERY_SOURCE_MATCH_NUMBER`（如 `6316` / `周六316`）与 `PROJECT_CANONICAL_ID`（如 `500j-…` / `500lq-…`）**不得**混用字段或互相代称；**严禁**把第三方归档 ID 写成 `official_fixture_id`，**严禁**编造 `00226-DEN-GSW` 这类无来源支持的「近似官方」编号。
- **数据集指纹变化即使既有候选失效**：`dataset_fingerprint` 一旦变化（真实同步、修复、剔除等），此前记录的 `candidate_id` 与 `candidate_fingerprint` 全部视为 `STALE_CANDIDATE_FINGERPRINT`；**不得**沿用旧指纹出具评审记录。证据可按**同一对 Canonical ID** 重新绑定，但必须在记录中保留原证据编号、原始标识符与重新绑定依据。
- **未打开原始来源不得升级证据**：操作者只以其它渠道（如虎扑、赛程聚合页）对照时间时，外部证据保持 `SOURCE_REFERENCE_SUPPLIED`、`checked_by` 留空；人工**决定**（`HUMAN_REVIEWED`）可与未核验证据并存，但**不得**据此声明来源已核验或建立跨源同一性。
- **评审记录必须绑定当前身份**：每条记录都必须携带当前 `dataset_fingerprint`、`candidate_fingerprint`、`canonical_ids`、真实 `reviewer`、`decided_at` 与 `evidence_reference`；矛盾评估写入 `notes`。记录中**不得**出现 `approved` / `signature` / `merge` / `surviving_canonical_id` 等生产语义字段。

### 4.13 彩票编号比赛日语义与提供方显示顺序

#### 4.13.1 七个互不等价的时间概念

| 概念 | 语义 | 本仓库取值 / 状态 |
| --- | --- | --- |
| `LOTTERY_MATCH_LIST_GROUP_DATE` | 编号所属的**上市分组日**（第三方赛程页的分组表头） | 由编号星期 token 间接体现 |
| `LOTTERY_MATCH_LIST_GROUP_WINDOW` | 该分组的**展示窗口**（北京时间 11:30 至次日 11:30） | 第三方分组约定，**非**官方契约 |
| `LOTTERY_NUMBER_WEEKDAY` | 编号中的星期 token（展示式 `周六`，编码式首位 `6`） | provider 原始写法，**不可改写** |
| `OFFICIAL_TICKET_SALES_HOURS` | 官方售票时段 | 已核验，见 §4.13.2 |
| `ACTUAL_MATCH_KICKOFF` | 真实开赛时刻（北京时间） | 源记录 `time` / `kickoff_at` |
| `NBA_US_LOCAL_GAME_DATE` | NBA 官方赛程的**当地**比赛日 | 官方页面标注的比赛日期 |
| `NBA_BEIJING_GAME_DATE` | 同一场比赛的**北京**日期（通常为当地日 +1） | 必须显式换算，不得与上者混用 |

> 这七者**不得**互相代称。编号星期既不是开赛日，也不是美方当地日期，更不是售票时段。

#### 4.13.2 上市分组窗口 ≠ 官方售票时段

官方参考：<https://m.sporttery.cn/bzzx/20210207/3143831.html?gid=1>（竞彩网「竞彩销售时间」）。

| 星期 | 开售 | 停售 |
| --- | --- | --- |
| 周一至周五 | 11:00 | 22:00 |
| 周六、周日 | 11:00 | 23:00 |

- 竞彩足球与竞彩篮球**时段相同**；页面明确提示「根据需要进行或有略微调整，具体请留意竞彩网公告」。
- 第三方赛程页把「11:30 至次日 11:30」这一区间呈现为**比赛列表分组**；它**不是**官方售票时段，
  **不得**表述为官方售票契约或官方赛制契约。
- 两类边界**不可互相反推**：本机观测到的分组边界严格晚于 11:00（11:00 北京开赛仍归前一日分组），
  而官方开售恰为 11:00，因此「分组窗口 = 售票时段」在实践中直接不成立。

#### 4.13.3 本机真实数据可复现的观测

对展示式编号（`周Xnnn`）逐条比对「token 星期 − 北京日期星期」（mod 7）。
下表实测于 `dataset_fingerprint = f030be64…0d7`（311 条赛事、40 条展示式编号）：

| 差值 | 条数 | 北京开赛时刻范围 |
| --- | --- | --- |
| `6`（token = 前一日） | 29 | 00:00 – 11:30 |
| `0`（token = 当日） | 11 | 14:00 – 23:00 |
| 其他 | 0 | — |

- NBA 子集 15/15 全部为差值 `6`。
- 判别量是**北京开赛时刻**而非联赛：跨零点至上午开赛归前一日分组，下午至夜间开赛归当日分组。
- 该数据把边界约束在 **(11:30, 14:00]** 区间内：11:30 开赛仍归前一日分组，
  而 14:00 开赛已归当日分组。这与「D-1 11:30 → D 11:30（末端含）」的窗口描述相容，
  但**不足以**证明端点恰好取 `11:30`。引用 `11:30` 必须同时声明这是未钉死的假设值。
- 条数随数据集变化；任何引用都必须绑定当时的数据集指纹并重新测量。

#### 4.13.4 证据等级保持 PARTIAL

- 上述语义来自**第三方归档分组表头 + 本机观测**，**不是**官方 provider schema 契约。
- 取得官方编号规则文本前，`LOTTERY_MATCH_LIST_GROUP_WINDOW` 与
  `MARKET_HOME_WIN_SEMANTICS` 均为 `PARTIAL`，**不得**声称已验证。
- 历史实测数字（曾记录的 77 条 / NBA 31/31）**不得**硬编码复用；
  每次必须按当前数据集重新测量，数据集变更即失效。

#### 4.13.5 星期差不是同一性矛盾，但不得整体屏蔽冲突

- 编号星期与北京日期不一致时分类为 `DATE_SEMANTIC_MISMATCH`，
  **不得**据此判定 `DIFFERENT_EVENT`，**不得**单独生成同一性矛盾告警。
- 但该豁免**只覆盖星期差本身**：真实的队名矛盾、主客反向未决、fixture 标识竞争、
  强编号自相矛盾等冲突仍然**必须 fail closed**，不得以「日期语义不同」为由整体抑制。
- 参考实例：官方 NBA 页面标注 `2026-10-31`（当地周六），对应北京开赛 `2026-11-01`（周日）；
  编号 token `周六` 与当地日一致、与北京日相差 1。**该例无法区分**
  「token 跟随当地日」与「token 跟随上市分组日」，两种假设在此例上给出同一结果，
  因此**不构成**任一假设的判别证据。

#### 4.13.6 提供方显示顺序与胜负盘方向（实测）

- `500j` 的 `home` / `away` 按 provider 语义对应场地主客（该语义**未获官方书面确认**）；
  `500lq` 的 `home` 字段是**显示第一位**。两者不得互相当作场地真值。
- **主客显示顺序反转：8/8 对成立。** 在 `captured_at` 完全相同的 8 组快照中，
  `500j` 与 `500lq` 的 `home` / `away` **全部互换**，无一例外。
- **价格精确镜像：仅 3/8 对成立。** 只有下列三组两侧价格完全互为镜像：

| 赛事 | `500j` home / away | `500j` home_win / away_win | `500lq` home / away | `500lq` home_win / away_win |
| --- | --- | --- | --- | --- |
| `2026-10-28` 2307 / 周二307 | 纽约尼克斯 / 底特律活塞 | `1.40` / `2.15` | 底特律活塞 / 纽约尼克斯 | `2.15` / `1.40` |
| `2026-10-28` 2309 / 周二309 | 洛杉矶湖人 / 波特兰开拓者 | `1.52` / `1.92` | 波特兰开拓者 / 洛杉矶湖人 | `1.92` / `1.52` |
| `2026-11-05` 3316 / 周三316 | 洛杉矶快船 / 迈阿密热火 | `2.78` / `1.22` | 迈阿密热火 / 洛杉矶快船 | `1.22` / `2.78` |

- 其余 5 组虽 `captured_at` 相同，两侧价格却**不相等**（例如 `1.40/2.15` vs `2.09/1.43`）。
  因此：**相同 `captured_at` 不保证两侧是同一个报价**，不得据同一时间戳就断言两侧价格可互换。
- 可确定的结论：**`500j.home_win` 与 `500lq.home_win` 指向相反的两支球队**
  （依据是 8/8 的队名互换，不是价格相等），字段同名**不**等于同一支球队。
  任何按字段名对齐的合并都是错的。
- 仍**未**确定：`500lq` 显示第一位是否等于真实场地客队。缺少独立场地证据时
  `ACTUAL_HOME_TEAM_WIN_PRICE` / `ACTUAL_AWAY_TEAM_WIN_PRICE` 不可断言，
  `MARKET_HOME_WIN_SEMANTICS` 保持 `PARTIAL`。
- 样本 8 对全部来自篮球、且仅覆盖两个 provider，**不得**外推为全量 provider 契约。

#### 4.13.7 队名对应只能由人工证明

- 中文队名的书写差异（`休斯敦火箭` / `休斯顿火箭`，`俄克拉荷马城雷霆` / `俄克拉荷马雷霆`）
  **不得**由拼写相似度推断为同一球队。
- 等价关系只能通过操作者填写的 `team_name_correspondence`
  （`provider_team` / `source_team` / `verification_status`）建立，
  且仅 `SOURCE_REFERENCE_REVIEWED` 条目可用于符号换算。
- 该对应关系是**复核证据**，**不**构成生产归并授权，**不**替代 Ed25519 签名治理。

#### 4.13.8 禁止动作

- 不得交换 `home_win` / `away_win`，不得改写主客字段，不得据此自动归并 Canonical 记录；
- 不得自动剥离或纠正星期 token，不得在运行时路径修改 `normalize_competition_number()`；
- 不得用解释结果覆盖 `jczq_no` / `matchnum` / `round` 或 Canonical ID 中的编号段。

---

### 4.14 刷新写入者所有权与跨文件一致性

- **互斥权威只有一个**：`utils.concurrency.acquire_refresh_lock()` 持有的
  `<DATA_DIR>/.refresh.lock` OS 字节锁。`active_writer()` 与 `RefreshBusyError.owner`
  只是**建议性**元数据，**绝不**用来判断锁是否空闲。
- **第二个实例不得静默**：被拒时必须显式记录持有者
  （`pid` / `host` / `action` / `started_at`）；被拒实例**不得**写入任何受保护存储。
- **代标记**：`utils.refresh_state` 在**持锁期间**写 `<DATA_DIR>/.refresh_state.json`
  （`IN_PROGRESS` / `COMPLETED` / `FAILED` + `generation` + 结束时七库摘要）。
  不在锁内写它的调用方（例如生产修复）不会声称刷新语义。
- **七文件不是一个事务**：每个 JSON 由 `atomic_write_json` 原子替换，但一次刷新跨文件
  可达数分钟，因此**不得**声称七文件原子性。读方必须用
  `refresh_state.consistency()` 判定；`consistent = False` 时**不得**把七库当作同一快照。
- **原子替换 ≠ 无逻辑丢更新**：`os.replace` 只保证单文件不撕裂。任何"读-改-写"若跨进程
  没有排他段，仍会丢掉别人的新增（见 §4.17）。不得以"用了原子写"论证并发安全。
- **一致性必须失败即关闭**，以下任一情况一律 `consistent = False`：
  标记缺失/不可读/损坏（`NO_REFRESH_STATE_MARKER` / `REFRESH_STATE_UNREADABLE` /
  `MALFORMED_REFRESH_MARKER`）、schema 不支持、状态取值未知（`UNKNOWN_REFRESH_STATUS`）、
  标记只覆盖部分库（`INCOMPLETE_MARKER`）、**七个库未全部存在**（`STORE_MISSING`）、
  摘要与标记不符（`STORES_CHANGED_SINCE_MARKER`）、读取摘要期间代号或状态改变
  （`GENERATION_CHANGED_DURING_READ`）。
- **顾问性，不是保证**：`consistency()` 返回 `advisory=True`、
  `permanent_guarantee=False`、`seven_file_atomicity=False`。一次成功观测**不**代表后续读
  仍然一致；任何非刷新写入（例如 HTTP GET 物化预测快照）都会立即让已发布摘要失效。
  需要内部一致的多文件视图时，必须显式重新验证，不得复用旧结论。
- **无标记 = 不可判定**：`NO_REFRESH_STATE_MARKER` 必须按"不可判定"处理，**不得**当作一致。
- **中断恢复**：进程被 kill 时由 OS 释放字节锁；状态停留在 `IN_PROGRESS`，读方据此判为
  混合状态；下一次刷新推进 `generation` 即可自愈，**不需要**也不允许手工删锁。
- **不得删除** `.refresh.lock`；不得用 `.gitignore` 或"隐藏旁路开关"替代该锁。
- **新增文件白名单**：一次刷新只允许新增 `.refresh.lock`、`.refresh_state.json`、
  `.snapshots.lock` 三个运行时文件，其余新增一律视为越界。

---

### 4.15 持久化人工证据与稳定身份

- **位置**：`utils.evidence_store.resolve_evidence_dir()`，优先级
  显式参数 > 环境变量 `DSL_EVIDENCE_DIR` > `~/.daily-sports-lottery/evidence`。
  **仓库内路径一律拒绝**（`EVIDENCE_DIR_INSIDE_REPOSITORY`），不依赖 `.gitignore`，
  也不得写进任何受跟踪文件。
- **包含性按解析后的真实路径判定**：`realpath` + `normcase`，因此符号链接、Windows
  目录联接（junction）、大小写别名、相对路径别名指向仓库内部时一律拒绝
  （`EVIDENCE_DIR_INSIDE_REPOSITORY`）；解析失败即失败关闭（`PATH_RESOLUTION_FAILED`）。
  外部的合法目录必须继续可用。
- **分片键**：`pair_key(sorted(canonical_ids))`。**不含**数据集指纹、队名、比分与时间；
  数据集刷新**不得**让证据字节消失。`pair_key` 只是**稳定查找地址**，**不是**新的批准：
  地址命中不等于决定仍然有效。
- **追加式历史 + 单写者**：读-校验-追加-替换必须整体位于同一把跨进程排他锁
  （`<EVIDENCE_DIR>/.evidence.lock`）内，**不得**只依赖 `os.replace`；锁获取失败即
  `EVIDENCE_WRITER_BUSY` 失败关闭，绝不覆盖。记录不可修改、不可删除。
- **追加语义**：同一 `(reviewer, decided_at)` 但内容不同 → `CONFLICTING_REWRITE`；
  完全相同的重复提交 → `IDEMPOTENT_DUPLICATE`（不产生第二份）。
- **矛盾决定**：同一对出现互斥决定时 `decision_summary()` 返回 `CONFLICTING_LABEL` 且
  `effective_decision = None`，**不得**按"最新一条"取胜；两个有效决定都保留。
- **校验复用既有 schema**：`label_provenance` 必须是 `HUMAN_REVIEWED`；
  `reviewer` 不得是 `model` / `bot` / `auto` / `ai` / `system` 等；
  `approved` / `signature` / `merge` / `surviving_canonical_id` 等生产语义字段命中即拒。
- **存储侧也要校验**：`load_pair()` 必须校验分片 schema / `kind` / `pair_key` 与
  `canonical_ids` 的一致性，并**逐条**校验历史记录；不合规即
  `CORRUPT_EVIDENCE_FILE`（附精确原因），**不得**静默重置、**不得**用空文件覆盖。
- **五个维度必须分开陈述**：持久化、当前有效性、候选身份、数据集新鲜度、生产授权。
  `inspect_record()` 恒返回 `auto_reattached = False` 与 `production_authorized = False`。
- **重绑定只能由操作者完成**：即便 `BOUND`，下一步仍是 `OPERATOR_REVIEW_REQUIRED`；
  候选 ID 变化 → `CANDIDATE_ID_CHANGED`；**候选指纹变化 →
  `CANDIDATE_FINGERPRINT_CHANGED`**（提供了 `current_candidate_fingerprint` 才校验，
  但一旦提供就必须匹配）；数据集指纹变化 → `STALE_DATASET_FINGERPRINT`；
  同一对标的多候选 → `AMBIGUOUS_IDENTITY`（**拒绝，不猜**）；队名相似度**不是**身份输入。
- **只读导出**：`export_copy()` 先**完整校验**每个分片（损坏即拒绝），复制后比对
  SHA-256，并显式声明 `point_in_time_consistent = False`
  （导出是逐文件复制，不是跨文件点时间快照）。导出目标同样不得位于仓库内。
- **来源留存 ≠ 来源核验**：`retain_source_copy()` 只留存字节 + 摘要，恒返回
  `verified_by_human = False` / `sets_review_provenance = False` /
  `sets_verification_status = False`，且必须保留原始路径与 `sha256`。
- **不打印证据正文**：任何命令、日志、异常信息都不得回显记录内容。

---

### 4.16 NBA 供给诊断分级

- **七类必须分别陈述**，不得统称 "NBA unavailable"：
  `SOURCE_HAS_NO_FIXTURE` / `SOURCE_FETCH_FAILED` / `SOURCE_PARSE_FAILED` /
  `FIXTURE_FILTERED` / `INGESTION_REJECTED` / `PERSISTENCE_FAILED` / `UI_DATE_HIDDEN`。
- **"无可用盘口"属于 `FIXTURE_FILTERED`**：这是 `_admit_market_candidates` 的既有准入契约，
  不是取数失败，也不是解析失败。
- **只读源核验**：允许直接调用 `utils.fetcher_500` 的读取函数做覆盖审计；
  **不得**调用 `scraper.refresh()`，**不得**获取写入者锁，**不得**落盘。
- **联赛标签等价**：`is_nba_league()` 把 `NBA` 与 `美国职业篮球联盟` 视为同一联赛；
  不得因标签写法不同而漏计。
- **日期可见性**：`get_available_dates_by_sport()` 是运动感知日期契约；
  选择篮球时不得停留在 `basketball` 列表之外的日期（否则 `UI_DATE_HIDDEN`）。
- **不得**因历史数据集缺少某日期就断言源侧无供给：必须先做只读核验再下结论。

---

### 4.17 预测快照的跨进程写入者所有权

- **写入者不止刷新进程**：`ensure_snapshot()` 被 `GET /`、`GET /match/<id>`、
  `GET /api/today`、`GET /api/matches`、`GET /api/match/<id>` 调用，这些 HTTP 读路径
  会**物化**赛前快照。它们与 `scraper.refresh()` 可能处于**不同进程**。
- **进程内 `RLock` 不够**：`utils.prediction_snapshots._LOCK` 只在单进程内互斥；
  跨进程必须使用 sidecar 排他锁 `<DATA_DIR>/.snapshots.lock`
  （`utils.concurrency.acquire_sidecar_lock`）。
- **读-改-写整体在锁内**：`_load_store()` → 去重判断 → `_save_store()` 必须在同一临界区；
  锁外读到的旧视图**不得**用于写回，否则会覆盖别的进程刚新增的快照。
- **不得复用 `.refresh.lock`**：GET 路径不得因为刷新进行中而失败，也不得阻塞刷新。
  两把锁的获取顺序固定为「刷新锁 → 快照锁」，**禁止**反向，避免死锁；
  `refresh()` 内部物化快照属于同一顺序，因此不会自我死锁。
- **忙时必须显式降级**：快照锁超时返回既有记录并置 `created = False`，**不写**、
  不谎报成功、不静默丢弃；`_create_or_get()` 的 `(snapshot, created)` 契约保持不变。
- **锁文件不删除**：崩溃由 OS 释放字节锁；保留锁文件不会阻塞下一个写者。
- **快照身份与时序不变**：`snapshot_id = sha256(match_id|slot|model_version)`、
  「每场每版本一条」「已有快照不可覆盖」「仅 `upcoming` 且开赛时间已知才新建」
  全部保持；本次修正**不**改变预测数学、`same_event()`、编号解释或赔率方向。

---

### 4.18 人工决定的显式持久化入口

- **唯一写路径**：`tools.nba_human_review_workbench persist --reviews <file> --confirm`。
  没有 `--confirm` 一律 `EXPLICIT_CONFIRMATION_REQUIRED`，**零写入**。
- **只读命令绝不落盘**：`validate` / `summary` / `prepare` / `show` / `pilot` 不得写入证据目录。
- **不得自动生成人工属性**：`reviewer`、`decided_at`、`decision`、来源核验结论
  全部来自操作者文件；自动化只做校验与复制。
- **先校验后持久化**：只有 `validation_status = SUCCESS` 的记录才会写入；
  被拒记录一条都不落盘。
- **来源状态分离**：`SOURCE_REFERENCE_SUPPLIED` **绝不**因持久化变成
  `SOURCE_REFERENCE_REVIEWED`；`retain_source_copy()` 不得写入任何核验结论。
- **合成夹具 ≠ 运营验收**：没有真实操作者参与时，`DURABLE_OPERATOR_WORKFLOW`
  必须报告为 `BLOCKED`/`PARTIAL`，测试夹具不得当作真实人工复核证据。

---

### 4.19 已持久证据与当前候选的对账

- **唯一对账入口**：`python -m tools.nba_human_review_workbench audit-persisted
  --data-dir <DATA_DIR> --evidence-dir <EVIDENCE_DIR> [--output …] [--markdown …]`。
  该命令对七个受保护存储与外部证据目录**都只读**：不创建缺失目录、不修复分片、
  不重绑定、不写回任何记录。
- **稳定身份只用于定位**：分片按 `pair_key(sorted(canonical_ids))` 查找当前候选。
  **不得**用队名相似度、联赛标签、开赛时间或最近邻匹配来重建绑定关系。
  `pair_key` 命中只代表"这是同一对源记录"，**不**代表决定仍然有效或已获批。
- **绑定分类**沿用 `inspect_record()`：`BOUND_TO_CURRENT_CANDIDATE` /
  `CANDIDATE_ID_CHANGED` / `CANDIDATE_FINGERPRINT_CHANGED` /
  `STALE_DATASET_FINGERPRINT` / `AMBIGUOUS_IDENTITY`。额外允许两类**非冲突**结果：
  `CANDIDATE_NOT_IN_CURRENT_DATASET`（当前数据集已无该对）与 `CORRUPT`
  （分片不可读，只报告不修复）。
- **队列侧状态词表**：`NO_STORED_EVIDENCE` / `STORED_BUT_STALE` /
  `STORED_BUT_AMBIGUOUS` / `STORED_WITH_CONFLICT` / `STORED_AND_CURRENTLY_BOUND`。
  **无论哪一种**，人工复核动作都未完成：`operator_review_required` 恒为 `true`，
  `next_required_action` 只会是 `OPERATOR_REVIEW_REQUIRED` /
  `REATTACH_BY_OPERATOR_REQUIRED` / `ADJUDICATE_CONFLICT_REQUIRED` /
  `COLLECT_OPERATOR_DECISION`。
- **计数口径**：`bound_record_count` **不是**"已独立复核的人工决定数"；
  `genuine_operator_decision_count` 只统计**本次运行显式提供的操作者文件**，
  自述 provenance 无法在本命令内独立验证，必须如实报 0 并说明原因。
- **矛盾决定**：同一对出现互斥决定时保留全部历史、`conflict = true`、
  不给出 effective decision，并按 `ADJUDICATE_CONFLICT_REQUIRED` 交还操作者。
- **输出最小化**：报告与 CLI 输出**不得**包含 `reviewer` / `notes` / `decided_at`
  或来源正文；只保留审计所需的 pair key、Canonical 对、记录数、绑定结果、
  冲突标记与下一步动作。
- **观测完成 ≠ 对账干净**：`status` 只回答"这次检查跑完了没有"。对账结论必须由
  `reconciliation_verdict` 单独陈述，四种取值互斥：
  `AUDIT_COMPLETE_AND_CLEAN` / `AUDIT_COMPLETE_WITH_CORRUPTION` /
  `AUDIT_INCOMPLETE` / `AUDIT_INPUT_UNAVAILABLE`。只有第一种可以令
  `strict_acceptance_ready = true`。
- **损坏证据永远不是 clean**：存在损坏分片（含不支持的 schema、结构合法但历史记录
  不合规）时，裁决必须是 `AUDIT_COMPLETE_WITH_CORRUPTION`，**不得**因为"其余分片
  都正常"就报 `AUDIT_COMPLETE_AND_CLEAN`；混合存储**不得**被描述为"已完全对账"。
  同一存储中合法的分片仍必须可见（`stored_record_count` 不得被清零）。
- **不可读分片不得被重置**：不重写、不修复、不用空文件覆盖；缺失的证据目录
  **不得**被创建，且只能报 `AUDIT_INPUT_UNAVAILABLE`——观测成功**不**等于对账完成。
- **空目录可以干净，但覆盖为零**：已存在但无分片的存储裁决为
  `AUDIT_COMPLETE_AND_CLEAN`，同时必须给出
  `evidence_coverage_status = NO_STORED_EVIDENCE`。
  **干净 ≠ 有人工复核覆盖**，两者必须分开陈述。
- **枚举与读取是两个步骤**：`list_pairs()` 与随后的 `load_pair()` 之间存在窗口，
  另一个获授权的写者可以替换或删除分片。因此：
  - 单条读取失败必须转成显式观测（`CORRUPT` + `detected_during=READ_AFTER_ENUMERATION`
    或 `VANISHED` + `persisted_state = STORED_BUT_VANISHED_DURING_AUDIT`），
    **不得**变成未捕获异常（那只会给出 traceback 与无法解释的退出码）；
  - 只有**两次枚举**描述同一视图时才可以声称观测一致
    （`observation_stable = true`、`stability_reason = TWO_ENUMERATIONS_AGREE`）；
    否则裁决降级为 `AUDIT_INCOMPLETE`。
  - 该一致性是**顾问性**观测：它不构成跨文件点时间原子性，最后一次读取之后的写入
    仍然无法被这次运行感知。

---

### 4.20 批次持久化的非原子性与新鲜度边界

- **逐条追加**：`persist` 对每条被接受的记录单独调用一次追加；多记录操作**不是**
  原子批次（`batch_atomic = false`）。部分成功时必须分别列出
  `persisted`（含 `APPENDED` / `IDEMPOTENT_DUPLICATE`）与 `errors`（含
  `CONFLICTING_REWRITE` / `EVIDENCE_WRITER_BUSY` 等），不得抹掉已成功的历史。
- **状态词表**：`PERSISTED` / `PARTIAL` / `FAILED`，并保留
  `EXPLICIT_CONFIRMATION_REQUIRED` / `REVIEW_FILE_REQUIRED` / `REJECTED` /
  `NOTHING_TO_PERSIST` 等前置拒绝状态。
- **新鲜度边界**：校验与追加之间数据集可能被刷新。`persist` 必须在追加后重新计算
  数据集指纹并与校验时的指纹比较：
  - 一致 → `freshness_state = UNCHANGED`；
  - 不一致 → `DATASET_CHANGED_DURING_PERSIST`，且状态**降级为 `PARTIAL`**——
    记录已落盘（历史不可抹除），但**不得**声称"当前有效"；
  - 无法重算 → `RECHECK_UNAVAILABLE`，同样不得声称新鲜。
- **不引入七文件事务**：跨文件一致性仍为顾问性（§4.14），不得用本节的检测替代事务。
- **耐久性与当前有效性必须分开陈述**：以下字段各自独立，不得互相替代——
  `durable_record_written`（是否真的写下去了）、`persisted_record_count`、
  `persistence_outcome`（`PERSISTED` / `PARTIAL` / `FAILED` / `NOT_ATTEMPTED`）、
  `freshness_check_completed`、`freshness_verified`、`freshness_state`、
  `current_dataset_binding_verified`、`batch_atomic`（恒为 `false`）。
- **`status = PERSISTED` 的确切含义**：**全部**被接受的记录都已耐久写入、**且**
  写入后的数据集指纹比较真实完成并且相等。缺任一条件即降级为 `PARTIAL`
  （已有记录仍保留）或 `FAILED`（一条都没写入）。**不得**在未验证新鲜度时返回
  `PERSISTED`。
- **四类新鲜度必须分别保留**：`UNCHANGED`、`DATASET_CHANGED_DURING_PERSIST`、
  `VALIDATION_FINGERPRINT_MISSING`、`RECHECK_UNAVAILABLE`。
  `freshness_verified` **只能**在比较真实完成且相等时为 `true`：
  - 校验结果未携带指纹 → `VALIDATION_FINGERPRINT_MISSING`，不得声称新鲜；
  - 写入后无法重新加载数据集 → `RECHECK_UNAVAILABLE`；
  - 重新加载成功但算不出指纹 → 同样是 `RECHECK_UNAVAILABLE`，
    **不得**谎报为"数据集已变更"；
  - 一条记录都没写入时 → `NOT_RECHECKED`（没有可声称新鲜的对象）。
- **幂等重复不产生新证据**：`IDEMPOTENT_DUPLICATE` 仍然计入 `persisted_count`，
  但重复提交**不**刷新证据，也**不**绕过指纹校验——指纹缺失时它同样只能得到
  `PARTIAL`。
- **写入尝试与前置拒绝分开**：`EXPLICIT_CONFIRMATION_REQUIRED` /
  `REVIEW_FILE_REQUIRED` / `DATASET_NOT_AVAILABLE` / `REJECTED` /
  `NOTHING_TO_PERSIST` 属于"连尝试都没有开始"，此时
  `persistence_outcome = NOT_ATTEMPTED`；只有真正执行过追加的调用才会出现
  `PERSISTED` / `PARTIAL` / `FAILED`。
- **不得回滚**：部分成功后不得撤销已写入的记录，也不得改写历史；未验证新鲜度只影响
  结论，不影响落盘事实。

---

### 4.21 首次操作者复核包与真实输入门

- **复核包只在本地产出**：`pilot --output-dir` 与 `prepare --output-dir` 写入
  `reports/` 或操作者指定的外部目录；`pilot` 的卷宗在
  `candidate-dossier-supplements/`，`prepare` 的在 `dossiers/`，二者**不互相覆盖**。
- **复核包内容**：优先队列、候选卷宗、数据集指纹、原始来源编号字符串、
  原始主客观测、可用独立方向证据、缺失证据要求、空白评审模板、未决登记册、
  人工复核说明。全部为**只读派生**，不含任何自动决定。
- **真实输入门**：人工决定仅在操作者**独立撰写并显式提供**时才算真实。
  `reviewer` / `decided_at` / `decision` **不得**由测试、仓库元数据、模型或当前数据集推导。
  未提供操作者文件时：`REAL_OPERATOR_INPUT = NOT_AVAILABLE`、
  `GENUINE_OPERATOR_DECISIONS = 0`、`REAL_HUMAN_REVIEW_ACCEPTANCE = BLOCKED_OPERATOR_INPUT`。
- **合成夹具不是验收**：合成测试只能验证机制，**不得**用于把
  `REAL_HUMAN_REVIEW_ACCEPTANCE` 标为 PASS。
- **真实持久化需单独授权**：即使存在操作者文件，也**不得**在未获单独明确授权时
  对真实证据目录执行 `persist --confirm`。

---

### 4.22 结果完整性与退出码契约

对账与持久化的结论必须能被**进程退出码**判定，不必先解析详细 JSON。

| 命令 | 退出码 | 含义 |
| --- | ---: | --- |
| 全部只读命令 | `0` | 观测完成且无拒绝 |
| `validate` | `4` / `5` / `6` | 评审被拒 / 证据被拒 / 两者皆有 |
| `audit-persisted` | `0` | `reconciliation_verdict = AUDIT_COMPLETE_AND_CLEAN` |
| `audit-persisted` | `7` | 观测完成但对账不干净：损坏、不稳定或证据目录缺失 |
| `audit-persisted` | `2` | 输入不可用（数据集缺失、证据路径被拒） |
| `persist` | `0` | `status = PERSISTED`（耐久 + 新鲜度已验证） |
| `persist` | `8` | 追加已执行但整体未达可验证完成（部分写入 / 新鲜度未验证） |
| `persist` | `2` | 前置拒绝，连写入尝试都没有开始 |

- `7` 与 `8` 是**新增**码，**不得**与 `2` / `3` / `4` 的既有含义复用；
  也不得把它们并入 `2`（那会把"跑完但不干净"与"输入不可用"混为一谈）。
- 退出码 `0` 只表示**本次观测/本次写入**通过严格验收，**不**表示：
  人工复核已完成、证据已由人核验、覆盖率达标、或生产授权存在。
- CLI 摘要必须直接暴露决定性字段（裁决、`corrupt_shard_count`、
  `vanished_shard_count`、`observation_stable`、`freshness_state`、
  `freshness_verified`、`persistence_outcome`），操作者不该为了发现损坏去翻详细报告。
- 任何组合都**不得**授权生产：`executable` / `apply_available` /
  `production_write_authorization` / `production_authorized` 恒为 `false`，
  `surviving_canonical_id` 恒为 `null`，`decision` 恒为 `NO-GO`。
- 合成夹具只能验证机制：通过本节的回归**不**构成 `REAL_HUMAN_REVIEW_ACCEPTANCE`。

---

### 4.23 独立来源证据的日期语义（当地日 vs 北京日）

- **两个日期不是同一概念**：`candidate.event_date` 来自 `daily_matches.json` 的
  `date`，是**北京比赛日**；证据条目的 `actual_event_date` 按 §4.12 必须保留
  **来源侧原始当地比赛日**（NBA 官方标注的美方日期）。NBA 在美国晚间开赛时
  两者通常相差 1 天（当地 `2026-10-27` → 北京 `2026-10-28`）。
- **不得用裸字符串相等判等**：`tools.nba_human_review_workbench.resolve_orientation()`
  曾直接比较 `str(actual_event_date) == str(candidate.event_date)`，
  会把「有完整时区换算依据的同一场比赛」误判为 `event date mismatch`。
  现由 `event_date_compatible()` 统一判定。
- **兼容两条且仅两条路径**：
  1. 两字符串相等 → `DATE_EQUAL`（既有行为，不削弱）；
  2. 不相等时，**仅当**证据自带显式且自洽的换算依据才兼容。
- **换算依据必须是可选扩展字段**（不改 `EVIDENCE_REQUIRED_KEYS`、不破坏既有文档）：
  `source_utc_offset`（严格 `±HH:MM`）、`event_start_local`（naive ISO-8601 本地时间）、
  `event_start_utc`（aware UTC）、`beijing_event_date`（声明的北京日期）、
  `local_time_semantics`（仅 `UNAMBIGUOUS` 可接受）。
  必须同时满足：原始当地日期 = 本地时间戳日期；`本地时间 − 偏移` = UTC 时刻；
  `UTC + 8h` 的日期 = `candidate.event_date` = 声明的北京日期。
- **失败即关闭**：字段缺失、格式非法、偏移越界、本地时间带时区、
  UTC 时刻非 UTC、`AMBIGUOUS` / `NONEXISTENT` / 未声明的本地时间、
  换算不一致、声明北京日期不符 —— 一律不兼容，回退到严格日期相等。
- **用显式 UTC 偏移而非 IANA 时区库**：Windows 无系统 `tzdata`、`zoneinfo` 不可用，
  而 CI 为 Linux；依赖 IANA 数据库会造成平台判定不一致。换算只用
  `datetime` 算术，跨平台确定。偏移值必须由操作者从独立来源证实，不得推算。
- **不得**从竞彩编号的星期 token 推断开赛时间；不得用换算结果覆盖
  `actual_event_date` 或任何源记录时间字段；不得据换算推断场地方向。
- **日期换算通过 ≠ 人工核验**：条目仍需 `SOURCE_REFERENCE_REVIEWED` +
  非空 `checked_by`（§4.11/§4.12）才能支持方向；日期比较通过**不**产生
  `HUMAN_REVIEWED` 溯源，**不**建立跨源同一性，**不**授权任何归并或生产写入。
- `resolve_orientation()` 的 `date_semantics` 字段逐条给出判定原因，供只读审计；
  `authoritative` 恒为 `false`。

---

### 4.24 测试完整性：受保护存储边界、运行时工件与回归证据

- **历史哈希不是不变式**：`tests/test_evidence_concurrency_and_consistency.py` 的
  `test_er_24_*` 曾把某一时刻真实数据集的七个 SHA-256 **写死**在测试里。一次合法的
  真实刷新就会改写它们——那不是越权写入。**不得**用历史常数判定"当前数据是否被篡改"，
  也**不得**为了让历史常数成立去改写 `data/*.json`。
- **完整性必须绑定操作边界**：正确口径是 T0 观测 → 执行被检验的**只读操作** → T1 观测，
  逐库比较字节。任何初始摘要都合法，只要求它在边界内保持不变。
  **不得**把哈希算两遍而中间不做任何被检验的操作（空转测试不算证据）。
- **结果必须分开陈述**：`PROTECTED_STORES_UNCHANGED` / `UNAUTHORIZED_TEST_MUTATION` /
  `EXTERNAL_CONCURRENT_REFRESH` / `PROTECTED_STORE_MISSING` / `PROTECTED_STORE_UNREADABLE`。
  「缺失」与「未变」**不**等价；「摘要变化」**不**自动等于「越权写入」。
  并发刷新归因只依据刷新标记的代号/状态，只用于**诊断**，不改变失败结论；
  **不得**用"可能是别人刷新的"来豁免一次边界内的真实变化。
- **跳过策略只覆盖"没有私有数据集"**：干净检出缺少 `daily_matches.json` 时按既有策略
  `skip`；真实数据集存在而任一受保护库缺失/不可读时必须**显式失败**，
  既不得静默跳过，也不得补文件、不得伪造数据。
- **版本化作用域 ≠ 未跟踪运行时清单**：`test_rb33_*` 曾把
  `git ls-files --cached` 与 `git ls-files --others` **合并**后与历史基线比较，
  于是 `data/.snapshots.lock` 这类合法运行时锁被读成「未授权的版本化变更」。
  两者必须**分开陈述**：
  - **版本化作用域**：`ALLOWED_PATHS` 之外的跟踪集合必须仍等于历史基线集合，
    且这些文件逐字节不变；新增、删除、改动任一越界即失败。
  - **未跟踪运行时清单**：逐条分类、逐条可见；**不得**并入版本化判定，
    也**不得**被说成"已授权的代码变更"。
- **运行时锁是运行时工件，不是版本化代码**：`data/.refresh.lock`、
  `data/.refresh_state.json`、`data/.snapshots.lock` 只按**精确路径**容忍；
  **不得**写入 `ALLOWED_PATHS`、**不得**提交、**不得**删除；
  也**不得**用 `.gitignore` 或 `.git/info/exclude` 把失败的检查藏起来
  （本机曾用排除行掩盖该失败，属违规做法，已移除）。
- **精确豁免不得掩盖其它路径**：`data/` 内只允许上述已知旁车，其余未跟踪工件一律拒绝；
  `reports/` 是 §5.1 规定的本机工件根，其内容只计入清单。
  但**敏感密钥后缀**（`.pem` / `.key` / `.pfx` / `.p12` / `.der` / `.jks` /
  `.keystore` / `.kdbx` / `.gpg` / `.asc`）无论出现在哪里都**必须**拒绝，包括 `reports/` 内；
  受保护代码目录中出现的未跟踪可执行源文件（`.py` 等）同样**必须**拒绝。
- **符号链接 / 目录联接不得绕过**：已知旁车路径若本身是符号链接，或解析后父目录不是
  `data/`，一律失败关闭（`RUNTIME_SIDECAR_PATH_SUBSTITUTED`）。
- **无关的未跟踪本地笔记不构成版本化变更**：`LOCAL_UNRELATED_ARTIFACT` 只进入清单，
  不改变版本化结论；它**绝不**被重新归类为已授权变更，也**绝不**因此从清单里消失。
- **对抗性回归必须真实执行**：上述规则由**临时 Git 仓库 / 临时目录**上的负向测试证明
  （单库改动、多库改动、删除、等长替换、敏感密钥、可执行源文件、符号链接替换、
  新增跟踪路径、删除基线路径、合法起始摘要不误报）。
  **不得**为模拟负向场景改动本仓库的 Git 历史，也不得改动真实受保护存储。
- **回归证据必须来自真实进程**：聚焦与全量回归都必须保留**命令、开始/结束时间、退出码、
  通过/失败/跳过数、失败名与最终摘要行**。`EXIT_CODE = 0` 是唯一可声称「绿」的依据；
  **不得**由进度符号（`.` / `F` / `s`）推断通过；输出被中断或截断时只能记 `INCOMPLETE`，
  **不得**重建结论。日志写入本机 `reports/`，**不得**提交、不得写进 `data/`。
- **解释器必须显式选定**：`cryptography` 只安装在项目 `.venv` 中；用系统解释器跑全量会让
  信任根相关测试在 setup 阶段报 `Ed25519 backend ... unavailable`。
  这是**环境**差异而非代码回归，必须用项目虚拟环境执行并如实标注解释器。

---

### 4.25 忽略工件清单、NBA 联赛口径与只读端点边界

- **忽略路径不是不可见路径**：`git ls-files --others --exclude-standard` 不会列出被忽略目录的
  子项（本机 `reports/` 被 `.git/info/exclude` 整目录排除），只按该命令收集会让
  `reports/*.pem` 绕过「敏感密钥在哪都拒绝」的契约。清单必须**同时**读取
  `git ls-files --others --ignored --exclude-standard`。
- **忽略扫描必须显式限定**：只扫描承载仓库工件的根（`reports/`、`data/`、`artifacts/`、
  `.runtime/`、`instance/`、`docs/analysis/`）。依赖与构建树（`.venv/`、
  `frontend/node_modules/`、`frontend/dist/`）是第三方产物，含公开 CA 证书
  （实测 `.venv/Lib/site-packages/certifi/cacert.pem`），**不得**纳入扫描。
- **忽略工件只做一件事**：敏感密钥后缀一律拒绝（`SENSITIVE_KEY_ARTIFACT_IGNORED`）；
  已知运行时旁车仍按精确路径放行；其余计为 `IGNORED_LOCAL_ARTIFACT`，
  **永不**被当作已授权版本化变更，也**永不**因被忽略而从清单中消失。
- **折叠目录必须展开**：Git 可能返回被忽略目录本身（`reports/`）而不是其子项，
  两种形态必须给出同一文件列表（只枚举名字，不读取内容，不跟随符号链接）。
- **NBA 口径排除 WNBA**：`fetcher_500.is_nba_league()` 曾因 `"NBA" in "WNBA"` 把女子联赛
  计入 NBA 供给（真实数据上多计 3 场、凭空多出 2 个「NBA 比赛日」）。
  `WNBA` / `美国女子篮球联盟` **不**属于 NBA；该修正只改统计口径，
  **不**改变市场准入、赔率合并或主客方向。
- **只读端点的真实边界**（隔离合成存储实测）：`/api/dates`、`/api/status`、
  `/api/match/<id>/snapshots` **不写任何存储**；`/api/today`、`/api/matches`、
  `/api/match/<id>` 会物化赛前预测快照（隔离夹具中还会触达球队库），
  **不得**当作只读探针使用。运营验收只允许使用上述三个不写库的 GET 端点。
- **运动感知日期不是可选装饰**：当日默认日期可能只有足球（实测 `2026-11-12` 仅足球）；
  前端必须按 `dates_by_sport` 选择该运动的日期，否则会把「当日无篮球」误报成「没有比赛」。
- **源比数据集新不是代码缺陷**：源已列出而数据集缺失的赛事，若其比赛日晚于
  `meta.last_refresh` 且数据集该日无任何记录，原因是「源在最近一次刷新之后才发布」，
  记为 `FIXTURE_PENDING_NEXT_REFRESH`，**不得**记为 `PERSISTENCE_FAILED`。

---

## 五、本地报告与交付顺序

### 5.1 报告只存在于本机，绝不进入远程

- 最终报告写入 `<仓库根>/reports/`，该目录**只属于本地检出**。
- 排除规则通过 Git 本地元数据建立，使用
  `git rev-parse --git-path info/exclude` 定位文件（兼容 `.git` 为文件的 worktree），
  并**幂等**追加 `/reports/`；不得用 `.gitignore` 代替。
- **不得** `git add -f reports/`；报告不得进入任何提交或推送。
- 该规则属于**本机 Git 元数据**，因此**不会**出现在全新克隆（包括 CI checkout）中；
  CI 不需要也不得创建它。本地校验命令如下（在未配置该规则的全新克隆上会输出为空，
  这属于预期环境差异，不代表规则缺失）。
- 评审工作流产出的候选队列、评审记录、预览文件同样属于本机工件：
  默认写入 `reports/` 或仓库之外的临时目录，**不得**写入 `data/`，也不得提交。
- 验证：`git check-ignore -v reports/<文件>` 必须命中；`git ls-files -- reports/` 必须为空。

### 5.2 远程 CI 必须先于最终报告完成

固定顺序：**提交 → 推送 → 定位最终 SHA 的 CI → 等待终态 → 核对每个 job → 才写报告**。

- 不得用旧 SHA 的 CI 结果作为验收证据；
- 不得以「各个 job 成功」推断「workflow 成功」，必须以 workflow 终态为准；
- 终态为失败 / 取消时，记录真实结果并前向修复后重新验证，**不得**放弃 CI 直接写成功报告；
- 未取得可验证终态前，**不得**生成最终报告，也不得写占位报告。

### 5.3 精确 SHA 的 CI 校验

- 以命令定位：`gh run list --commit <FINAL_SHA>`、`gh run view <RUN_ID> --json
  databaseId,headSha,status,conclusion,jobs,url`，必要时 `gh run watch <RUN_ID> --exit-status`。
- 记录 run id、URL、`headSha`、`status`、`conclusion` 与每个必需 job 的名称与结论。
- 仓库 CI 为 `.github/workflows/ci.yml`，`develop` 触发，四个 job：
  `offline-tests`（ubuntu / windows）与 `staging-validation`（ubuntu / windows）。
- **阻断门是 Windows**：`ubuntu-latest` 带 `continue-on-error: ${{ matrix.os == 'ubuntu-latest' }}`，
  属**顾问性信号**——Linux 环境差异不得掩盖或阻断 Windows 回归。
  验收时必须分别陈述：Windows 两项是否成功（阻断）、Ubuntu 两项的实际结论（顾问）。
  **不得**把"Ubuntu 通过"当作跨平台结论，也**不得**把"Ubuntu 失败"当作代码回归证据。

---

## 六、验收

- 提交前运行 `python -m pytest -q`（项目虚拟环境），并在回复中报告**真实执行结果**。
- 全量回归必须保留**进程退出码**与**最终摘要行**（见 §4.24）。只有退出码为 `0`
  才能声称本地全绿；摘要缺失时记 `INCOMPLETE`，**不得**由进度符号推断通过。
- 必须分别标注三类结论：`SYNTHETIC_TEST_VERIFIED`、`REAL_DATA_MEASURED`、
  `REAL_DATA_NOT_AVAILABLE`，不得混同。
- 重复评审工作流必须**真实执行**一次 `generate` / `validate` / `preview`
  （本机有真实数据时对真实数据执行，否则对隔离夹具执行），并如实报告：
  候选数、已评审数、待评审数、被拒绝记录数与预览结论。
- 依赖人工标签的精度 / 召回指标，只有在**独立评审决定存在**时才能报告，
  且必须注明其来源为自述本地评审（非签名组织批准）；否则记 `N/A`。
- CI：`.github/workflows/ci.yml`，push / PR 到 `develop` 触发，
  矩阵 `ubuntu-latest` + `windows-latest` × Python 3.12。
  **最终 SHA 上 Windows 两个 job 必须 `success`**；Ubuntu 两个 job 为顾问信号
  （`continue-on-error`），其结论必须如实记录，但**不**构成阻断。
