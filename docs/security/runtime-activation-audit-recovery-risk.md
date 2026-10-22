# 运行期激活：审计信任与恢复风险评估

**基线**：`7b44e4c36b162c7ef73b07bda016397c28e7acdf`
**范围**：`ShadowActivationAuditStore`、`ShadowAuditCommitReceipt`、`ShadowActivationSimulator`、`ShadowRuntimeReader.re_enable()`
**性质**：只读评审；不实现生产审计服务

> **整改更新（B-04 / B-05）**：新增外部签名审计链检查点
> （`ShadowActivationAuditStore.verify_external_checkpoint`，要求链尖与长度**精确匹配**）
> 与签名恢复证据（`utils/recovery_authority.py`，要求 `divergence=NONE`）。
> 严格回滚与严格再启用会强制使用它们；基础 `verify()` 仍然只证明内部一致性，
> 不单独检测尾部截断。

---

## 1. 现状与不变量

| 属性 | 实现 | 证据 |
| :--- | :--- | :--- |
| 追加前完整校验 | 锁内读取完整历史 -> 哈希链/序号/事件 ID/事件形状/严格格式 -> 追加 | `GC-17 ~ GC-22`、`AC-06/07` |
| 严格物理格式 | LF-only、禁止空行、禁止未终止尾记录、禁止 `\r` | `GC-21/22` |
| 崩溃/撕裂处理 | fail closed，绝不自动截断或修复，失败时原始字节不变 | `GC-22`、`RC-08` |
| 持久化 | 单行写入 + `flush` + `fsync` | `append()` |
| 提交收据 | 存储身份 + 事件 ID + 序号 + 条目哈希 + 前驱哈希 + `persisted` | `RC-04/05` |
| 回滚提交点 | 完成事件**验证通过**后才切换有效指针 | `RC-05/07/08/09`、`SR-14` |
| 不确定结果 | `UNCERTAIN` + 停用 + 失效指针 + 保留字节 + 差异报告 | `RC-03/09` |
| 重启恢复 | 只读重建，不恢复活动状态 | `RC-11`、`SG-38` |
| 外部信任 | 默认**无**；严格模式提供外部签名检查点与恢复证据（发布方仍是外部密钥） | `test_sr_18`、`test_b04_*`、`SG-39` |

---

## 2. 威胁分析

### 2.1 全链重写

**攻击**：拥有审计文件写权限的攻击者重写全部事件并重新计算每条 `entry_hash` 与前驱绑定。
**结果**：`verify()` 通过（链内部自洽），但仍可进一步截断尾部。
**结论**：基础链**无法检测**；启用严格模式的外部签名检查点后，链尖 + 长度绑定可检出整链替换与截断。
**分类**：基础模式 **PRODUCTION_BLOCKER**；严格模式 **ISOLATED_PROTOTYPE_ONLY**（生产独立检查点发布方仍缺失）。

### 2.2 有效链尾截断

**攻击**：删除最后 N 条完整、LF 终止的记录，保留合法链。
**结果**：`verify()` 返回 `integrity=OK`，`entry_count` 减少（`test_sr_17` 已执行验证）。
**结论**：基础 `verify()` **无法检测**；严格模式的签名检查点会因链尖/长度不匹配而拒绝（`test_b04_checkpoint_detects_tail_truncation`）。
**分类**：基础模式 **PRODUCTION_BLOCKER**；严格模式 **ISOLATED_PROTOTYPE_ONLY**。

### 2.3 收据伪造与适配器替换

| 子场景 | 结果 | 分类 |
| :--- | :--- | :--- |
| `append()` 返回 `None` | 拒绝，`NOT_COMMITTED`/`UNCERTAIN` | 已修复（RC-02） |
| 返回无关/无效收据 | 拒绝，fail closed | 已修复（RC-04） |
| 正常写入但确认丢失 | `UNCERTAIN`，事件可见但指针不切换 | 已修复（RC-03） |
| 审计对象同时伪造 `append/records/verify/receipt_for` | 进程内回读可被欺骗 | **PRODUCTION_BLOCKER**（必须由独立、受信任的审计服务提供回执；原型无法自证） |

**注意**：收据中的 `store_identity` 是内容指纹，不是签名；它不能抵抗拥有写权限的适配器。

