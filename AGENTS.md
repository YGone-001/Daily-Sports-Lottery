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

---

## 六、验收

- 提交前运行 `python -m pytest -q`，并在回复中报告**真实执行结果**。
- 必须分别标注三类结论：`SYNTHETIC_TEST_VERIFIED`、`REAL_DATA_MEASURED`、
  `REAL_DATA_NOT_AVAILABLE`，不得混同。
- 重复评审工作流必须**真实执行**一次 `generate` / `validate` / `preview`
  （本机有真实数据时对真实数据执行，否则对隔离夹具执行），并如实报告：
  候选数、已评审数、待评审数、被拒绝记录数与预览结论。
- 依赖人工标签的精度 / 召回指标，只有在**独立评审决定存在**时才能报告，
  且必须注明其来源为自述本地评审（非签名组织批准）；否则记 `N/A`。
- CI：`.github/workflows/ci.yml`，push / PR 到 `develop` 触发，
  矩阵 `ubuntu-latest` + `windows-latest` × Python 3.12；最终 SHA 上四个 job 必须全部成功。
