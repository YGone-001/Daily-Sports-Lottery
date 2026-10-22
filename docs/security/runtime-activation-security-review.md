# 运行期激活安全评审（Phase 2-H）

**评审对象**：`7b44e4c36b162c7ef73b07bda016397c28e7acdf`（`develop`）
**性质**：只读安全评审与就绪性评估，不实现生产激活
**生产标志**：`RUNTIME_MAPPING_ACTIVATION = NOT_ENABLED`、`PRODUCTION_RUNTIME_READER = NOT_ENABLED`、`PRODUCTION_MAPPING_WRITE_AUTHORIZATION = NOT_GRANTED`

> 本文件是评审证据，不是生产授权。任何 PASS 仅表示“该评审项已完成”，不代表可上线。

---

## 1. 结论摘要

| 决策 | 结果 |
| :--- | :--- |
| 影子原型是否可接受（SHADOW PROTOTYPE） | **SHADOW_PROTOTYPE_ACCEPTED**（仅限隔离影子用途） |
| 生产激活是否就绪 | **PRODUCTION_ACTIVATION_NOT_READY / NO-GO** |

理由：影子层的选择权威、所有权冲突、Ed25519 授权复核、停用/回滚审计提交与严格审计链均已具备可执行证据；但生产激活所需的
**权威治理证据读取视图、可信观测来源、可信时间权威、外部审计信任锚、恢复前置约束、权威 Canonical 所有权视图**均不存在或仍由调用方提供。
这些缺口不能由原型测试通过来抵消。

> **整改更新**：B-01 ~ B-05 已按“隔离组件 + 显式严格模式”关闭（权威治理视图、观测来源证明、
> 外部审计检查点、恢复证据）；B-06 ~ B-14 仍未关闭，生产决策仍为 **NO-GO**。
> 详见 [阻断项登记](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/docs/security/runtime-activation-production-blockers.md)。

---

## 2. 评审范围与信任模型

### 2.1 资产

1. `data/daily_matches.json` 的 Canonical 身份与所有权。
2. 六个受保护派生存储（强度、校准、预测快照、赔率快照、结算、评估行）。
3. 映射所有者 / 激活授权人的 Ed25519 私钥（必须位于仓库外）。
4. 治理账本与影子审计链的历史完整性。
5. 生产刷新链路的可用性与确定性。

### 2.2 对手模型

| 对手 | 能力 | 主要攻击面 |
| :--- | :--- | :--- |
| 恶意/错误的调用方 | 构造任意 `SourceObservation`、`ShadowReadContext`、资格判定与治理事实 | 读取器、模拟器、资格评估 |
| 被篡改的工件 | 修改提案、批准件、授权件、快照、审计记录 | Ed25519 校验、内容指纹、哈希链 |
| 被攻陷的所有者/授权人密钥 | 签发合法但恶意的批准/授权 | 治理意图本身（密码学无法解决） |
| 本地文件系统攻击者 | 改写/截断审计与账本文件 | 哈希链、检查点 |
| 并发写入者 | 与追加/校验竞争 | 锁与回读 |
| 重放者 | 重用未过期授权或历史批准 | 有效期、撤销、基线绑定 |
| 供应链 / 依赖缺失 | 使 `cryptography` 不可用 | fail closed 行为 |

### 2.3 明确不在范围内的信任

- 影子模块的输入**一律不被视为生产权威**：调用方提供的治理事实、Canonical ID 集合、源绑定、撤销登记、受控时钟都是“断言”，不是“已独立验证的事实”。
- 哈希链只证明内部一致性，**不是**防篡改证据。
- 原型通过测试不等于生产可上线。

---

## 3. 端到端信任边界矩阵

状态定义：**VERIFIED**（有可执行证据且边界成立）/ **CONDITIONAL**（仅在隔离影子条件下成立）/ **UNVERIFIED**（无可执行或权威来源）/ **UNSAFE**（可被绕过）。

