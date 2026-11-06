# 重复赛事评审与 Canonical 归并预览（离线、只读）

本文件说明 `tools/real_sports_duplicate_review.py` 的工作方式与边界。
一句话：**它把"看起来像同一场比赛"的证据摆到人面前，让人自己下结论，并把结论的影响算清楚——但它不合并、不批准、不写入。**

> 生产状态：`runtime_mapping_activation = NOT_ENABLED`、
> `production_mapping_write_authorization = NOT_GRANTED`、`DECISION = NO-GO`。
> 本工作流**不**构成生产授权，也**不**替代 `utils/mapping_governance` 的签名所有者治理。

---

## 1. 为什么需要它

真实数据审计（`docs/analysis/real-sports-identity-mapping-evidence.md`）给出的是**计数**，
不是**结论**：有多少组疑似重复、多少组歧义、多少条身份不足。要把这些数字变成业务判断，
必须有人逐组看过、签字（或至少署名）、并知道"如果合并会发生什么"。

本工具提供这条链路：

```text
生成候选 → 人看证据 → 人写决定 → 校验决定 → 预览影响
```

每一步都只读写本机文件，且**只读**七个受保护存储。

---

## 2. 候选分类（诊断，不是裁决）

| 类别 | 含义 | 是否等同"同一赛事" |
| --- | --- | --- |
| `HIGH_EVIDENCE_DUPLICATE_CANDIDATE` | 队名、日期、开赛时间、赛事、编号、赛果全部一致 | **否**，仍需人工确认 |
| `AMBIGUOUS_NAME_CANDIDATE` | 同一时段，但至少一方队名被截断/缩写 | **否**，字段证据不足以判等 |
| `INSUFFICIENT_SOURCE_IDENTITY` | 该记录的身份键不足以授权活动绑定；若存在更强关联则成对出现 | **否**，且不得据此确立归属 |
| `NEGATIVE_CONTROL_CANDIDATE` | 刻意选取的"不同事件"或"证据冲突"对照，用于校准评审者 | **否**，用于防止"照单全收" |

对照形态（数据支持多少就产出多少，每形态有上限）：

`SAME_TEAMS_DIFFERENT_DATE`、`SAME_TEAMS_DIFFERENT_KICKOFF`、`DIFFERENT_COMPETITION`、
`DIFFERENT_SPORT_SAME_TEAMS`、`CONFLICTING_COMPETITION_NUMBER`、
`CONFLICTING_FINAL_SCORE`、`SIMILAR_TEAM_NAMES`、`DIFFERENT_TEAMS`。

每个候选携带：稳定 `candidate_id`、`candidate_fingerprint`、相关 Canonical ID、
数据 SHA-256、运动/日期/开赛时间/赛事、双方队名、原始源标识符与规范化编号、
生命周期状态、全场比分、预测/赔率/结算/评估引用、分类依据、支持与矛盾证据、
证据质量、以及初始状态 `UNREVIEWED`。

---

## 3. 人工评审记录

```json
{
  "kind": "READ_ONLY_DUPLICATE_REVIEW_DECISIONS",
  "schema_version": "1.0.0",
  "dataset_fingerprint": "<生成时的 dataset_fingerprint>",
  "reviews": [
    {
      "candidate_id": "CAND-…",
      "candidate_fingerprint": "…",
      "dataset_fingerprint": "…",
      "canonical_ids": ["…", "…"],
      "decision": "SAME_EVENT",
      "label_provenance": "HUMAN_REVIEWED",
      "reviewer": "reviewer-name",
      "decided_at": "2026-11-01T10:00:00+08:00",
      "evidence_reference": "附带的截图 / 官方赛程 URL / 工单号",
      "notes": "为何这样判定",
      "schema_version": "1.0.0"
    }
  ]
}
```

允许的 `decision`：`SAME_EVENT`、`DIFFERENT_EVENT`、`INSUFFICIENT_EVIDENCE`、
`REQUIRES_FURTHER_REVIEW`。**没有记录 = `UNREVIEWED`**；写入 `UNREVIEWED` 会被拒绝。

### 3.1 校验规则（全部 fail-closed）

| 拒绝码 | 触发条件 |
| --- | --- |
| `UNSUPPORTED_SCHEMA_VERSION` | 文件 schema 版本不符 |
| `DATASET_FINGERPRINT_MISMATCH` | 文件或记录绑定的数据集不是当前数据 |
| `PROHIBITED_REVIEW_FIELD` | 记录中出现 `approved` / `signature` / `apply` / `merge` / `delete` / `surviving_canonical_id` 等 |
| `INCOMPLETE_REVIEW` | 缺字段，或 decision 写成 `UNREVIEWED` |
| `UNSUPPORTED_DECISION` | 不在允许集合内的决定 |
| `AUTOMATED_LABEL_REJECTED` | `label_provenance` 不是 `HUMAN_REVIEWED` |
| `MISSING_REVIEWER` / `INVALID_TIMESTAMP` / `MISSING_EVIDENCE_REFERENCE` | 归属不可审计 |
| `UNKNOWN_CANDIDATE` | 候选 ID 不在本队列中 |
| `STALE_CANDIDATE_FINGERPRINT` | 候选证据已变化 |
| `CANONICAL_IDS_MISMATCH` | 记录里的 Canonical ID 与候选不一致 |
| `CONFLICTING_LABEL` | 同一候选出现互相矛盾的决定 |
| `DUPLICATE_DECISION` | 同一候选被重复记录同一决定 |