### 2.4 事件已提交但指针未应用

**攻击/故障**：完成事件持久化成功，`registry.select()` 失败。
**结果**：`ROLLBACK_COMMIT_APPLY_FAILED`、`audit_committed=True`、`pointer_applied=False`、`discrepancy` 非空、读取器停用、活动指针失效（`test_sr_14`）。
**残余**：审计记录声称完成，但有效状态未切换；需要人工/自动化对账。
**分类**：**REQUIRED_HARDENING**（生产需 checkpoint + 对账任务；当前已 fail closed 且如实报告）。

### 2.5 `fsync` 不确定性

**故障**：字节已写入但 `fsync` 抛错。
**结果**：回读可见事件 -> `UNCERTAIN`，不声称“确定未持久化”；读取器停用（`RC-09`）。
**残余**：事件存在但指针未切换，形成可检测的差异。
**分类**：**ACCEPTABLE_WITH_CONTROLS**（原型）/ 生产需事务化提交或对账。

### 2.6 并发与锁

| 子场景 | 现状 | 分类 |
| :--- | :--- | :--- |
| 并发追加 | 跨进程 + 跨线程锁，有界等待，追加前全历史校验 | 已测试（`SG-35`、`test_concurrency.py`） |
| `verify()` 与追加并发 | `verify()` 不加锁，可能观察到撕裂尾并 fail closed | **REQUIRED_HARDENING**（生产读取需快照或锁） |
| Windows 间歇性锁失败 | 既有问题单独跟踪；本次未复现根因 | **ACCEPTABLE_WITH_CONTROLS**（原型）/ 生产需修复 |

### 2.7 保留与备份

- 审计文件必须位于仓库外显式工作区；无默认位置。
- 无保留期限、无备份、无删除策略；删除/搬移由外部运维负责。
- 生产需要：独立存储、最小权限、备份、保留期、访问审计。

---

## 3. Phase 2-G-C-C 已知限制分类

| # | 已知限制 | 分类 | 理由 |
| :--- | :--- | :--- | :--- |
| 1 | 基础模式无外部信任锚 | **PRODUCTION_BLOCKER**（基础）/ **ISOLATED_PROTOTYPE_ONLY**（严格检查点） | 基础链不可检测重写/截断；严格模式依赖外部签名检查点 |
| 2 | 调用方控制审计适配器 | **PRODUCTION_BLOCKER** | 适配器可同时伪造回读与收据；严格检查点缓解但不能替代独立服务 |
| 3 | 事件已提交但指针未应用 | **REQUIRED_HARDENING** | 已检测/停用/报告差异；恢复证据可识别分歧但需对账 |
| 4 | 默认模式恢复未被 `re_enable()` 强制 | **PRODUCTION_BLOCKER**（默认）/ **ISOLATED_PROTOTYPE_ONLY**（严格模式） | 严格模式要求恢复证据；生产尚未强制启用 |
| 5 | `verify()` 在并发写入下无锁 | **REQUIRED_HARDENING** | 读取窗口可能误判撕裂 |
| 6 | Windows 间歇性账本锁失败 | **ACCEPTABLE_WITH_CONTROLS**（原型）/ **REQUIRED_HARDENING**（生产） | 有界重试；需根因修复 |

---

## 4. 恢复授权边界（§9 问题回答）

