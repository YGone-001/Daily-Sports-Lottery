# 运行期激活：生产阻断项登记与整改依赖

**基线**：`7b44e4c36b162c7ef73b07bda016397c28e7acdf`
**决策**：`PRODUCTION_ACTIVATION_NOT_READY` / **NO-GO**
**说明**：本文件只登记阻断项与整改顺序，不实现生产激活、生产账本读取器或生产审计服务。

---

## 0. 整改状态更新（B-01 ~ B-05 已按组件/契约关闭）

以下关闭表示**隔离原型中已存在可执行、可验证的信任控制**，且严格模式会强制使用；
生产标志仍为 `NOT_ENABLED`，尚未接入任何生产入口。

| 阻断项 | 状态 | 证据 |
| :--- | :--- | :--- |
| B-01 权威治理证据 | **CLOSED（组件）** | `utils/authoritative_governance.py` + 签名检查点/清单；`tests/test_runtime_activation_trust_hardening.py::test_b01_*` |
| B-02 治理/撤销/Canonical 视图 | **CLOSED（组件）** | 资格评估器校验签名视图并逐项匹配；严格读取器使用视图中的活动绑定 / Canonical 集合 / 撤销；`test_b02_*` |
| B-03 观测来源认证 | **CLOSED（组件）** | `utils/observation_authority.py`；严格读取器要求签名观测证明；`test_b03_*` |
| B-04 审计外部信任锚 | **CLOSED（检查点/截断/重写）** | `ShadowActivationAuditStore.verify_external_checkpoint`；严格回滚要求检查点；`test_b04_*`。**残余**：生产审计适配器的信任仍属部署控制，需独立审计服务 |
| B-05 恢复前置 | **CLOSED（组件）** | `utils/recovery_authority.py`；严格再启用要求 `divergence=NONE` 的签名恢复证据；`test_b05_*` |

**B-06 隔离组件更新**：已增加签名时间证据、独立 freshness reference、会话连续性、24 小时合成寿命上限，
并接入 opt-in shadow 读取/再启用；详见 [可信时间验收说明](runtime-activation-trusted-time.md)。
该实现**没有部署生产时间权威**，不能将其视为生产控制关闭。

**B-07 隔离组件更新**：已实现从已认证账本派生的逐提案批准记录、签名视图 schema 1.1.0、
资格/激活批准指纹一致性及 strict shadow 读取/恢复/回滚保护，详见
[已认证批准绑定验收说明](runtime-activation-approval-binding.md)。
状态为 **COMPONENT_IMPLEMENTED / COMPONENT_VERIFIED / ISOLATED_PROTOTYPE_ONLY / PRODUCTION_CONTROL_NOT_READY**；
不表示已部署生产批准服务。B-13 生产 supersession 权威和 B-14 生产撤销权威仍是独立待办。

**B-13 隔离组件更新**：已实现专用域签名替代事件、签名当前 inventory、独立会话检查点、
单调历史连续性、唯一 CURRENT 与 strict shadow 读取/再启用/回滚检查；详见
[显式授权替代说明](runtime-activation-supersession.md)。状态为
**COMPONENT_IMPLEMENTED / COMPONENT_VERIFIED / ISOLATED_PROTOTYPE_ONLY / PRODUCTION_CONTROL_NOT_READY**；
生产生命周期注册表、持久检查点与全局当前性未部署，不能标记生产关闭。B-14 保持单独待办。

**仍未达到生产关闭**：B-06（生产时间权威与持久连续性）、B-07（生产批准权威）、B-08（空所有权证据）、
B-09 ~ B-14。因此生产决策仍为 **NO-GO**，且本次**未**启用生产激活、生产读取器或生产审计服务。

关闭 B-01 ~ B-05 的实现均位于**仓库内隔离原型**，密钥只在测试临时目录生成；
严格模式由调用方显式开启（`require_authoritative_governance` /
`require_observation_attestation` / `require_trusted_audit_checkpoint` /
`require_recovery_evidence_for_reenable`），默认影子行为保持不变。

---

## 1. 阻断项登记

严重度：**Critical**（可导致身份/授权被伪造或历史被重写）/ **High**（可导致越权消费或恢复绕过）/ **Medium**（可用性/一致性问题）。

### 1.1 Phase 2-I-C 状态核对

