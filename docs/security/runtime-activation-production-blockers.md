# 运行期激活：生产阻断项登记与整改依赖

**基线**：`7b44e4c36b162c7ef73b07bda016397c28e7acdf`
**决策**：`PRODUCTION_ACTIVATION_NOT_READY` / **NO-GO**
**说明**：本文件只登记阻断项与整改顺序，不实现生产激活、生产账本读取器或生产审计服务。

---

## 1. 阻断项登记

严重度：**Critical**（可导致身份/授权被伪造或历史被重写）/ **High**（可导致越权消费或恢复绕过）/ **Medium**（可用性/一致性问题）。

### B-01 权威治理证据不可达（Critical）

- **位置**：[runtime_mapping_policy.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/runtime_mapping_policy.py) 的 provenance 常量；`_check_governance_evidence_provenance_supported`（[runtime_mapping_eligibility.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/runtime_mapping_eligibility.py)）。
- **受影响属性**：治理事实真实性与生产授权资格。
- **现状**：`VERIFIED_GOVERNANCE_EVIDENCE` 被策略阻断；仅允许调用方断言与隔离快照。
- **复现**：`tests/test_runtime_mapping_readiness.py::test_ec_12...`（声称该来源 -> `EVIDENCE_PROVENANCE_UNSUPPORTED`）。
- **整改标准**：存在生产治理读取视图，能独立执行结构校验 + 可信公钥批准审计 + 当前状态派生，并在策略中启用该来源（且仅在该视图可用时）。
- **验收测试**：新增 `test_production_governance_view_*`：缺失账本/签名失败/证据缺失/状态陈旧 -> NOT_ELIGIBLE；完整证据 -> VERIFIED（仍需独立评审）。

### B-02 治理事实、撤销登记、Canonical 视图由调用方提供（Critical）

- **位置**：`RuntimeMappingContext`（`recorded_proposal_id/fingerprint/state`、`canonical_ids`、`revocation_registry`）与 `ShadowReadContext`（`known_canonical_ids`、`known_source_bindings`、`revocation_registry`）。
- **受影响属性**：权威性、撤销即时性、所有权冲突检测。
- **复现**：`SG-11` / `GC-05/07` 证明“当调用方提供冲突数据时会拒绝”，但空集合会跳过检查；`test_runtime_activation_security_review.py::test_sr_02`。
- **整改标准**：所有治理事实必须来自只读权威视图；空/缺失必须拒绝而非跳过；撤销必须由独立权威源查询。
- **验收测试**：`test_missing_governance_view_fails_closed`、`test_empty_ownership_evidence_fails_closed`。

### B-03 源观测来源不可认证（Critical）

- **位置**：`ShadowRuntimeReader.read` 第 4 步；`ScopedSourceIdentityKey.for_observation`（[source_identity.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/source_identity.py)）。
- **受影响属性**：身份真实性。
- **现状**：读取器接受任何调用方构造的 `SourceObservation`；语义指纹只证明字段自洽，不证明来自真实采集。
- **复现**：手工构造与夹具同键的观测即可让读取器解析（静态可构造；现有夹具本身即合成观测）。
- **整改标准**：生产运行期只能消费由受控采集/解析管线签发、带来源签名或不可伪造句柄的观测；读取器必须验证该来源。
- **验收测试**：`test_forged_observation_without_provenance_is_rejected`、`test_observation_source_signature_required`。

### B-04 审计无外部信任锚；截断/重写/适配器伪造不可检测（Critical）

- **位置**：`ShadowActivationAuditStore.verify/append/store_identity/receipt_for`（[shadow_activation_audit_store.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/shadow_activation_audit_store.py)）。
- **受影响属性**：审计真实性与不可否认性。
- **复现**：`test_sr_17`（有效链尾截断后 `verify()` 仍 OK）、`test_sr_18`（无检查点）、全链重写为静态可构造攻击。
- **整改标准**：引入外部签名/可信检查点（链尖 + 长度 + 时间），并规定审计适配器为受信任、最小权限的独立组件；哈希链只作为内部一致性校验。
- **验收测试**：`test_tail_truncation_is_detected_by_checkpoint`、`test_full_chain_rewrite_is_detected`、`test_audit_adapter_identity_is_authenticated`。

### B-05 恢复不是再启用的前置条件（High / 恢复路径 PRODUCTION_BLOCKER）