> 自述 `reviewer` 是**审计元数据**。它不是 Ed25519 所有者签名，也不能让任何绑定变成活动绑定。

---

## 4. 预览：只算影响，不做决定

`preview` 对每个候选输出：

- 涉及的 Canonical ID 与建议的关系（`PROPOSED_SAME_EVENT` / `CONFIRMED_DISTINCT` /
  `UNREVIEWED` / …）；
- 是否已存在 Canonical 归属冲突（同一事件被两条记录拥有）；
- 源身份键是否充分、是否共享作用域、是否重复身份键；
- 预测 / 赔率 / 结算 / 评估四类引用的数量与样例 ID；
- **历史标识符保留要求**（`snapshot_id` 由 `match_id` 派生，改写即不可复现）；
- 需要的治理证据与当前阻断原因；
- `surviving_canonical_id = null` 与"不选存活者"的理由。

固定声明：`executable=false`、`apply_available=false`、
`production_write_authorization=false`、`no_mutation_performed=true`、`decision=NO-GO`。

**明确不做的事**：按字典序 / 源层级 / "缺编号"挑存活者；删除较弱记录；
把"移除记录后规划器重放成功"当作删除许可；生成可立即执行的修复清单。

---

## 5. CLI

```bash
python -m tools.real_sports_duplicate_review generate \
  --data-dir <只读数据目录> --output <本地路径>/candidates.json

python -m tools.real_sports_duplicate_review validate \
  --data-dir <只读数据目录> --reviews <本地路径>/decisions.json

python -m tools.real_sports_duplicate_review preview \
  --data-dir <只读数据目录> --reviews <本地路径>/decisions.json \
  --output <本地路径>/preview.json
```

| 退出码 | 含义 |
| ---: | --- |
| 0 | 成功 |
| 2 | 输入错误（存储或评审文件格式/结构） |
| 3 | 输出路径被拒（`data/`、生产根，或检出内非 `reports/` 路径） |
| 4 | 存在被拒绝的评审记录（fail closed） |

输出位置：`reports/`（本机 Git 排除）或仓库外的临时目录。**不得**写入 `data/`。
JSON 是主工件（确定性、`sort_keys`），同时提供人读 Markdown 摘要。

### 5.1 NBA 复核工作台：已持久证据对账

人工决定持久化后，用只读对账命令确认它们与**当前**候选数据集的关系：

```bash
# 5 个只读命令
python -m tools.nba_human_review_workbench prepare --output-dir <dir>
python -m tools.nba_human_review_workbench show --candidate-id <id>
python -m tools.nba_human_review_workbench validate --reviews <file>
python -m tools.nba_human_review_workbench summary
python -m tools.nba_human_review_workbench pilot --output-dir <dir>

# 唯一写路径（需显式确认）
python -m tools.nba_human_review_workbench persist \
  --reviews <file> --evidence-dir <仓库外目录> --confirm

# 只读对账（本阶段新增）
python -m tools.nba_human_review_workbench audit-persisted \
  --evidence-dir <仓库外目录> --output <本地路径>/recon.json [--markdown <本地路径>/recon.md]
```

`audit-persisted` 的行为约定：

| 情况 | 报告 | 自动动作 |
| --- | --- | --- |
| 证据目录不存在 | `evidence_store_status = NOT_PROVISIONED` | **不创建** |
| 分片损坏 | `CORRUPT` + `corrupt_shard_count` | **不修复** |
| 当前数据集已无该对 | `CANDIDATE_NOT_IN_CURRENT_DATASET` | **不猜测** |
| 同一对命中多个候选 | `AMBIGUOUS_IDENTITY` | **不裁决** |
| 指纹漂移 | `STALE_DATASET_FINGERPRINT` / `CANDIDATE_FINGERPRINT_CHANGED` | **不重绑定** |
| 决定互相矛盾 | `conflict = true`，无 effective decision | **不按时间取胜** |
| 绑定成功 | `BOUND_TO_CURRENT_CANDIDATE` | 仍需人工复核；**不授权生产** |

> `pair_key` 命中只是"同一对源记录"的**查找地址**，不是新的批准。
> `bound_record_count` **不是**"已独立复核的人工决定数"。

---

## 6. 与既有安全链的关系

| 既有机制 | 本工具的态度 |
| --- | --- |
| `utils/mapping_governance`（Ed25519 批准、账本） | **未调用**；预览输出不产生 APPROVED，也不写账本 |
| `utils/ingestion_identity`（规划器 fail-closed） | 只读复用其结论（例如归属冲突仍被拒绝） |
| `utils/source_identity`（身份分类） | 唯一身份来源；本工具不复制第二套分类引擎 |
| 七个受保护存储 | 只读；前后 SHA-256 必须一致，否则中止 |

---

## 7. 目前的能力边界

1. 队列是**生成**的，不是审完的；没有人工决定时覆盖率恒为 0，且不报告精度/召回。
2. 对照项每形态有上限，队列长度受 `--max-controls` 控制；数据支持更多，但无意义地刷量
   不会提高评审质量。
3. 预览不理解未来业务规则（例如赛制变更），它只陈述**当前数据事实**与**证据缺口**。
4. 真实数据结论只在本机成立；CI 无该私有数据集，只验证合成行为与安全不变量。