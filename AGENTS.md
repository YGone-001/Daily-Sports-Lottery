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

## 四、验收

- 提交前运行 `python -m pytest -q`，并在回复中报告**真实执行结果**。
- CI：`.github/workflows/ci.yml`，push / PR 到 `develop` 触发，
  矩阵 `ubuntu-latest` + `windows-latest` × Python 3.12。