- **位置**：`ShadowRuntimeReader.re_enable`（[shadow_runtime_reader.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/shadow_runtime_reader.py)）；`ShadowActivationSimulator._fail_closed_after_audit_failure`。
- **受影响属性**：不确定提交后的恢复完整性。
- **复现**：`test_sr_19`（UNCERTAIN 停用后，未执行 `recover()` 即可显式选择 + 证据再启用）。
- **整改标准**：`re_enable()` 必须要求已验证的恢复证据，并识别事件/指针差异；差异未消解前拒绝再启用。
- **验收测试**：`test_reenable_requires_recovery_evidence`、`test_event_without_pointer_blocks_reenable`。

### B-06 无可信时间权威，无最大授权寿命（High）

- **位置**：`ActivationVerificationContext.current_time`；`_collect_rejections`；`_assert_body_is_signable`（[runtime_activation_authorization.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/runtime_activation_authorization.py)）。
- **受影响属性**：过期/生效边界、重放窗口。
- **现状**：时间由调用方提供；无 NTP/单调时钟/偏差检查；仅要求 `expires_at > authorized_at`，无上限。
- **整改标准**：受控时间源（NTP + 单调时钟 + 最大偏差）、时钟回拨 fail closed、最大寿命策略（建议 ≤24h，高风险 ≤1h）。
- **验收测试**：`test_clock_rollback_fails_closed`、`test_max_authorization_lifetime_enforced`、`test_expiry_boundary_uses_trusted_time`。

### B-07 激活授权未绑定 `approval_fingerprint`（High）

- **位置**：`MAY_BE_EMPTY_FIELDS` 与 `_collect_rejections`；`build_activation_authorization_body`。
- **受影响属性**：批准与激活的证据链完整性。
- **现状**：`approval_fingerprint` 允许空字符串，验证时从不与资格判定/批准件比对。
- **整改标准**：签发与验证都要求非空，且必须等于 `decision.approval_fingerprint` 与批准件指纹。
- **验收测试**：`test_activation_requires_approval_fingerprint_binding`。

### B-08 所有权保护在证据为空时被跳过（High）

- **位置**：`ShadowRuntimeReader.read` 第 6 步；`_rollback_rejections`。
- **受影响属性**：Canonical 所有权完整性。
- **现状**：`known_canonical_ids` 为空 -> 不检查未知目标；`known_source_bindings` 为空 -> 视为“缺少证据”通过。
- **整改标准**：生产读取必须要求非空的权威 Canonical 视图；空集合 fail closed。
- **验收测试**：`test_empty_known_canonical_ids_fails_closed`、`test_empty_known_bindings_fails_closed`。

### B-09 快照发布/选择仅存在于内存，选择本身无授权、无审计（Medium）

- **位置**：`ShadowSnapshotRegistry.select`（[shadow_activation_simulation.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/shadow_activation_simulation.py)）。
- **受影响属性**：发布/切换的可审计性与操作授权。
- **现状**：`select()` 只改内存指针、无签名、无审计；虽然消费仍要求证据，但生产切换必须可审计且受角色控制。
- **整改标准**：持久化快照注册表 + 受控选择 API + 选择事件审计 + 角色分离。
- **验收测试**：`test_snapshot_selection_requires_role_and_is_audited`。

### B-10 事件/指针差异的残余（High）

- **位置**：回滚成功事件提交后指针应用失败路径。
- **受影响属性**：审计与有效状态一致性。
- **现状**：已检测并 fail closed（`SR-14`），但无自动对账。
- **整改标准**：恢复流程必须识别并消解 `EVENT_WITHOUT_POINTER` / `POINTER_WITHOUT_EVENT`。
- **验收测试**：`test_recovery_reconciles_event_pointer_divergence`。

### B-11 并发读一致性与 Windows 锁（Medium）

- **位置**：`verify()` 无锁；`_ledger_lock` 有界等待。
- **受影响属性**：审计读取一致性、跨平台可用性。
- **现状**：并发追加有锁（`SG-35`）；`verify()` 可能看到撕裂尾并 fail closed；Windows 间歇性锁问题未根因修复。
- **整改标准**：生产读取使用快照/共享锁；修复 Windows 锁竞争根因。
- **验收测试**：`test_concurrent_verify_and_append_never_observes_partial_state`、Windows CI 稳定通过。