| # | 边界 | 可信输入 | 不可信输入 | 身份/结构校验 | 密码学校验 | 治理权威 | 基线绑定 | 撤销 | 过期 | 失败行为 | 已知绕过 | 生产所需证据 | 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `SourceObservation` | 合法解析器的输出（设计意图） | 任意调用方构造的冻结对象 | `ScopedSourceIdentityKey` 要求充分证据种类 | 无（语义指纹只是自洽） | 无 | 无 | 无 | 无 | 证据不足 -> 不充分键 | 可伪造同键观测 | 生产抓取路径的原始证据签名 / 可信采集边界 | **UNVERIFIED** |
| 2 | `ScopedSourceIdentityKey` | 观测中的竞彩编号或已验证 provider ID | 未验证 provider ID、行位置、歧义共享 ID | 版本 3.0.0；`is_sufficient` 单一权威 | 按内容指纹构造键 | 无 | 含 sport/date/作用域 | 无 | 无 | 不充分键阻断活动绑定 | 无（键本身不授权） | 观测来源可信 | **VERIFIED（影子）** |
| 3 | `MappingProposal` | 操作者构造 | 任意字段 | `validate_proposal` / 状态机 | 无签名（内容指纹） | 操作者角色 | `expected_baseline_fingerprint` | 无 | 无 | 结构非法拒绝 | 提案本身不可信，但需所有者批准 | 批准件签名 | **CONDITIONAL** |
| 4 | `Owner Approval` | 所有者外部私钥 | 任何未签名/错用途/错主体工件 | schema 2.0.0、用途、主体分离 | Ed25519 + 可信公钥路径 | 所有者 | 数据集 + 基线 | 状态迁移表达 | 无内在过期（由基线变化失效） | 任何不匹配 fail closed | 可信公钥路径由调用方给出 | 可信公钥分发 / 轮换 / 吊销 | **VERIFIED（密码学）** |
| 5 | `Runtime Eligibility` | 调用方提供的提案、批准、观测、上下文 | 调用方断言的记录状态/指纹/Canonical 集合 | 45 条规则、版本/证据/基线绑定 | 复核批准签名 | 无权威账本 | 绑定当前基线 | 依赖调用方 `recorded_proposal_state` 与撤销 | 无（重新评估时点） | 不满足 -> `NOT_ELIGIBLE` | 调用方可提供“已批准”状态 | 权威治理读取视图 | **CONDITIONAL** |
| 6 | 权威治理证据 | — | 自称 `VERIFIED_GOVERNANCE_EVIDENCE` | 策略直接阻断该来源 | — | **不存在** | — | **无权威撤销源** | — | `EVIDENCE_PROVENANCE_UNSUPPORTED` | 不适用（不可达） | 见 §4 | **UNVERIFIED** |
| 7 | `Activation Authorization` | 授权人外部私钥 | 任意工件/重放 | 字段/用途/范围/schema | Ed25519 + 可信公钥路径 | 授权人（与所有者分离） | 数据集 + 基线 | `revocation_registry`（调用方） | `now >= expires_at` | 任一不满足不可消费 | 无最大寿命；`approval_fingerprint` 可为空 | 可信时间 + 撤销权威 + 最长寿命 | **CONDITIONAL** |
| 8 | `Shadow Activation Snapshot` | 构建器 | 手工构造/反序列化 | ID==内容指纹、绑定去重/冲突 | 无签名 | 无 | 快照基线 | 无 | 状态字段 | 冲突/不可复算拒绝 | 注册不授权，选择才授权 | 持久化发布与审计 | **VERIFIED（影子）** |
| 9 | `Shadow Runtime Reader` | 显式选择的已登记快照 | 任意快照对象/ID/观测/上下文 | 注册 + 选择 + 指纹 + 身份键 | 读时复核 Ed25519 | 无 | 当前基线 | 读时撤销登记 | 读时过期 | 拒绝且不降级 | 所有权证据由调用方提供 | 权威 Canonical 视图 | **CONDITIONAL** |
| 10 | `Shadow Resolution Result` | 只读结果对象 | — | 三态结果 + 拒绝码 | — | 无 | 记录检查时点 | — | — | 绝不主张生产授权 | — | 不得作为生产权威 | **UNVERIFIED（生产）** |

**结论**：影子链路在“隔离 + 调用方诚实提供治理事实”的前提下是自洽且 fail closed 的；生产链路缺少第 1、5、6、7、9 边界所需的权威来源。

---

## 4. 治理证据真实性（为什么 `VERIFIED_GOVERNANCE_EVIDENCE` 不可达）

### 4.1 现状

