# 显式激活授权替代与确定性影子解析

**范围**：B-13 的隔离组件；不构成生产授权注册表。
**起始基线**：`10ecca5fa57d275773042bb609bcf580020aac01`（`develop`）。
**生产决策**：`PRODUCTION_ACTIVATION_READY = NO` / **NO-GO**。

## 1. 基线审计与可执行复现

实现前已读取 AGENTS.md，并检查治理、权威视图、资格、策略、激活签名、Reader、模拟器、
审计存储、可信工件、可信时间、恢复以及对应测试。基线工作区干净，无未发布提交，
本地 HEAD 与 origin/develop 完全一致；origin/main 为
`bb8bbe542449ceca3964bae42ad6f132b33b9af2`。
前置 [CI 40478749194](https://github.com/YGone-001/Daily-Sports-Lottery/actions/runs/40478749194)
及四个作业均为 completed/success。

`tools/shadow_supersession_baseline.py` 复用原有非 supersession 模式执行，首次执行发生于产品代码修改前。
后续补充了显式 caller claim 和独立合成角色签署的外部声明，使复现输入可直接检视。
该旧模式仍保留以下行为，便于持续比较。

| 场景 | 实际基线结果 | 严格模式修正结果与证据 |
| :--- | :--- | :--- |
| SP-01：A 已过期，B 有效 | REJECTED_SHADOW；ACTIVATION_AUTHORIZATION_EXPIRED | 签名 A→B、当前状态及检查点齐全时仅 B 解析；SS-01 |
| SP-02：A 已撤销，B 有效 | REJECTED_SHADOW；ACTIVATION_AUTHORIZATION_REVOKED | A 撤销保留为历史，B 独立通过验证；SS-02 |
| SP-03：两个同时有效授权，无替代关系 | **RESOLVED_SHADOW**，返回两个授权 ID | LIFECYCLE_AMBIGUOUS_CURRENT；SS-05 |
| SP-04：调用方声称 A 被替代 | REJECTED_SHADOW；旧 A 仍参与验证 | 缺失原始签名证据必拒绝；SS-03/04/07 |
| SP-05：外部签名 A→B 声明 | REJECTED_SHADOW；旧 A 仍参与验证 | 完整专用契约的签名 A→B + 当前状态 + 独立检查点使 B 解析；SS-06/34 |

> 基线签发 helper 尚未声明生命周期用途，Reader 也没有生命周期输入 API。
> 首次复现使用已有外层用途的签名声明；随后补充专用域、完整绑定字段与独立角色签名，
> 通过原有通用验证器独立验签，并在临时导出的精确基线源码上重跑。
> 即使外部声明的签名有效，基线 Reader 也不能据此认证当前生命周期。
> 修正场景还要求签名 CURRENT 状态及独立检查点，不能把签名存在混称为当前或生产权威。

## 2. 最小生命周期契约

新增 `utils/activation_supersession.py`，schema 为 **1.0.0**。
历史集合与有效快照采用分离设计：`AuthorizationLifecycleEvidence` 包含完整历史授权、
显式替代事件、签名当前状态、独立签名检查点；不包含可执行快照指针。

| 概念 | 表达 | 能否单独允许消费 |
| :--- | :--- | :--- |
| 已签发授权 | inventory 中不可变的原始 activation 工件 | 否 |
| 密码学完整性 | 可信 activation 公钥独立验签，校验原始提案、资格、范围 | 否 |
| 授权资格 | 复用既有 activation / StrictActivationVerifier 验证 | 否 |
| 当前授权 | 签名状态明确列出的 CURRENT，且必须等于替代图唯一末端 | 否 |
| 已替代授权 | 存在经签名认证的出边，selection=SUPERSEDED | 否 |
| 过期 / 撤销 | 分别保留 expired / revoked；可与 SUPERSEDED 同时成立 | 否 |
| 停用 | Reader 的 enabled=False，独立于签名完整性和替代关系 | 否 |
| 影子消费 | 当前授权 + 所有当前检查 + 注册表显式选择的有效快照 | 仅影子解析 |
| 生产激活权威 | **未部署、未授予** | 否 |

`VerifiedAuthorizationState` 是只读派生结果；不得作为后续读取的权威输入。
当前授权过期、撤销或存在歧义时不返回可消费视图，以稳定拒绝码表示不可消费状态。
`ISSUED` 体现为原始工件存在，不能从签名存在直接推导 CURRENT。

## 3. 签名替代证据与信任根

复用 `SignedTrustedArtifact`、`sign_trusted_artifact()`、`verify_trusted_artifact()`，新增三个独立签名域：

| 用途 | 签名角色 | 必要绑定 |
| :--- | :--- | :--- |
| RUNTIME_ACTIVATION_SUPERSESSION | 生命周期权威 | schema、dataset、source key、Canonical、baseline、scope、old/new activation ID 与完整工件指纹、old/new proposal ID、后继批准指纹、后继资格判定指纹、transition ID、sequence、effective/issued time、authority identity、reason |
| RUNTIME_ACTIVATION_LIFECYCLE_STATE | 生命周期权威 | 完整授权 inventory、事件指纹集合、显式 CURRENT 集合、治理视图指纹、dataset、baseline、sequence、issued time、authority identity |
| RUNTIME_ACTIVATION_LIFECYCLE_CHECKPOINT | 独立检查点权威 | 状态完整签名工件指纹、dataset、baseline、authority identity、sequence、当前 verifier challenge、issued time |

外层 `artifact_type` 及载荷 `purpose` 必须同时匹配；Ed25519 签名覆盖外层 schema、用途、载荷、signer key ID。
信任根在 verifier 构造时固定并记录公钥 ID，路径内容被换成其他公钥也不能建立新权威。
生命周期、检查点、activation、治理视图、时间五个角色要求公钥不同；生命周期密钥不能与当前 owner 相同。
工件不携带能够自立信任的公钥。owner approval 或 activation 签名不能替代生命周期签名。

**没有自动签名**：模块提供纯载荷构造函数；演示和测试才显式使用临时私钥签署合成工件。

## 4. 确定性当前状态派生

验证顺序如下：

1. 使用既有 TrustedTimeVerifier 验证当前时间，使用 StrictActivationVerifier 的既有批准视图验证和会话连续性。
2. 验证签名状态与独立检查点，匹配当前 dataset、Canonical baseline、治理视图及 challenge。
3. 完整核对 inventory：同 activation ID 不同签名或内容拒绝，调用方证据也不得替换 inventory 身份。
4. 独立认证每个替代事件，严格比对旧/新授权及各项绑定；替代不得改变 source、dataset、baseline、Canonical 或 scope。
5. 检查未知节点、环、分叉、多前驱、重复序号和非单调链；签名状态中的 CURRENT 集合必须正好等于图末端。
6. 同一个 scoped source 必须只有一个 CURRENT；不按签发时间、approval 时间、activation ID、快照新旧或输入顺序选择。
7. 每个历史授权独立复用原有 activation 验证器认证签名、提案、资格、范围及原始绑定；
   仅对已经认证 SUPERSEDED 的节点容许过期或撤销，不容许伪造、未知信任根或错误 revocation reference。
8. CURRENT 使用完整 StrictActivationVerifier，强制当前已认证批准、资格、可信时间、寿命、撤销、Canonical 及当前所有权。
9. 输出排序稳定的历史/当前条目。排序只用于规范序列化和诊断，绝不建立消费权威。

**过期、撤销、替代互相独立**：历史 A 同时 expired=True、revoked=True 时仍保持 SUPERSEDED；
只有独立有效的 B 能消费。当前授权过期或撤销一律拒绝，替代不能删除撤销事实。
历史批准无需被错误地解释为当前批准，当前 successor 的批准必须在 schema 1.1.0 治理视图中有效。

## 5. 重复、冲突、重放与连续性

**幂等规则**：完全相同的签名事件与授权可以重复输入，规范 inventory 去重；
同一状态和检查点可在新鲜度窗口内重复验证。相同 transition ID 对应不同完整签名工件拒绝。
同 activation ID 对应不同授权字节拒绝。每条非重复替代事件必须使用唯一正整数 sequence。

会话高水位串行加锁验证：状态序号不能后退，相同序号不能换状态，issued time 不能回退；
新状态必须保留全部已认证历史授权和事件。新增事件序号必须大于上次状态序号。
因此 A→B→C 后，删除旧事件或重放 A 无法使 A 再次成为 CURRENT。
认证控制状态与后继可消费性分开处理：已认证 A→B 即使因 B 过期、撤销或批准失效而无法消费，
也会推进控制状态高水位，旧的 A CURRENT 状态不能因此重新被接受。
认证撤销一旦被观察到，即使该次 CURRENT 验证因撤销失败，也不允许后续签名视图删除它。

默认生命周期新鲜度 **300 秒**，构造上限 **3600 秒**；状态、事件、检查点都独立验证新鲜度。
较长历史链若事件过旧会整体 fail closed，本实现没有自动重签或长期归档迁移服务。

**重启边界**：新 verifier 有新的随机 challenge，旧检查点无法复用，必须由独立可信检查点角色
针对新 challenge 签发当前状态证明。该角色必须在仓库外独立知道哪个状态是当前状态。
如果它错误地给旧状态签发新 challenge，本原型无法在进程重启后发现全局回滚。
本实现**没有持久化生产高水位、全局授权注册表、生产检查点发布服务或密钥管理后端**。
丢失活动 verifier 的会话状态会 fail closed，不能自行重置继续消费。

主要拒绝码：

| 类别 | 拒绝码 |
| :--- | :--- |
| 证据 / 契约 / 信任 | LIFECYCLE_EVIDENCE_MISSING、LIFECYCLE_MALFORMED、LIFECYCLE_CONTRACT_INVALID、LIFECYCLE_SIGNER_CHANGED、LIFECYCLE_ACTIVATION_SIGNER_CHANGED、LIFECYCLE_ROLE_SUBSTITUTION、LIFECYCLE_AUTHORITY_MISMATCH；以及既有 TRUSTED_ARTIFACT_* |
| 范围 / 当前治理 | LIFECYCLE_DATASET_MISMATCH、LIFECYCLE_BASELINE_MISMATCH、LIFECYCLE_GOVERNANCE_MISMATCH、LIFECYCLE_SCOPE_MISMATCH、LIFECYCLE_TRANSITION_BINDING_MISMATCH、LIFECYCLE_OWNERSHIP_MISMATCH |
| inventory / 图 | LIFECYCLE_INVENTORY_MISMATCH、LIFECYCLE_ACTIVATION_ID_CONFLICT、LIFECYCLE_EVENTS_MISMATCH、LIFECYCLE_UNKNOWN_AUTHORIZATION、LIFECYCLE_TRANSITION_ID_CONFLICT、LIFECYCLE_SUCCESSOR_CONFLICT、LIFECYCLE_CYCLE、LIFECYCLE_AMBIGUOUS_CURRENT、LIFECYCLE_CURRENT_MISMATCH |
| 时间 / 重放 / 连续性 | LIFECYCLE_TRANSITION_INVALID、LIFECYCLE_TRANSITION_TIME_INVALID、LIFECYCLE_SEQUENCE_INVALID、LIFECYCLE_STATE_ROLLBACK、LIFECYCLE_HISTORY_REMOVED、LIFECYCLE_CHECKPOINT_MISMATCH、LIFECYCLE_CONTINUITY_LOST、LIFECYCLE_REVOCATION_REMOVED |
| 历史 / 快照 / 恢复 | LIFECYCLE_HISTORICAL_ELIGIBILITY_INVALID、LIFECYCLE_SNAPSHOT_NOT_CURRENT、LIFECYCLE_SNAPSHOT_BINDING_MISMATCH、LIFECYCLE_ACTIVATION_NOT_CURRENT；以及既有 activation、approval、trusted-time、recovery 拒绝码 |

## 6. 快照兼容性与严格 Reader

**没有放宽 build_shadow_activation_snapshot()**。它仍拒绝过期、撤销、签名不合法或资格不合法的授权。
仅 B 进入有效快照，A 保留在不可变历史集合中。快照 wire format、内容指纹及 legacy 检视均保持原有契约。

`ShadowRuntimeReader(require_verified_supersession=True, supersession_verifier=...)` 构造时要求：
真实 ShadowSnapshotRegistry、authenticated governance、authenticated approval、trusted time，
以及与生命周期 verifier 相同的 StrictActivationVerifier。缺一即在构造期失败。

每次读取仍执行原有快照指纹、注册、显式选择、数据集、基线、版本、观测认证检查；
随后重新验证**原始**生命周期证据，要求整个有效快照与签名 CURRENT 集合一一对应，
拒绝重复、遗漏、历史混入、错误 scope 或 decision 指纹。选中快照不能单独充当生命周期权威。
最后执行既有 Reader 的授权再验证。歧义不降级为 NOT_FOUND；只有有效生命周期下的有效未知观测返回 NOT_FOUND。

旧快照在获得完整、匹配的当前生命周期证据后可安全使用；没有该证据的旧快照不能声称严格生命周期权威。
旧模式仍用于显式的非生产检视；strict supersession **不会回退**到旧的多绑定解析。

## 7. 再启用、回滚与紧急停用

严格再启用验证整个当前快照/生命周期，并核对待恢复授权的 ID 和完整工件指纹。
SUPERSEDED A 即使签名仍有效也不能恢复消费。既有恢复证据继续绑定审计存储身份、链尖、长度、
完成事件、选择/有效快照和 recovery generation；审计推进后旧证据拒绝。

回滚在既有外部审计检查点和当前授权检查外，增加完整生命周期/目标快照匹配检查。
旧 A 快照返回 LIFECYCLE_SNAPSHOT_NOT_CURRENT，不能恢复已替代授权。
已有有效 B 可继续保持选择；若当前证据本身无效，读取会 fail closed。
显式 disabled reconciliation 仍保持停用，完成后必须单独通过原有受限 re_enable()。

紧急停用无需 owner、activation 或 lifecycle 签名，立即使 Reader 不可消费并使旧 generation 句柄失效。
生命周期认证成功也不能自行打开 disabled Reader。无任何自动替代签名或恢复签名。

## 8. 可执行演示与测试矩阵

```powershell
python -m tools.shadow_supersession_baseline
python -m tools.shadow_activation_supersession_validation
python -m pytest -q tests/test_activation_supersession.py
python -m pytest -q
```

本机使用现有 `.venv/Scripts/python.exe`（Python 3.12.10，cryptography 50.0.2），
系统 Python 缺少 cryptography，因此不更改全局 Python 环境。
演示使用临时 workspace，owner、activation、lifecycle、ledger checkpoint、governance、time 六个角色
分别生成临时 Ed25519 密钥，另有独立 recovery 密钥。没有访问生产私钥、网络抓取或生产写入。
输出结构化检查结果和七存储 SHA-256，任何实际验证失败返回非零退出码。

| 测试编号 | 证据范围 |
| :--- | :--- |
| SS-01～SS-07 | 历史过期/撤销、无替代证据、双 CURRENT、正确签名替代、缺失证据 |
| SS-08～SS-17 | 伪造、错误角色/用途、dataset、baseline、source、owner、approval、eligibility、scope |
| SS-18～SS-24 | 相同 activation ID 异内容/签名、事件幂等/冲突、环、分叉、非可信/旧签名状态 |
| SS-25～SS-31 | 当前撤销/过期、旧批准/基线、A→B→C、A 重放、不可变历史保留 |
| SS-32～SS-35 | 注册/内容顺序、有效快照唯一性、严格 Reader B、歧义拒绝 |
| SS-36～SS-40 | A 再启用拒绝、A 回滚拒绝、紧急停用、可信时间/寿命、恢复证据 |
| SS-41～SS-43 | 生产导入隔离、实际演示/七存储字节、复用批准/激活引擎；旧完整套件另行运行 |
| SS-44 | 测试检查跨平台 CI 配置；**四个远端作业实际结果须另查最终提交的 GitHub Actions** |
| 附加测试 | 状态/检查点伪造、检查点过旧、重启 challenge、会话状态丢失、角色替代、缺撤销清单、序号类型/边界、历史伪造、旧恢复证据、未知观测 |

未新增 skip/xfail，未放宽既有 AC/GC/RC/RE/TT/AB 断言。
原有 AA-12 的签名篡改夹具原先固定把首字符写成 A，随机签名本身以 A 开头时实际没有篡改；
现改为保证翻转首字符，保留原有拒绝断言，消除该测试的概率性无效输入。

本机最终执行结果（2026-10-28，Windows / Python 3.12.10）：

| 验证 | 实际结果 |
| :--- | :--- |
| 新增 supersession 套件 | **82 passed，0 failed，0 skipped** |
| 指定的 14 组安全/并发套件 | 合计 **518 passed，0 failed，0 skipped** |
| 完整 `python -m pytest -q` | **1779 passed，0 failed，0 skipped**（212.07 秒） |
| 功能演示 | **17/17** 实际检查通过，退出码 0 |
| 精确基线导出复现 | SP-01～SP-05 全部实际执行；结果见第 1 节 |
| 七个实际生产 JSON | SHA-256 前后完全一致；完整散列见演示输出 |

远端四个作业的最终状态在推送后核对，并写入交付报告；上述本机结果不代替远端 CI。

## 9. 生产隔离与剩余风险

B-13 状态区分为 **COMPONENT_IMPLEMENTED / COMPONENT_VERIFIED / ISOLATED_PROTOTYPE_ONLY /
PRODUCTION_CONTROL_NOT_READY**；组件验证证据是新增测试、实际演示、指定旧套件和完整回归的执行结果。
远端 CI 必须针对最终提交核验，不用实现存在或 CI 配置存在代替实测成功。

**没有生产生命周期权威、生产快照发布、全局当前性/持久回滚保护。**
B-14 的生产独立撤销权威、更新可达性、密钥分发和服务部署仍单独待办。
当前 shadow 继续消费既有签名治理撤销事实；调用方空撤销表不能覆盖它们，替代不会擦除历史撤销。

没有修改 scraper.refresh()、same_event()、ingestion identity、Canonical ID、历史 source ID、
模型/Elo 数学、结算/评估公式、React 前端、生产 data/*.json 或 main。
没有运行生产 repair apply/recover，没有生产 activation API、读取器、签名器或撤销服务。

```text
PRODUCTION_RUNTIME_READER       = NOT_ENABLED
PRODUCTION_ACTIVATION           = NOT_ENABLED
PRODUCTION_MAPPING_WRITE_AUTHORIZATION = NOT_GRANTED
PRODUCTION_ACTIVATION_READY     = NO
DECISION                        = NO-GO
```
