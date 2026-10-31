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
- CI：`.github/workflows/ci.yml`，push / PR 到 `develop` 触发，
  矩阵 `ubuntu-latest` + `windows-latest` × Python 3.12；最终 SHA 上四个 job 必须全部成功。