| 事实 | 位置 |
| :--- | :--- |
| 允许的来源只有 `CALLER_ASSERTED_CONTEXT` 与 `ISOLATED_SNAPSHOT_CONTEXT` | [runtime_mapping_policy.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/runtime_mapping_policy.py) |
| `VERIFIED_GOVERNANCE_EVIDENCE` 被显式列入阻断集合 | 同上 |
| 资格评估器对声称该来源的上下文直接拒绝 | `_check_governance_evidence_provenance_supported`，[runtime_mapping_eligibility.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/runtime_mapping_eligibility.py) |
| 隔离快照可以执行结构校验与历史批准审计，但来源仍是 `ISOLATED_SNAPSHOT_CONTEXT` | `build_isolated_governance_snapshot`，[mapping_governance_shadow.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/tools/mapping_governance_shadow.py) |
| 账本路径必须在仓库外且无默认位置 | `_assert_isolated_ledger_path`，[mapping_governance.py](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/utils/mapping_governance.py) |

### 4.2 缺失组件（最小清单）

1. **权威账本位置与只读访问器**：生产治理账本的固定根、只读打开、版本/所有权/权限校验。
2. **当前状态派生权威**：从完整事件历史派生 `proposal state`、最新内容指纹、活动绑定与撤销状态，而非调用方断言。
3. **历史批准审计强制化**：每次构建证据视图都必须执行结构校验 + 可信公钥批准审计；缺证据即拒绝。
4. **撤销权威**：撤销记录必须有独立来源与查询接口；调用方 `revocation_registry` 不能作为生产权威。
5. **基线/数据集权威**：当前 Canonical 基线必须由受控管线计算，而不是调用方传入。
6. **完整性锚**：账本头部/链尖检查点或外部签名，用于检测整链重写与尾部截断。
7. **新鲜度**：证据视图的生成时刻、账本长度、链尖必须可验证且足够新。
8. **超集/替代映射检测**：同一源键的新提案必须能判定旧提案已被 supersede。
9. **只读性与隔离**：读取视图不得暴露写入、修复、迁移入口。

### 4.3 最小“可信治理读取视图”契约（未来）

输入：`(ledger_root, trusted_public_key, dataset_identity, current_baseline, not_before)`。
输出：`(proposal_state, proposal_content_fingerprint, active_bindings, revocations, chain_tip, ledger_length, verified_at, verification_category)`。
失败：任何结构/签名/证据缺失/新鲜度不足都 fail closed，不产生 `VERIFIED_GOVERNANCE_EVIDENCE`。

---

## 5. 身份安全评审

### 5.1 已验证

- 竞彩编号键 `sport|date|jczq|number` 与既有 `same_event()` 语义一致，不含开赛时间。
- `RAW_PROVIDER_ID` 只有在 `VERIFIED_SCOPED` 时才是充分证据；`UNVERIFIED_PROVIDER_ID`、`AMBIGUOUS_SHARED_SOURCE_ID`、`INSUFFICIENT` 一律阻断活动绑定。
- 键值显式携带 adapter / namespace / sport / date / 类型 / 唯一性作用域 / 原始值，跨运动、跨日期不再碰撞。
- 旧契约键（1.0.0/2.0.0）不被静默重新解释。
- `same_event()`、摄取规划器与既有 Canonical ID 未被修改（全量回归与静态扫描）。

### 5.2 残余风险

| 风险 | 说明 | 处置 |
| :--- | :--- | :--- |
| 观测可伪造 | 读取器只接受 `SourceObservation`，但该对象可由任何调用方构造；语义指纹不能证明来源 | 生产必须从受控采集路径获取观测，并对原始证据做签名/绑定（B-03） |
| 所有权证据可省略 | `known_canonical_ids` 为空时跳过“未知所有者”检查；`known_source_bindings` 为空时跳过冲突检查 | 生产必须从权威 Canonical 视图注入且禁止为空（B-08） |
| 多观测提案 | 构建器支持多观测，但读取器按单一源键解析；不构成越权 | 记录为信息项 |

---

## 6. 激活授权验证矩阵