| 阻断项 | 组件实现 | 隔离验证 | 生产控制 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| B-01 | `build_authoritative_governance_view` + 签名检查点/清单 | 是（TI-01~TI-15b） | 否：无生产账本位置/读取服务 | `ISOLATED_PROTOTYPE_ONLY` / `PRODUCTION_CONTROL_NOT_READY` |
| B-02 | 签名 Canonical / 撤销清单 + 严格读取器 | 是（B-02、TI-13/15） | 否：信任锚与视图仍由调用方提供 | `ISOLATED_PROTOTYPE_ONLY` / `PRODUCTION_CONTROL_NOT_READY` |
| B-03 | 签名观测来源证明（含数据集、身份契约、sport/date、唯一性作用域） | 是（B-03、TI-16b~19） | 否：无生产采集权威 | `ISOLATED_PROTOTYPE_ONLY` / `PRODUCTION_CONTROL_NOT_READY` |
| B-04 | 外部签名审计检查点 + 恢复证据 | 是（B-04、TI-05~07/21） | 否：无独立审计服务/检查点发布方 | `ISOLATED_PROTOTYPE_ONLY` / `PRODUCTION_CONTROL_NOT_READY` |
| B-05 | 严格再启用要求恢复证据 + `NONE` 差异 | 是（B-05、TI-20~22） | 否：严格模式为可选构造参数，非生产强制策略 | `ISOLATED_PROTOTYPE_ONLY` / `PRODUCTION_CONTROL_NOT_READY` |

**结论**：B-01~B-05 的组件已实现并在隔离夹具中验证，但**没有一个**达到 `PRODUCTION_CONTROL_VERIFIED`；生产激活仍为 NO-GO。

### B-01 权威治理证据不可达（Critical）

- **位置**：[runtime_mapping_policy.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/runtime_mapping_policy.py) 的 provenance 常量；`_check_governance_evidence_provenance_supported`（[runtime_mapping_eligibility.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/runtime_mapping_eligibility.py)）。
- **受影响属性**：治理事实真实性与生产授权资格。
- **现状**：`VERIFIED_GOVERNANCE_EVIDENCE` 仅在提供**已签名、未过期、事实逐项匹配**的权威视图时被接受；仅声称来源仍被阻断。隔离原型已实现，但没有生产账本位置、生产读取服务或生产锚分布。
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
- **现状**：读取器接受任何调用方构造的 `SourceObservation`；隔离原型现提供签名来源证明（绑定数据集、身份契约、sport/date、唯一性作用域），但生产采集权威不存在，且严格模式为可选参数。
- **复现**：手工构造与夹具同键的观测即可让读取器解析（静态可构造；现有夹具本身即合成观测）。
- **整改标准**：生产运行期只能消费由受控采集/解析管线签发、带来源签名或不可伪造句柄的观测；读取器必须验证该来源。
- **验收测试**：`test_forged_observation_without_provenance_is_rejected`、`test_observation_source_signature_required`。

### B-04 审计无外部信任锚；截断/重写/适配器伪造不可检测（Critical）

- **位置**：`ShadowActivationAuditStore.verify/append/store_identity/receipt_for`（[shadow_activation_audit_store.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/shadow_activation_audit_store.py)）。
- **受影响属性**：审计真实性与不可否认性。
- **现状**：隔离原型已实现 `export_checkpoint` / `verify_external_checkpoint` 与恢复证据签名；严格模式下可检测尾部截断、追加与整链替换。检查点发布方仍是调用方提供的密钥，生产独立审计服务不存在；未启用严格模式时截断仍不可检测。
- **复现**：`test_b04_checkpoint_detects_tail_truncation`（严格模式检出）、`test_sr_17`（无检查点模式仍不可检测）、`test_sr_18`（默认声明无外部锚）。
- **整改标准**：引入外部签名/可信检查点（链尖 + 长度 + 时间），并规定审计适配器为受信任、最小权限的独立组件；哈希链只作为内部一致性校验。
- **验收测试**：`test_tail_truncation_is_detected_by_checkpoint`、`test_full_chain_rewrite_is_detected`、`test_audit_adapter_identity_is_authenticated`。

### B-05 恢复不是再启用的前置条件（High / 恢复路径 PRODUCTION_BLOCKER）

