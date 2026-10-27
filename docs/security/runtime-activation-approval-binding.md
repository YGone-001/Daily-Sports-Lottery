# 已认证所有者批准与激活完整性：隔离实现

**基线**：`49eaf7291a2b20c2909e1d761c3f654977f11495`，分支 develop。
**范围**：B-07 的逐提案批准绑定、strict shadow 消费与生命周期验证。
**生产状态**：激活和读取器 NOT_ENABLED、映射写入 NOT_GRANTED、NO-GO。

## 1. 预检与基线复现

执行 fetch origin develop main 后，HEAD 与 origin/develop 均为上述基线，工作树干净、
无未发布提交，origin/main 为 `bb8bbe542449ceca3964bae42ad6f132b33b9af2`。
AGENTS.md 已读取，作者与提交者配置符合仓库要求。
前一 CI run 40469233121 的整体状态为 completed/success，四个 job 均 success。

在修改实现前执行新文件的五项 baseline 测试，**5 passed**，实际结果如下：

| 编号 | 基线实际行为 |
| :--- | :--- |
| AB-01 | 存在真实 owner 签名、APPROVED 账本及资格判定时，空 activation approval_fingerprint 仍可通过 |
| AB-02 | 激活签名有效时，任意错误 approval_fingerprint 仍可通过 |
| AB-03 | 两个相互匹配的调用方字段可以通过旧验证；没有独立批准身份来源 |
| AB-04 | 当前视图显示 SUPERSEDED 时，旧 reader 已拒绝；无当前视图输入的 time-only verifier 仍可接受旧授权 |
| AB-05 | 当前正确批准可以通过旧验证和隔离 shadow 读取 |

这些测试保留旧接口的真实行为，没有把已被原 reader 拦截的状态变更误报为新缺陷。

## 2. 已认证批准记录的来源

AuthoritativeGovernanceView 新增不可变的 AuthenticatedApprovalRecord 元组。
来源是同一份经检查点绑定的真实 GovernanceLedger 历史，步骤为：

1. 读取账本并验证链、序号、决策指纹、迁移身份和重放规则。
2. 使用独立配置的 checkpoint 公钥验证签名、数据集、账本身份、链尖、条目数与新鲜度。
3. 调用既有 audit_historical_approvals，使用明确配置的 owner 公钥复核历史批准。
4. 重算保留批准件指纹，验证原用途、Ed25519、提案、内容、数据集、源作用域、Canonical 与主体归属。
5. 按真实状态机验证历史连续性，仅选取当前 APPROVED 且唯一的批准。
6. 交叉检查记录中的源键、Canonical、内容和原基线与保留提案一致；不重解释旧身份键。
7. 若验证期间账本发生变化，拒绝混合历史的视图构造。

记录字段为 proposal_id、proposal_content_fingerprint、approval_fingerprint、source_identity_key、
candidate_canonical_id、baseline_fingerprint、dataset_identity、approved_transition_identity、
ledger_entry_sequence、actor、actor_role、owner_public_key_id。
字段来自账本和已认证工件，不接受调用方另传一个“预期指纹”替代认证。

历史认证仍保留所有历史 APPROVED 的审计证据；当前 approved_records 只收录仍活动的批准。
现有状态机不允许 SUPERSEDED/REVOKED 提案直接重新 APPROVED。替代测试采用显式终止旧提案、
创建不同内容的新提案、真实 owner 签批新提案的合法流程，没有增加新的状态迁移。

## 3. 签名视图版本与完整性

治理视图 payload schema 从 **1.0.0 升为 1.1.0**。approved_records 按 proposal_id 排序，
加入 facts_payload、view fingerprint 和签名载荷；外层 SignedTrustedArtifact 契约保持不变。
它不再只依赖全局 approval_audit 状态字符串。

严格批准验证拒绝缺失记录、重复或矛盾记录、非当前状态、字段错配、旧身份键、错误数据集、
未知 Canonical、视图篡改、未知 view 签名者与缺失信任锚。
显式 schema=1.0.0 的原视图仍可作 legacy 隔离检视，但不能满足严格批准权威。

已建立 strict verifier 固定 view 公钥身份及新鲜度策略；默认窗口 3600 秒，构造时只能选择更短窗口。
会话记录账本身份、条目数、链尖和批准状态指纹，拒绝已观察到新状态后的旧视图重放，
以及同一账本状态下相互矛盾的批准身份。丢失已建立会话状态时 fail closed。
这些都是进程内保护，不是生产持久权威或全局最新状态服务。

## 4. 资格、授权与时间的一致性