| # | 绑定项 | 实现位置 | 结果 | 若缺失/错误的攻击 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | Ed25519 算法 | `signature_algorithm` + `_crypto_backend` | VERIFIED | 伪造签名 |
| 2 | 可信密钥身份 | `trusted_public_key_path` + `public_key_id` | VERIFIED（路径由调用方给出） | 自签公钥替换信任锚 |
| 3 | 用途域分离 | `PURPOSE_MISMATCH` | VERIFIED | 批准件/修复授权重放为激活 |
| 4 | 提案 ID | `PROPOSAL_ID_MISMATCH` | VERIFIED | 授权错误提案 |
| 5 | 提案内容指纹 | `PROPOSAL_FINGERPRINT_MISMATCH` | VERIFIED | 偷换提案内容 |
| 6 | 资格判定 ID/指纹/版本 | `ELIGIBILITY_*` | VERIFIED | 篡改资格结论 |
| 7 | 作用域源身份键 | `SCOPED_SOURCE_IDENTITY_KEY_MISMATCH` | VERIFIED | 授权其他源 |
| 8 | Canonical 目标 | `candidate_canonical_id` 绑定 | VERIFIED | 改绑其他 Canonical |
| 9 | 数据集身份 | `DATASET_IDENTITY_MISMATCH` | VERIFIED | 跨数据集重放 |
| 10 | Canonical 基线 | `BASELINE_STALE` | VERIFIED | 旧基线授权继续生效 |
| 11 | 策略/评估器版本 | `POLICY_VERSION_MISMATCH` / `ELIGIBILITY_EVALUATOR_VERSION_MISMATCH` | VERIFIED | 旧规则结果复用 |
| 12 | 激活范围 | 允许列表 + 写入语义黑名单 | VERIFIED | 授权写操作 |
| 13 | 时间窗口 | `NOT_YET_VALID` | VERIFIED | 未来生效授权 |
| 14 | 过期 | `now >= expires_at` | VERIFIED（时间由调用方提供） | 过期授权复用 |
| 15 | 撤销 | `revocation_registry` | CONDITIONAL（调用方提供） | 已撤销授权复用 |
| 16 | 重放抵抗 | 全字段签名 + 内容派生 `activation_id` | CONDITIONAL（无 nonce；有效期内可整体重放） | 授权重放 |
| 17 | 授权替代 | **无实现** | UNVERIFIED | 新旧授权并存 |
| 18 | 同一身份多授权 | 读取器要求全部匹配绑定可消费 | CONDITIONAL（旧的过期/撤销会阻断新的） | 可用性/陈旧授权 |
| 19 | `activation_id` 唯一性 | 内容派生，无全局唯一登记 | CONDITIONAL | 重复工件 |
| 20 | 快照成员一致性 | 绑定必须出现在 `activation_ids` | VERIFIED | 快照外授权消费 |

**所有者批准 vs 激活授权**：用途域、工件 schema、签发主体与校验路径均分离；批准件不能替代激活授权，激活授权也不能替代批准（`AA-16` / `AA-17`）。但激活授权未强制绑定 `approval_fingerprint`（可为空），列为 B-07。

---

## 7. 时间权威与过期策略

| 检查项 | 现状 | 结论 |
| :--- | :--- | :--- |
| 生效边界 | `now < authorized_at` -> `NOT_YET_VALID` | 已实现 |
| 到期边界 | `now >= expires_at` -> 过期（到期瞬间即失效） | 已实现 |
| 时区 | `fromisoformat`，无时区按 UTC；`Z` 归一化 | 已实现（文档化） |
| 时钟回拨/跳跃 | 无单调时钟、无回拨检测 | **缺失** |
| 主机失同步 | 无 NTP/权威时间校验 | **缺失** |
| 非法时间戳 | 拒绝 | 已实现 |
| 缺失受控时间 | `CLOCK_UNAVAILABLE` / `RE_ENABLE_CLOCK_UNAVAILABLE` | 已实现 |
| 最大授权寿命 | **无**（仅要求 `expires_at > authorized_at`） | **缺失** |
| 续期/替代 | 设计为重新签发；无强制 supersession | **缺失** |
| 撤销 vs 过期 | 两类拒绝码分离 | 已实现（撤销来源不可信） |

**建议（非现有策略）**：

- 生产必须使用受控时间源：NTP 同步 + 单调时钟增量 + 最大允许偏差（例如 ≤ 60s），并在每次读取/授权校验时记录时间证据。
- 建议最大授权寿命：普通映射 **≤ 24 小时**、高风险映射 **≤ 1 小时**；需要续期时重新签发新 `activation_id`，旧件立即 supersede。该数值是建议，不是已生效政策。
- 时钟回拨、跳跃、失同步任一发生 -> 立即 fail closed 并停用消费，直到重新建立可信时间。