### B-12 设计文档与实现状态漂移（Informational）

- **位置**：[runtime-activation-architecture.md](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/docs/runtime-activation-architecture.md) 声称授权/读取器 `NOT_IMPLEMENTED`，但隔离影子原型已存在。
- **受影响属性**：评审与运维认知。
- **整改标准**：文档明确区分“生产未实现”与“隔离影子原型已实现”；不改变生产标志。
- **验收测试**：文档一致性检查（人工评审）。

### B-13 多授权/替代语义不完整（Medium）

- **位置**：`ShadowActivationSnapshot.bindings_for` + 读取器“全部匹配绑定必须可消费”。
- **现状**：同一源键的旧授权若过期/撤销，会阻断新授权的解析；无显式 supersede 规则。
- **整改标准**：定义并实现 supersession/最新有效授权选择规则（不得按时间臆测，需显式状态）。
- **验收测试**：`test_superseded_authorization_does_not_block_current_one`、`test_revoked_old_authorization_cannot_be_reused`。

### B-14 撤销权威缺失（High）

- **位置**：`revocation_registry` 由调用方传入。
- **现状**：读取器/再启用只按调用方登记判断撤销；无法发现未登记的真实撤销。
- **整改标准**：独立撤销权威（签名撤销记录 + 当前状态视图）。
- **验收测试**：`test_unregistered_revocation_is_detected_from_authority`。

---

## 2. 整改顺序与依赖

| 顺序 | 工作项 | 依赖 | 关闭的阻断项 |
| :--- | :--- | :--- | :--- |
| 1 | 权威治理读取视图（只读、结构校验 + 批准审计 + 当前状态 + 新鲜度） | 生产账本位置与信任根 | B-01、B-02 |
| 2 | 撤销权威与 supersession 状态视图 | 1 | B-13、B-14 |
| 3 | 受控观测采集与来源认证 | 采集管线 | B-03 |
| 4 | 可信时间权威 + 最大寿命策略 | 运维时间源 | B-06 |
| 5 | 审计外部签名/检查点 + 受信任适配器 | 独立审计服务 | B-04 |
| 6 | 恢复证据契约 + 差异对账 + 再启用前置 | 5 | B-05、B-10 |
| 7 | 权威 Canonical 所有权视图（非空强制） | 1 | B-08 |
| 8 | 授权完整性（`approval_fingerprint`、唯一性、supersession） | 2、3 | B-07 |
| 9 | 持久快照注册表 + 受控选择 API + 审计 | 5、6 | B-09 |
| 10 | 并发读一致性 + Windows 锁根因 | 5 | B-11 |
| 11 | 文档状态对齐 | 全部 | B-12 |

**关键路径**：1 -> 2 -> 3 -> 4 -> 5 -> 6。任何生产激活实现不得早于 1–6 全部关闭并通过独立复评。

---

## 3. 未来生产 API 的必要控制

1. 认证：操作者/所有者/授权人/紧急停用者四类身份，最强多因素。
2. 授权：所有者批准 + 激活授权分离；高风险变更双人控制。
3. 审计：每次请求/决策/切换的不可否认记录；外部检查点。
4. 幂等：请求 ID + 状态机；禁止重放。
5. 隔离：Web 层不得直接选择快照或签发授权；只读查询与写入命令分离。
6. 时间：服务端可信时间，客户端时间不参与判定。
7. 故障：任何不确定状态 fail closed，要求恢复证据后才能再启用。
8. 回滚：受控、可审计、单步停用无需审批。

---

## 4. 显式 GO / NO-GO

```text
SHADOW_PROTOTYPE_ACCEPTED      = YES (isolated shadow use only)
PRODUCTION_ACTIVATION_READY    = NO
DECISION                       = NO-GO
```

**NO-GO 的依据**：B-01、B-02、B-03、B-04、B-05、B-06、B-07、B-08、B-14 均未关闭；
其中任何一项都足以阻止生产激活。影子测试通过不能替代这些生产控制。

**重新评审条件**：全部 Critical/High 阻断项关闭，且由独立评审复验以下证据：

1. 生产治理读取视图的可执行验证报告。
2. 外部审计检查点/签名的验证报告。
3. 可信时间与最大寿命的验证报告。
4. 观测来源认证的验证报告。
5. 恢复证据与差异对账的验证报告。
6. 权威 Canonical 所有权视图的验证报告。