- **位置**：`ShadowRuntimeReader.re_enable`（[shadow_runtime_reader.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/shadow_runtime_reader.py)）；`ShadowActivationSimulator._fail_closed_after_audit_failure`。
- **受影响属性**：不确定提交后的恢复完整性。
- **现状**：严格模式（`require_recovery_evidence_for_reenable=True`）已要求签名恢复证据，并绑定当前审计存储身份、链尖、条目数、完成事件、选中快照与 `recovery_generation`；空历史、跨存储、指针不匹配、链推进后的旧证据均拒绝。但该模式是可选构造参数，默认关闭，未构成生产强制策略。
- **复现**：`test_b05_strict_reenable_requires_recovery_evidence`、`test_re_01` ~ `test_re_19`（严格模式拒绝）；`test_sr_19`（默认模式未强制恢复）。
- **整改标准**：`re_enable()` 必须要求已验证的恢复证据，并识别事件/指针差异；差异未消解前拒绝再启用。
- **验收测试**：`test_reenable_requires_recovery_evidence`、`test_event_without_pointer_blocks_reenable`。

### B-06 生产可信时间权威与最大授权寿命（High；隔离组件已实现）

- **位置**：`ActivationVerificationContext.current_time`；`_collect_rejections`；`_assert_body_is_signable`（[runtime_activation_authorization.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/runtime_activation_authorization.py)）。
- **受影响属性**：过期/生效边界、重放窗口。
- **现状**：legacy 保留调用方时间。opt-in strict 模式要求 Ed25519 时间证明、单独配置的受信任 freshness reference、会话 challenge 与高水位，并强制版本化合成策略 `0 < lifetime <= 24h`。时间不可信、过期、回拨、旧 generation、丢失会话状态均拒绝。没有生产时间服务或持久高水位。
- **整改标准**：部署独立认证的生产时间源与持久连续性，审批生产最大寿命策略并独立复评。24 小时仅为当前合成策略；更短风险分层须有单独批准的分类体系。
- **验收测试**：`test_clock_rollback_fails_closed`、`test_max_authorization_lifetime_enforced`、`test_expiry_boundary_uses_trusted_time`。

### B-07 激活授权与已认证批准绑定（High；隔离组件已实现）

- **位置**：`MAY_BE_EMPTY_FIELDS` 与 `_collect_rejections`；`build_activation_authorization_body`。
- **受影响属性**：批准与激活的证据链完整性。
- **现状**：legacy 工件仍可隔离检视。新的 opt-in strict 模式要求 activation、decision 与当前签名治理视图中已认证批准记录的指纹完全一致且非空；批准记录派生前必须验证检查点、owner Ed25519 与历史状态。新模式保持可信时间、寿命、撤销、Canonical 基线和恢复证据检查。
- **整改标准**：隔离组件已实现并验证；生产 owner/view 服务、信任锚分发与全局当前性仍需部署和独立评审，不能标记生产关闭。
- **验收测试**：`tests/test_runtime_activation_approval_binding.py`，AB-01～AB-34 与附加对抗测试；`tools/shadow_approval_binding_validation.py`。

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
- **现状**：隔离原型已实现外部签名检查点与恢复证据，严格模式下可检测尾部截断/追加/重写与事件-指针差异；但检查点发布方仍是调用方提供的密钥，生产独立审计服务不存在。未启用严格模式时仍无法检测截断。
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
- **现状**：legacy 检视仍保留旧行为。隔离 strict 模式通过独立生命周期签名 A→B、签名 CURRENT 状态与独立检查点确定唯一有效授权；历史 A 保留并独立验签，其过期/撤销不再阻断有效 B。双 CURRENT、环、分叉、缺失/伪造证据、旧状态及旧快照恢复一律拒绝。
- **组件状态**：**COMPONENT_IMPLEMENTED / COMPONENT_VERIFIED / ISOLATED_PROTOTYPE_ONLY / PRODUCTION_CONTROL_NOT_READY**。
- **生产整改标准**：部署独立生命周期权威、可信当前检查点、持久连续性及快照发布/选择控制并独立复评。不得按签发时间、approval recency、快照新旧、词法 ID 或插入顺序推断当前授权。
- **验收测试**：`tests/test_activation_supersession.py`（SS-01～SS-44 及附加对抗测试）、`tools/shadow_activation_supersession_validation.py`；实现与限制见 [显式授权替代说明](runtime-activation-supersession.md)。

### B-14 撤销权威缺失（High）

- **位置**：`revocation_registry` 由调用方传入。
- **现状**：legacy 依赖调用方登记；strict shadow 使用既有签名治理撤销事实，调用方空表不能覆盖，supersession 不能擦除已观察撤销。但没有生产独立撤销服务、全局新鲜性或密钥管理后端，无法将隔离签名视图视为生产撤销权威。
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