---

## 8. 生产集成攻击面

| 入口 | 是否可触达影子/激活 | 证据 |
| :--- | :--- | :--- |
| `utils/scraper.refresh()` | 否 | 仅导入摄取/生命周期/存储模块；无治理/影子导入（`SG-40`、`RC-14`） |
| Flask `app.py` 路由 | 否 | 仅 matches/strategy/history/refresh/status；无 activation/mapping 路由（`SR-20`） |
| React 前端 | 否 | `frontend/src/api/*` 仅调用既有 `/api/*`（`SR-20`） |
| 启动脚本 | 否 | `start.bat` / `start.sh` 不引用影子模块（`SR-20`） |
| 后台调度 | 无独立调度器 | 只有请求触发的 refresh |
| CLI 工具 | 是（显式、隔离） | `tools/shadow_runtime_activation.py`、`tools/mapping_governance_shadow.py`，均要求显式仓库外工作区 |
| 生产修复工具 | 否 | `tools/production_canonical_repair.py` 不导入影子/治理原型（`SG-49`） |

结论：当前不存在“应用启动即实例化影子读取器 / 自动选择快照 / 自动启用映射”的可执行路径。任何未来生产 API 必须补齐：

1. 强认证与角色分离（操作者 / 所有者 / 授权人 / 紧急停用者）。
2. 变更审批与双人控制（至少高风险映射）。
3. 请求级审计与幂等键。
4. 只读/写入路径隔离，禁止从 Web 层直接选择快照。
5. CSRF/速率限制/输入验证（现有 Flask 无认证层）。

---

## 9. 对抗场景 SR-01 ~ SR-22

| 场景 | 方法（可执行测试） | 实际结果 | 不变量 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| SR-01 缺少治理权威 | `test_ec_01/05`、资格规则 | 缺记录事实 -> `NOT_ELIGIBLE`；条件式判定绝无生产授权 | 缺权威不得升级为授权 | PASS（影子） |
| SR-02 伪造 provenance | `test_ec_12` + `test_sr_02` | `VERIFIED_GOVERNANCE_EVIDENCE` -> `EVIDENCE_PROVENANCE_UNSUPPORTED` | 自述不是证据 | PASS |
| SR-03 无效批准签名 | `test_rr_06`、`test_aa_02` | 拒绝 | 签名必须可验证 | PASS |
| SR-04 错误用途/重放 | `test_aa_04`、`test_aa_16` | `PURPOSE_MISMATCH` | 域分离 | PASS |
| SR-05 篡改资格判定 | `test_aa_05` | 指纹/ID 不匹配拒绝 | 判定不可篡改 | PASS |
| SR-06 未选择快照 | `AC-01`、`GC-01` | `SNAPSHOT_NOT_SELECTED` | 登记 != 授权 | PASS |
| SR-07 未授权再启用 | `AC-04`、`GC-09` | `TypeError` / 保持停用 | 停用边界不可绕过 | PASS |
| SR-08 已撤销 | `SG-09`、`GC-12` | `ACTIVATION_AUTHORIZATION_REVOKED` | 撤销即时生效 | PASS（撤销源条件式） |
| SR-09 已过期 | `SG-08`、`GC-11` | `ACTIVATION_AUTHORIZATION_EXPIRED` | 到期不可消费 | PASS |
| SR-10 陈旧基线 | `SG-06`、`GC-13` | `BASELINE_STALE` | 基线绑定 | PASS |
| SR-11 多个冲突所有者 | `AC-03`、`GC-05/07` | `OWNERSHIP_CONFLICT`（两种顺序） | 顺序无关 | PASS |
| SR-12 缺少审计存储 | `RC-01` | `ROLLBACK_AUDIT_STORE_MISSING`、不报告完成 | 无持久提交不完成 | PASS |
| SR-13 伪造收据 | `RC-04` | `ROLLBACK_AUDIT_COMMIT_UNVERIFIED`、fail closed | 收据必须可回读 | PASS |
| SR-14 事件已提交但指针未应用 | `test_sr_14` | `ROLLBACK_COMMIT_APPLY_FAILED`、差异报告、停用 | 不得静默不一致 | PASS（已检测） |
| SR-15 不确定 fsync | `RC-09` | `UNCERTAIN`、停用、字节保留 | 不谎报确定结果 | PASS |
| SR-16 篡改哈希链 | `GC-17/18` | `MODIFIED_ENTRY` / `BROKEN_PREVIOUS_HASH` | 篡改拒绝 | PASS |
| SR-17 有效链尾截断 | `test_sr_17` / `test_b04_*` | 基础 `verify()` 仍无法发现；**严格模式外部检查点可检出** | 截断应被检出 | PASS（严格模式）/ 基础链仅内部一致 |
| SR-18 缺少可信检查点 | `test_sr_18` / `test_b04_*` | 基础存储如实声明无检查点；新增外部签名检查点契约 | 不得声称防篡改 | PASS（严格模式） |
| SR-19 恢复不需要当前授权 | `RC-13`、`test_sr_19` | `recover()` 不激活；`re_enable()` 仍需证据；但恢复非强制 | 恢复不得自动激活 | PASS（附带 B-05） |
| SR-20 生产导入/激活隔离 | `SR-20`、`SG-41`、`RC-14` | 无生产导入/路由 | 无生产触达 | PASS |
| SR-21 受保护路径逃逸 | `SG-37`、`test_sr_21` | 仓库内账本/审计、`../` 逃逸拒绝 | 隔离工作区 | PASS |
| SR-22 七存储字节完整性 | `RC-15`、`SG-46`、§14 | 测试前后 SHA-256 一致 | 不改历史数据 | PASS |