RuntimeMappingContext 的 opt-in require_authenticated_approval 要求已验证治理来源，
检查当前记录与实际 owner approval 指纹一致。CALLER_ASSERTED_CONTEXT 不能升级为独立认证事实。
既有 decision.approval_fingerprint 语义和字段保留，未创建竞争性的批准指纹字段或策略引擎。
原授权/decision schema 和默认 policy/evaluator 版本保留；新增批准证明能力由视图 1.1.0
和显式 opt-in 模式约束，历史隔离调用不被默默升级为新的批准权威。

StrictActivationVerifier 在同一有效可信时间下验证视图和授权，要求：

```text
authorization.approval_fingerprint
    == decision.approval_fingerprint
    == current_authenticated_record.approval_fingerprint
    != empty
```

同时检查提案 ID、内容指纹、源身份、Canonical、数据集、基线和当前状态；重算 decision_id，
检查观测指纹、ELIGIBLE、来源、schema/evaluator 版本。授权 Ed25519、用途、范围、
授权/资格完整指纹仍由原验证器执行。
当前撤销取自本次验证通过的签名治理视图，调用方空撤销集合不能覆盖它。

可信时间、窗口边界和最大 24 小时合成寿命策略全部保留；不使用 raw current_time、
工件 issued_at 或 fallback 系统时钟绕过时间验证。正确批准不能覆盖过期、撤销或陈旧基线。

build_approval_bound_authorization_body 验证时间与当前签名视图后，从记录派生指纹；
提供的指纹不一致（包括显式空值）即拒绝。它只构造主体，不创建 owner approval、不自动签名。
合成工具在临时目录中显式调用签名函数，生产私钥没有被访问或生成。

主要拒绝码为 ACTIVATION_APPROVAL_MISSING、ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH、
ACTIVATION_APPROVAL_NOT_AUTHENTICATED、ACTIVATION_APPROVAL_NOT_CURRENT、ACTIVATION_APPROVAL_SCOPE_MISMATCH。

## 5. Shadow 消费与恢复

require_authenticated_approval=True 的 reader 必须同时配置 registry、权威治理模式、
可信时间模式及启用批准绑定的 StrictActivationVerifier；不完整构造直接拒绝，不能降级。
每次读时验证当前视图、批准、资格、激活签名、时间、撤销、已选择快照和 Canonical 所有权。
快照绑定的资格指纹及 scope 也必须与本次授权一致。旧缓存不跳过任何校验。

同一严格路径用于 re_enable、普通 rollback 与 disabled-reader reconciliation。
恢复仍要求实际当前审计状态、签名恢复证据和检查点保护的对账；失败不启用 reader，
也不签发替代授权或改变 Canonical 所有权。
演示执行真实带审计停用、保持停用的对账、当前恢复证据生成与 guarded re-enable；旧句柄保持失效。

## 6. 密钥角色与可执行演示

```powershell
.\.venv\Scripts\python.exe -m tools.shadow_approval_binding_validation
.\.venv\Scripts\python.exe -m pytest -q tests/test_runtime_activation_approval_binding.py
.\.venv\Scripts\python.exe -m pytest -q
```

演示使用五组独立合成 keypair：owner、ledger checkpoint、governance view、activation、trusted time；
恢复另有独立 keypair。它们只存在于 TemporaryDirectory 中。
工具输出确定性 JSON，对 12 项真实验证结果逐项判断，失败返回非零；python -O 下仍执行不变量。
没有网络抓取、生产写入、生产 signing key 或自动批准。

## 7. 对抗测试矩阵

| 编号 | 可执行覆盖 |
| :--- | :--- |
| AB-01 | 空激活批准指纹拒绝，激活签名真实仍不可消费 |
| AB-02 | 错误激活批准指纹拒绝 |
| AB-03 | 匹配调用方断言不能替代认证记录 |
| AB-04 | 历史正确但非当前批准拒绝 |
| AB-05 | 当前认证批准链通过 |
| AB-06 | 重算账本哈希并签发检查点后，伪造 owner 签名仍拒绝 |
| AB-07 | 错误 owner key 拒绝 |
| AB-08 | 错误 owner purpose 拒绝 |
| AB-09 | 账本指纹与保留批准件不一致拒绝 |
| AB-10 | 缺失保留批准件拒绝 |
| AB-11 | 篡改账本哈希拒绝 |
| AB-12 | 缺失/伪造检查点、检查点/view 角色替换拒绝 |
| AB-13 | 错误数据集拒绝 |
| AB-14 | 陈旧 Canonical 基线拒绝 |
| AB-15 | 错误源身份及旧式身份键拒绝 |
| AB-16 | 错误 Canonical 候选拒绝 |
| AB-17 | 资格批准指纹不一致拒绝 |
| AB-18 | 缺失或不合格 decision 拒绝 |
| AB-19 | 正确批准不能覆盖授权到期 |
| AB-20 | 当前签名撤销仍有权威性 |
| AB-21 | 最大寿命继续执行 |
| AB-22 | 未受信任 view signer、缺失信任锚拒绝 |
| AB-23 | 篡改视图批准事实拒绝 |
| AB-24 | 旧视图 schema 不能满足 strict approval |
| AB-25 | 合法替代签批使旧授权失效，新授权可通过；旧视图重放拒绝 |
| AB-26 | legacy standalone inspection 保持非生产、非当前激活权威 |
| AB-27 | strict reader 拒绝错配批准 |
| AB-28 | strict reader 正确解析，调用方无效时间不能覆盖可信时间 |
| AB-29 | strict re-enable 拒绝旧批准且保持停用 |
| AB-30 | 普通回滚及停用对账均不能恢复过时批准 |
| AB-31 | 真实签名治理与审计恢复生命周期兼容 |
| AB-32 | 演示前后七存储一致 |
| AB-33 | 无新增生产导入，生产标志关闭 |
| AB-34 | 原跨平台矩阵保留；实际最终 SHA 的 job、workflow 和平台计数见最终报告 |

