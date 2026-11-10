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
# 只读命令（全部不落盘）
python -m tools.nba_human_review_workbench prepare --output-dir <dir>
python -m tools.nba_human_review_workbench show --candidate-id <id>
python -m tools.nba_human_review_workbench validate --reviews <file>
python -m tools.nba_human_review_workbench summary
python -m tools.nba_human_review_workbench pilot --output-dir <dir>

# 唯一写路径（需显式确认）
python -m tools.nba_human_review_workbench persist \
  --reviews <file> --evidence-dir <仓库外目录> --confirm

# 只读对账
python -m tools.nba_human_review_workbench audit-persisted \
  --evidence-dir <仓库外目录> --output <本地路径>/recon.json [--markdown <本地路径>/recon.md]
```

`audit-persisted` 的行为约定：

| 情况 | 报告 | 自动动作 |
| --- | --- | --- |
| 全部可读且两次枚举一致 | `AUDIT_COMPLETE_AND_CLEAN` | 无 |
| 存在损坏分片（含不支持的 schema、历史记录不合规） | `AUDIT_COMPLETE_WITH_CORRUPTION` + `corrupt_shard_count` | **不修复、不重写** |
| 枚举与读取之间分片被替换/删除，或两次枚举不一致 | `AUDIT_INCOMPLETE` + `observation_stable=false` | **不重试到底、不猜测** |
| 证据目录不存在 | `AUDIT_INPUT_UNAVAILABLE`，`evidence_store_status = NOT_PROVISIONED` | **不创建** |
| 当前数据集已无该对 | `CANDIDATE_NOT_IN_CURRENT_DATASET` | **不猜测** |
| 同一对命中多个候选 | `AMBIGUOUS_IDENTITY` | **不裁决** |
| 指纹漂移 | `STALE_DATASET_FINGERPRINT` / `CANDIDATE_FINGERPRINT_CHANGED` | **不重绑定** |
| 决定互相矛盾 | `conflict = true`，无 effective decision | **不按时间取胜** |
| 绑定成功 | `BOUND_TO_CURRENT_CANDIDATE` | 仍需人工复核；**不授权生产** |

> `pair_key` 命中只是"同一对源记录"的**查找地址**，不是新的批准。
> `bound_record_count` **不是**"已独立复核的人工决定数"。

### 5.1.1 独立方向证据的日期字段

`validate --evidence <file>` 读取的证据文档顶层为 `{"evidence": [...]}`；
每条记录必须带 `EVIDENCE_REQUIRED_KEYS` 中的 14 个字段（见工作台模板）。
其中 `actual_event_date` **保留来源侧原始当地比赛日**，而候选的
`event_date` 是 `daily_matches.json` 的**北京比赛日**——两者常相差 1 天。

因此证据条目可**可选**携带一组换算字段，用于证明「不同日历日、同一场比赛」：

| 字段 | 要求 |
| --- | --- |
| `source_utc_offset` | 严格 `±HH:MM`（如 EDT 写 `-04:00`），须由独立来源证实 |
| `event_start_local` | naive ISO-8601 本地时间，不带时区 |
| `event_start_utc` | aware UTC 时刻（`Z` 或 `+00:00`） |
| `beijing_event_date` | 声明的北京日期，须与换算结果一致 |
| `local_time_semantics` | 仅 `UNAMBIGUOUS` 可接受 |

判定条件：`原始当地日期 = 本地时间戳日期`、`本地时间 − 偏移 = UTC 时刻`、
`UTC + 8h 的日期 = 候选 event_date = 声明北京日期`。缺任一字段、格式非法、
换算不自洽或本地时间歧义，一律回退为**严格日期相等**（失败即关闭）。

这些字段是**可选扩展**，不加入 `EVIDENCE_REQUIRED_KEYS`，既有证据文件不受影响。
换算通过**不**等于人工核验：条目仍需 `verification_status = SOURCE_REFERENCE_REVIEWED`
与非空 `checked_by` 才支持方向；也不推断场地方向、不授权归并。
详见 `AGENTS.md` §4.23。

### 5.2 `status` 与 `reconciliation_verdict` 不是一回事

`status` 只说明**这次检查跑完了**（`SUCCESS` / `NOT_AVAILABLE` / `FAIL_CLOSED`）。
对账结论看 `reconciliation_verdict`：

| 裁决 | `strict_acceptance_ready` | 退出码 |
| --- | --- | ---: |
| `AUDIT_COMPLETE_AND_CLEAN` | `true` | `0` |
| `AUDIT_COMPLETE_WITH_CORRUPTION` | `false` | `7` |
| `AUDIT_INCOMPLETE` | `false` | `7` |
| `AUDIT_INPUT_UNAVAILABLE` | `false` | `7`（数据集缺失或路径被拒时为 `2`） |

- **损坏证据永远不会是 clean**：只要存在一个读不出来的分片，裁决就不可能是
  `AUDIT_COMPLETE_AND_CLEAN`，退出码也不可能是 `0`。
- **混合存储不是"已完全对账"**：干净分片仍然会被计数（`stored_record_count` 不清零），
  但整体裁决仍是 `AUDIT_COMPLETE_WITH_CORRUPTION`。
- **干净 ≠ 有覆盖**：已存在但为空的分片目录裁决为 `AUDIT_COMPLETE_AND_CLEAN`，
  同时 `evidence_coverage_status = NO_STORED_EVIDENCE`。
  退出码 `0` **不**代表已经有人工复核证据。
- **缺失目录不等于已对账**：`evidence_store_status = NOT_PROVISIONED`，
  裁决为 `AUDIT_INPUT_UNAVAILABLE`——观测成功，但对账从未完成。
- **并发窗口是顾问性边界**：`list_pairs()` 与 `load_pair()` 之间允许另一个写者替换
  分片。因此本命令只做**两次枚举**的确认，一致才标记 `observation_stable=true`；
  它**不**声称跨文件点时间原子性，最后一次读取之后的写入无法被本次运行感知。

### 5.3 `persist` 的结果字段

| 字段 | 含义 |
| --- | --- |
| `persistence_outcome` | 耐久性结论：`PERSISTED` / `PARTIAL` / `FAILED` / `NOT_ATTEMPTED` |
| `status` | **操作验收**结论：只有"全部写入 + 新鲜度已验证"才是 `PERSISTED` |
| `durable_record_written` / `persisted_record_count` | 是否真的写下去了、写了几条 |
| `freshness_state` | `UNCHANGED` / `DATASET_CHANGED_DURING_PERSIST` / `VALIDATION_FINGERPRINT_MISSING` / `RECHECK_UNAVAILABLE` / `NOT_RECHECKED` |
| `freshness_verified` | 只有真实比较完成且相等才为 `true` |
| `batch_atomic` | 恒为 `false`：逐条追加，不是事务 |

| 情形 | `status` | 退出码 |
| --- | --- | ---: |
| 全部写入 + 指纹未变 | `PERSISTED` | `0` |
| 全部写入但数据集已变 / 指纹缺失 / 无法重算 | `PARTIAL` | `8` |
| 部分写入（含来源留存失败） | `PARTIAL` | `8` |
| 一条都没写入 | `FAILED` | `8` |
| 未确认 / 无评审文件 / 数据集缺失 / 评审被拒 | 前置拒绝态 | `2` |

> 已写入的记录**永不回滚**。新鲜度未验证只影响结论，不影响落盘事实。
> 幂等重复（`IDEMPOTENT_DUPLICATE`）不产生新证据，也不绕过指纹校验。

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