---

## 10. 验收门 A–T

| 门 | 结果 | 说明 |
| :--- | :--- | :--- |
| A 基线完整性 | PASS | HEAD/`origin/develop` = `7b44e4c`，工作区干净，main 未变 |
| B 源码与依赖审计 | PASS | 14 个指定文件、测试、CI、前端 API 已审计 |
| C 身份信任边界 | PASS（评审完成；含 UNVERIFIED 项） | §3、§5 |
| D 治理证据真实性评审 | PASS（评审完成；结论为缺失） | §4 |
| E 授权用途分离 | PASS | §6、`AA-16/17` |
| F 签名与撤销评审 | PASS（含 B-07/B-14） | §6 |
| G 可信时间评估 | PASS（评审完成；无时间权威） | §7 |
| H 审计信任锚评估 | PASS（评审完成；无外部锚） | §11 |
| I 回滚提交安全评审 | PASS | SR-12~15 |
| J 恢复授权评审 | PASS（含 B-05） | SR-19 |
| K Canonical 所有权安全 | PASS（影子）/ CONDITIONAL（生产） | §5、SR-11 |
| L 生产集成隔离 | PASS | §8、SR-20 |
| M 对抗证据矩阵 | PASS | §9 |
| N 生产阻断项登记 | PASS | 见 blockers 文档 |
| O 明确 GO/NO-GO | PASS（NO-GO） | §1 |
| P 七存储完整性 | PASS | §14 |
| Q 全量本地回归 | PASS | 见最终报告 |
| R 远程 CI | PASS | 四 job success（最终报告） |
| S Git 完整性 | PASS | 前向单行提交 |
| T 无生产激活 | PASS | 三标志保持关闭 |

> 门的 PASS 表示“评审已完成且结论有证据”，不表示生产就绪。

---

## 11. 审计与恢复评审

见 [runtime-activation-audit-recovery-risk.md](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/docs/security/runtime-activation-audit-recovery-risk.md)。

---

## 12. 生产阻断项与整改顺序

见 [runtime-activation-production-blockers.md](file:///c:/Users/YGone/Desktop/program/Daily-Sports-Lottery/docs/security/runtime-activation-production-blockers.md)。

---

## 13. 未验证假设

1. 生产环境不存在独立审计服务或外部检查点（仓库内无实现）。
2. 生产治理账本尚不存在；本评审只能审计隔离账本工具。
3. 生产时间源未配置；所有时间结论仅适用于传入的 `current_time`。
4. 生产 Canonical 视图未以服务形式存在；所有权证据仍由调用方组装。
5. 故障注入使用包装器模拟 I/O 失败顺序，未在真实断电/内核缓存条件下验证。
6. Windows 间歇性治理锁问题未在本次评审中复现根因，仍按既有记录单独跟踪。