额外覆盖五角色分离、缺少前置的构造拒绝、派生指纹签发安全、strict 资格不升级调用方断言、
decision 身份/版本/观测指纹、矛盾记录、同账本状态的批准身份冲突、账本并发变化、
视图过期、连续性丢失、快照绑定及演示失败退出。旧测试未修改，未新增 skip/xfail。

### 本地定向执行结果

本地 Windows、Python 3.12.10、仓库 `.venv`。指定测试文件逐项执行，合计 **436 passed**：

| 测试文件 | passed |
| :--- | ---: |
| test_runtime_activation_approval_binding.py | 65 |
| test_runtime_activation_authorization.py | 29 |
| test_runtime_activation_trust_hardening.py | 29 |
| test_trusted_time_authority.py | 73 |
| test_runtime_activation_recovery_binding.py | 16 |
| test_shadow_runtime_reader.py | 20 |
| test_shadow_activation_simulation.py | 18 |
| test_shadow_activation_audit_store.py | 12 |
| test_shadow_runtime_authority_corrections.py | 50 |
| test_runtime_mapping_readiness.py | 59 |
| test_runtime_activation_security_review.py | 7 |
| test_canonical_mapping_governance.py | 45 |
| test_concurrency.py | 13 |

合成演示为 **12/12**，正常运行和 python -O 均退出 0。
最终源码执行 `python -m pytest -q`：**1697 passed in 190.49s**，无失败、skip 或 xfail。
最终提交的 Ubuntu/Windows job 计数与整体 workflow 结论在最终交付报告中记录。

## 8. 七存储证据与生产残余

以本轮预检的实际本机文件为准，不能复用上一轮运行时数据的哈希。对照值为：

| 存储 | 本轮起始 SHA-256 |
| :--- | :--- |
| daily_matches.json | `ff844f20cbd0ef3309f83ace2a7730edf35ed5d6f4dca0ca927e17f3ab955f0a` |
| team_strength.json | `f406f1ee4da8581aecfd1574b646fc84101aa5a1e2fef92811ca02a90cc491ea` |
| calibrated.json | `b5bd53a9476c5ab5f2c9480cc652ecb4a12f3ade8903a3985a97aa8f44c42c0d` |
| prediction_snapshots.json | `ab3f60a7d244ebb69b60303b742aec04817528ceb4dd6a27387e7d28c1d0197a` |
| odds_snapshots.json | `13fdd261488303b0a7d2dcb2864ed1e328b2a20b701ffc12bcd6007d7ecf6eec` |
| settlements.json | `bf29628eba8ef662d21e94f63ed2c2482ca572ba54ad1f4769ef7314425e7280` |
| evaluation_rows.json | `6ccbf7a2870199e8f06e0da1c55f537df6bf77f4af1266a0df9351ead8571ef0` |

B-07 的组件实现与隔离验证不等同于生产关闭。生产 owner/view 服务、信任锚运维、
全局当前状态分发及持久会话连续性未部署。
B-13 的完整 supersession 策略、B-14 的生产撤销权威仍是单独待办，没有自动实施。

| 概念 | 本次含义 |
| :--- | :--- |
| 历史认证批准 | 证明 owner 曾对特定提案签名 |
| 当前权威批准 | 已认证历史中仍 APPROVED、由当前签名视图公布的逐提案记录 |
| 资格判定 | 满足既有运行期资格规则的只读判断 |
| 激活授权 | 独立激活签名者授予的有界、可撤销授权 |
| Shadow 消费 | 在显式选中快照及所有当前证据通过后允许隔离解析 |
| 生产映射权威 | 本次不存在，也未授予 |

**B-07 = COMPONENT_IMPLEMENTED / COMPONENT_VERIFIED / ISOLATED_PROTOTYPE_ONLY / PRODUCTION_CONTROL_NOT_READY。**
**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO。**
