# 运行期激活：审计信任与恢复风险评估

**基线**：`7b44e4c36b162c7ef73b07bda016397c28e7acdf`
**范围**：`ShadowActivationAuditStore`、`ShadowAuditCommitReceipt`、`ShadowActivationSimulator`、`ShadowRuntimeReader.re_enable()`
**性质**：只读评审；不实现生产审计服务

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
| 外部信任 | **无**（无检查点、无签名、无独立认证） | `SR-18`、`SG-39` |

---

## 2. 威胁分析

### 2.1 全链重写

**攻击**：拥有审计文件写权限的攻击者重写全部事件并重新计算每条 `entry_hash` 与前驱绑定。
**结果**：`verify()` 通过（链内部自洽），但仍可进一步截断尾部。
**结论**：**无法检测**。哈希链是未加锁的完整性校验，不是真实性证明。
**分类**：**PRODUCTION_BLOCKER**（必须引入外部签名或可信检查点）。

### 2.2 有效链尾截断

**攻击**：删除最后 N 条完整、LF 终止的记录，保留合法链。
**结果**：`verify()` 返回 `integrity=OK`，`entry_count` 减少（`test_sr_17` 已执行验证）。
**结论**：**无法检测**。
**分类**：**PRODUCTION_BLOCKER**（链尖检查点 / 单调长度锚定）。

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
| 1 | 无外部信任锚 | **PRODUCTION_BLOCKER** | 全链重写与尾部截断不可检测 |
| 2 | 调用方控制审计适配器 | **PRODUCTION_BLOCKER** | 适配器可同时伪造回读与收据 |
| 3 | 事件已提交但指针未应用 | **REQUIRED_HARDENING** | 已检测/停用/报告差异；需对账 |
| 4 | 恢复未被 `re_enable()` 强制 | **PRODUCTION_BLOCKER** | 运维可跳过恢复直接再启用 |
| 5 | `verify()` 在并发写入下无锁 | **REQUIRED_HARDENING** | 读取窗口可能误判撕裂 |
| 6 | Windows 间歇性账本锁失败 | **ACCEPTABLE_WITH_CONTROLS**（原型）/ **REQUIRED_HARDENING**（生产） | 有界重试；需根因修复 |

---

## 4. 恢复授权边界（§9 问题回答）

| 问题 | 现状 | 结论 |
| :--- | :--- | :--- |
| 恢复是否强制？ | 否；`re_enable()` 不检查是否执行过 `recover()` | **缺失**（B-05） |
| 操作者能否绕过恢复？ | 能：显式选择 + 已验证证据即可再启用（`test_sr_19`） | **缺失** |
| 恢复是否验证正确存储？ | `recover()` 作用于调用方提供的 store；无生产存储身份绑定 | 条件式 |
| 最后一个已提交链尖是否被认证？ | 否；无签名/检查点 | **缺失** |
| 恢复能否识别事件/指针差异？ | 不能自动识别；`recover()` 只重建历史，`consumable=False` | **缺失** |
| 能否重放更早的激活？ | 读取器要求当前绑定授权可消费；旧授权若未过期可被重放 | 条件式（依赖撤销/过期） |
| 读取器能否消费陈旧快照？ | 否；必须显式选择 + 基线/数据集匹配 | 已实现 |
| 需要什么显式授权？ | 已验证激活证据 + 显式选择 | 已实现（但未绑定恢复） |
| 恢复自身失败会怎样？ | `verify()` 抛错 -> fail closed，不激活 | 已实现 |

### 4.1 未来恢复证据契约（建议）

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
