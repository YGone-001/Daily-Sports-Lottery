# 独立撤销权威与认证当前状态

**范围**：B-14 的隔离 shadow 组件。**起始提交**：`fcfe1a27882add98f897a96b85f1d20f95bca0ce`。
**生产决策**：`PRODUCTION_ACTIVATION_READY = NO` / **NO-GO**。

## 1. 预检与基线复现

已读取 AGENTS.md；起始 HEAD 与 origin/develop 完全匹配，工作区干净，无未发布提交。
origin/main 为 `bb8bbe542449ceca3964bae42ad6f132b33b9af2`，未更改。
前置 [工作流 40633281580](https://github.com/YGone-001/Daily-Sports-Lottery/actions/runs/40633281580)
整体 Success，四个作业 completed/success。整体状态接口一度限流，使用 GitHub 公开页面独立核对整体 Success 与精确提交；作业状态由连接器核对。

产品实现修改前执行 `python -m tools.shadow_revocation_baseline`。所有输入、密钥和状态均为临时合成工件。
基线没有独立撤销输入 API；复现把外部状态置于扩展 context 并独立验签，不假称基线已经认证其当前性。

| 场景 | 实际基线结果 | 结论 |
| :--- | :--- | :--- |
| RV-01：外部签名撤销存在，调用方登记为空 | 普通 activation 验证仍 is_consumable=True | 普通路径不能发现外部独立撤销 |
| RV-02：较新撤销存在，仍使用新鲜旧治理 V1 | RESOLVED_SHADOW | 新鲜签名不等于独立撤销权威的当前状态 |
| RV-03：外部签名 S1 有撤销、S2 删除撤销 | 两次扩展 context 读取均 RESOLVED_SHADOW | 基线没有独立状态输入；**已有**生命周期验证器对观察过的治理撤销删除返回 LIFECYCLE_REVOCATION_REMOVED |
| RV-04：当前授权已在签名治理视图撤销 | 读取、再启用和停用对账回滚均拒绝，含 ACTIVATION_AUTHORIZATION_REVOKED | **已有保护**，本次保留 |
| RV-05：A→B，签名治理撤销 A | B RESOLVED_SHADOW，A 历史撤销保留 | **已有保护**，本次保留并扩展独立来源 |
| RV-06：完整签名空状态与独立检查点 | 外部证明有/无时均 RESOLVED_SHADOW，基线不认证独立证明 | 不能据此声称独立完整当前非撤销 |

## 2. 六个不同的信任概念

| 概念 | 证明范围 | 不得推导的结论 |
| :--- | :--- | :--- |
| 已签名撤销事件 | 某角色签署了特定授权身份的不可变撤销事实 | 不能单独证明当前清单完整 |
| 认证完整当前状态 | 指定 dataset/baseline/scope 的完整事件、授权 inventory、撤销集合 | 不能单独证明生产全局最新 |
| 独立检查点 | 独立角色为精确状态、序号、事件数、inventory、当前 challenge 作证 | 不能自行创建或删除事件 |
| 进程内单调连续性 | 已接受状态不能回退或删除历史 | 不能替代重启后的独立高水位 |
| 生产全局当前性 | 需要生产签发者持久化、独立检查点权威及可达性 | 本次未部署 |
| 生产映射激活权威 | 需要独立生产授权与启用流程 | 本次未授予 |

## 3. 事件 schema、身份与角色

新增 `utils/activation_revocation_authority.py`，schema **1.0.0**，复用现有
`SignedTrustedArtifact`、`sign_trusted_artifact()`、`verify_trusted_artifact()` 与 Ed25519 后端。
模块只构造纯载荷和执行只读验证，不签发工件，不创建私钥、服务或生产存储。

| 工件 | 专用签名用途 | 主要绑定 |
| :--- | :--- | :--- |
| 不可变事件 | RUNTIME_ACTIVATION_REVOCATION | schema、确定性事件 ID、dataset、activation ID、完整授权指纹、revocation reference、source key、Canonical、activation authorizer、撤销权威、effective/issued time、sequence、reason、signing role |
| 完整状态 | RUNTIME_ACTIVATION_REVOCATION_STATE | schema、权威与 scope、dataset/baseline、完整授权 inventory、事件指纹集合、明确撤销集合、状态序号、签发时间、新鲜度策略、生命周期完整状态指纹与序号 |
| 独立检查点 | RUNTIME_ACTIVATION_REVOCATION_CHECKPOINT | 精确状态签名工件指纹、dataset、权威、状态序号、事件数、事件 inventory 指纹、issued/expires time、会话 challenge、新鲜度策略与角色 |

事件 ID 是事件完整规范载荷（不含 ID 自身）的 SHA-256 指纹；外层签名还覆盖签名用途、schema、signer key ID 和载荷。
事件、状态、检查点都是不可变 SignedTrustedArtifact。状态的 fingerprint 是完整签名工件指纹。

`IndependentRevocationVerifier` 构造时固定七个不同的公钥身份：owner、activation、lifecycle、revocation、
revocation checkpoint、governance view、trusted time。实际演示另外保留既有独立 ledger checkpoint 与 recovery 密钥。
Reader 同时开启 supersession 时，两个 verifier 必须一致认定 dataset、activation 与 lifecycle 信任根。
公钥路径被替换不会改变已固定的密钥身份；嵌入式公钥不能建立自己的权威。

**事件策略**：完全相同的签名事件重复输入幂等；同事件 ID 异内容拒绝。同一授权的第二个不同撤销事件拒绝。
未知 activation 直接拒绝整个证据，未实现 pending 模式。不同签名授权复用同 activation ID 拒绝。
不支持撤销撤销、恢复或重置授权身份；未来 rescission 必须独立授权。

## 4. 完整状态与当前非撤销

验证器先复用 TrustedTimeVerifier 与 StrictActivationVerifier 认证当前时间、治理视图与批准视图连续性，
再验证状态及独立检查点。状态必须与完整授权 inventory、全部事件和显式撤销集合逐项吻合。
每个引用的授权复用原 activation 验证器验证签名、资格、提案、dataset、baseline 和 scope，
历史身份验证允许其已经过期，**不因此授予消费**；当前消费仍执行完整 StrictActivationVerifier。

完整范围固定为 `SCOPED_SHADOW_ACTIVATION_AUTHORIZATIONS`，并绑定当前 baseline 和精确授权 inventory。
启用 supersession 时，inventory 包含该生命周期的全部当前与历史授权，状态同时绑定精确 lifecycle state fingerprint/sequence。
不能把旧的 A 范围撤销状态用于新的 A→B→C 当前生命周期。

合法空状态仍必须具有签名状态、精确 inventory 和独立新鲜检查点。
调用方空 tuple、缺失事件列表、缺失状态或缺失检查点均不能证明当前未撤销。
完整性表示可信签发者对所声明范围的完整性作证；不能证明签发者从未隐瞒尚未被观察到的事实。

## 5. 检查点、可信时间和新鲜度

新鲜度策略名为 **isolated-revocation-v1**：默认 **60 秒**，构造上限 **300 秒**。
该值只用于隔离原型，**不是生产撤销 SLA**。

状态和检查点均用 TrustedTimeVerifier 的认证有效时间检查；不使用 raw caller current_time。
检查点还要求 `state.issued_at <= checkpoint.issued_at < checkpoint.expires_at`、
有效区间不超过配置窗口、当前时间严格早于 expires_at，以及精确会话 challenge。
未来状态、未来事件、过旧状态、过旧检查点、错误签名/用途/角色/权威/清单均拒绝。
只刷新检查点不能延长旧状态的有效期。

撤销事件是不可变历史，不因状态新鲜度窗口过去而失效或重签。
每次仍验证事件签名、严格 UTC 时间、生效不早于授权签发、不晚于事件签发，且事件签发不晚于状态签发。
完整当前状态和检查点认证哪些历史事件完整地属于当前状态。

## 6. 单调连续性、重放与并发

会话内 RLock 串行验证状态。保留最新序号、完整状态指纹、事件 ID/指纹、授权身份 inventory、
已撤销身份、观察过的签名治理撤销和连续性 generation。每个 inventory 上限 **1024**，序号为有界非负整数。
状态序号可以前进；较低序号拒绝，相同序号只能重复完全相同的签名状态。

新状态必须保留全部已接受事件和授权身份，新增事件序号必须高于前次状态序号。
事件缺失、清单截断、相同序号异内容、授权身份改写及撤销消失均拒绝。
认证撤销的记忆独立于后续读取是否因撤销而失败，不会通过消费失败撤回高水位。

丢失已建立的连续性对象或回退 generation 会 fail closed。
Reader 对新的认证撤销 generation 使旧解析句柄失效；外部直接推进同一 verifier 也会令旧句柄的 generation 检查失败。
Reader 本地同步避免撤销代际回退，并拒绝验证期间变化的撤销状态。
**句柄代际检查仍不是生产授权证明**，当前消费必须重新 read，不得使用旧 RESOLVED_SHADOW 绕过验证。

## 7. 治理事实对账与 supersession

独立状态与既有认证治理撤销采用**保守合并**：任一认证来源撤销当前授权就拒绝该授权。
不按调用方时间戳选择来源，不删除既有签名治理事实，不让空 caller registry 覆盖任何认证来源。
治理撤销的未知 ID 或错误 reference 直接拒绝；已观察治理撤销在后续视图消失也拒绝。

| 生命周期组合 | 独立撤销结论 | 消费结果 |
| :--- | :--- | :--- |
| A CURRENT | 完整当前状态证明未撤销 | 其余检查通过才解析 |
| A CURRENT | A 已撤销 | 拒绝 |
| A SUPERSEDED、B CURRENT | A 撤销、B 未撤销 | A 保留为历史撤销，B 可解析 |
| A SUPERSEDED、B CURRENT | B 撤销 | 拒绝 B，不恢复 A |
| A→B→C | A/B 撤销、C 未撤销 | 其余检查通过仅 C 解析 |
| A→B→C | C 撤销 | 拒绝 C，不恢复 A/B |
| 生命周期已改变 | 撤销状态仍绑定旧生命周期 | 拒绝 REVOCATION_LIFECYCLE_MISMATCH |

既有 lifecycle view 的 revoked 字段继续描述其治理事实；独立撤销与治理合并后的结论由
VerifiedRevocationState 表达，Reader 对所有当前有效绑定强制采用该合并结果。

## 8. 严格 Reader、再启用、回滚与停用

`require_independent_revocation=True` 在构造时要求真实 registry、authenticated governance、
authenticated approval、trusted time 和同一个 StrictActivationVerifier。没有隐式回退。
与 supersession 同时开启时还检查两个组件的根身份一致性。

每次读取保留原有选择快照、快照指纹、baseline、dataset、版本、观测和当前所有权检查，
并验证独立完整状态与检查点、显式生命周期（若启用）、当前批准和 activation 授权。
授权验证仍使用既有引擎；独立撤销只附加拒绝能力，不能把原引擎拒绝的授权变成允许。

再启用重新验证当前撤销证据及选择快照，继续强制当前审计恢复状态、批准、时间、寿命和生命周期。
再启用单独传入的授权参数也必须以 activation ID 和完整工件指纹匹配认证 inventory，不能只凭相同 ID 替换工件。
回滚与 disabled-reader reconciliation 都重新验证撤销，不能使用旧的非撤销结果恢复授权。
既有审计未提交、结果不确定和指针失败路径保持原 fail-closed 行为。
紧急停用不依赖撤销服务可用性，不需要 owner/activation/revocation 签名，也没有生产停用 API。

## 9. 可执行演示与测试证据

```powershell
python -m tools.shadow_revocation_baseline
python -m tools.shadow_revocation_authority_validation
python -m pytest -q tests/test_activation_revocation_authority.py
python -m pytest -q
```

本机使用既有 `.venv/Scripts/python.exe`（Python 3.12.10）。
演示使用临时目录，真实创建 owner 批准、治理视图、activation、生命周期、独立撤销及检查点签名，
实际执行 Reader、受限恢复再启用、回滚和停用方法；输出确定性结构化 JSON，任何安全不变量失败退出非零。
演示将 A 从未撤销推进到已撤销，拒绝旧空状态，再显式授权 B、A→B、验证 B 恢复，最后撤销 B 并拒绝恢复和回滚。

| 测试范围 | 内容 |
| :--- | :--- |
| VR-01～VR-11 | 正确状态、签名/角色/用途/dataset、授权指纹/reference/source/Canonical、重复与冲突 |
| VR-12～VR-23 | 历史缺失、截断、旧状态、同序号冲突、检查点 signer/状态/序号/事件数/challenge、新鲜度和时间 |
| VR-24～VR-35 | caller 空表、旧治理视图、当前/历史撤销、A→B→C、旧状态、严格证据与缓存失效 |
| VR-36～VR-46 | 再启用、普通/停用对账回滚、停用、恢复、批准、寿命、supersession、重启、连续性丢失、角色替代、无 rescission |
| VR-47～VR-50 | 实际演示与七存储、生产导入/API 隔离、复用旧引擎、CI 配置；旧完整套件和最终远端 CI 另行实测 |
| 附加对抗测试 | 清单上限、可信根路径替换、事件排序、generation 丢失、过旧状态的新检查点、并发、治理对账、真实恢复链、独立模式与生命周期模式兼容 |

没有修改旧安全断言，没有新增 skip/xfail。远端四作业必须针对最终提交重新核验，不能以配置或本机通过替代。

本机实际结果（2026-10-28，Windows / Python 3.12.10）：

| 验证 | 执行结果 |
| :--- | :--- |
| 新增独立撤销套件 | **97 passed，0 failed，0 skipped** |
| 指定 15 组安全及并发套件 | 合计 **615 passed，0 failed，0 skipped** |
| 完整 `python -m pytest -q` | **1876 passed，0 failed，0 skipped**，216.47 秒 |
| 真实合成演示 | **17/17** 检查通过，退出码 0 |
| 七个生产 JSON | 与实现前基线的 SHA-256 **7/7 相同** |

最终提交的远端作业及整体工作流结果在推送后核验，写入最终交付报告。

## 10. 重启、密钥失陷与生产限制

新 verifier 有新 challenge，旧检查点不被接受；恢复后必须取得独立可信角色对当前状态的新检查点。
**对旧状态重新签名并不证明全局当前性**：只有检查点签发者独立持久化并维护真实高水位时，才可能建立该保证。
没有独立当前检查点时必须停止消费，不能自行新建空状态代替。

本组件不能阻止失陷或恶意签发者在首次观察前故意签出虚假的完整状态。
已观察历史在同一会话中受单调检查保护；跨重启需要生产签发者持久化、独立检查点权威、密钥分发与外部审计。
检查点角色失陷也可能对旧状态签发新 challenge，本组件不声称在该情况下仍有生产全局回滚保护。

密钥身份在构造时固定，**没有自动轮换或多代密钥迁移**；替换公钥路径会拒绝。
未来轮换需要单独授权、历史验签策略和连续性迁移。没有 KMS、生产撤销服务器、持久数据库或生产可达性保证。

## 11. 组件状态与生产隔离

B-14 的隔离组件状态区分为 **COMPONENT_IMPLEMENTED / COMPONENT_VERIFIED / ISOLATED_PROTOTYPE_ONLY /
PRODUCTION_CONTROL_NOT_READY**。生产独立撤销服务仍待部署与独立复评。

没有修改 scraper.refresh()、same_event()、ingestion identity、Canonical/source ID、足球/篮球或 Elo 数学、
结算/评估、React 前端及生产 JSON。没有生产 repair apply/recover、生产私钥、自动批准、自动授权续期、
自动替代激活、生产 activation API 或 B-09 生产快照发布。

```text
PRODUCTION_RUNTIME_READER              = NOT_ENABLED
PRODUCTION_ACTIVATION                  = NOT_ENABLED
PRODUCTION_MAPPING_WRITE_AUTHORIZATION = NOT_GRANTED
PRODUCTION_ACTIVATION_READY            = NO
DECISION                              = NO-GO
```