| 问题 | 现状 | 结论 |
| :--- | :--- | :--- |
| 恢复是否强制？ | 默认否；严格模式（`require_recovery_evidence_for_reenable=True`）要求签名恢复证据，并绑定当前审计存储/链尖/条目数/完成事件/选中快照 | **严格模式已实现 / 默认未强制**（B-05） |
| 操作者能否绕过恢复？ | 默认能；严格模式在缺少/无效/有分歧/状态不匹配的恢复证据时拒绝（`test_re_*`） | **严格模式已阻断 / 默认未阻断** |
| 恢复是否验证正确存储？ | `recover()` 作用于调用方提供的 store；无生产存储身份绑定 | 条件式 |
| 最后一个已提交链尖是否被认证？ | 严格模式：由外部签名检查点认证链尖/长度；默认模式：无 | **严格模式已实现 / 默认缺失** |
| 恢复能否识别事件/指针差异？ | `build_recovery_evidence` 输出 `NONE` / `EVENT_WITHOUT_POINTER` / `POINTER_WITHOUT_EVENT`；`verify_recovery_evidence` 仅在 `NONE` 时通过 | **已实现（隔离）** |
| 能否重放更早的激活？ | 读取器要求当前绑定授权可消费；旧授权若未过期可被重放 | 条件式（依赖撤销/过期） |
| 读取器能否消费陈旧快照？ | 否；必须显式选择 + 基线/数据集匹配 | 已实现 |
| 需要什么显式授权？ | 已验证激活证据 + 显式选择；严格模式额外要求已验证恢复证据 | 已实现（严格模式） |
| 恢复自身失败会怎样？ | `verify()` 抛错 -> fail closed，不激活 | 已实现 |

### 4.1 恢复状态绑定契约（Phase 2-I-C-C）

恢复证据 schema `1.1.0` 现在必须绑定以下状态；严格读取器会用**独立读取到的当前状态**逐项比较：

| 事实 | 来源 |
| :--- | :--- |
| `dataset_identity` | 当前读取上下文 |
| `store_identity` | 读者绑定的只读审计存储 |
| `chain_tip` / `entry_count` | 读者重新读取并 `verify()` 的完整链 |
| `completion_event_id` | 当前链最后一条记录（仅当为 `SHADOW_ROLLBACK_COMPLETED`） |
| `selected_snapshot_id` / `effective_snapshot_id` | 当前显式选中的快照 |
| `recovery_generation` | 上述状态的确定性指纹 |
| `divergence` | 必须为 `NONE` |

规则：

1. 空审计历史 -> `RE_ENABLE_RECOVERY_EMPTY_HISTORY`，绝不证明已恢复选中激活。
2. 读者未绑定审计存储 -> `RE_ENABLE_RECOVERY_STATE_SOURCE_MISSING`；调用方断言不能替代实际证据。
3. 链尖/条目数/完成事件/快照任一变化 -> `RE_ENABLE_RECOVERY_STATE_MISMATCH`（RE-04/RE-14）。
4. 跨存储重用 -> 同上（RE-01）。
5. 快照选择变化 -> 同上（RE-02/RE-18）。
6. 读取/校验失败 -> `RE_ENABLE_RECOVERY_STATE_INVALID`，读取器保持停用。
7. 失败绝不切换快照指针，也不恢复旧句柄（RE-17/RE-19）。
8. 旧 schema 工件缺少强制绑定 -> 严格模式 fail closed。

**残余**：审计存储仍是读者构造时传入的隔离对象；生产必须由受信任的只读状态源提供，且不得由请求调用方替换。

### 4.2 未来恢复证据契约（建议）

恢复不得自动激活。建议最小契约：

```text
RecoveryEvidence = {
  store_identity,                # 与生产审计存储身份绑定
  verified_chain_tip,            # 经外部签名/检查点认证的链尖
  verified_ledger_length,
  completion_event_present,      # 是否存在完成事件
  effective_pointer_state,       # 当前有效指针（若可读）
  divergence: NONE | EVENT_WITHOUT_POINTER | POINTER_WITHOUT_EVENT,
  recovered_at, recovery_actor,
  recovery_fingerprint
}
```

规则：

1. 只有 `divergence=NONE` 才能进入再启用授权流程。
2. `EVENT_WITHOUT_POINTER`：必须先把有效指针恢复到审计记录状态，或追加补偿事件。
3. `POINTER_WITHOUT_EVENT`：必须追加完成事件或回退指针。
4. 恢复证据必须由独立可信组件签发，或至少与调用方断言明确分离。
5. `re_enable()` 应要求 `RecoveryEvidence`（或等价的 `recovery_verified=True`），否则拒绝。

---

## 5. 评审结论

- 影子审计层在“调用方诚实 + 隔离工作区”的前提下实现了严格的 fail-closed 语义。
- 作为生产审计基础设施，它缺少真实性（签名/检查点）、适配器信任、恢复强制与并发读一致性。
- 因此：审计层 **不满足生产激活条件**，但与生产激活隔离，可作为后续实现的参考原型。
