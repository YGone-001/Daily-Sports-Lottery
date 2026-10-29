# 离线根引导与固定权威验证试验

## 1. 范围、基线与证据分层

起点 **4f4da993a27dc0c5362f38bc228b463ba364999b**，分支 develop；保留 main
`bb8bbe542449ceca3964bae42ad6f132b33b9af2`。预检工作区干净、HEAD=origin/develop，
前次 [workflow 40824167722](https://github.com/YGone-001/Daily-Sports-Lottery/actions/runs/40824167722)
整体 completed/success，四作业全部成功。已重读 AGENTS.md。

本次实现 **NON-PRODUCTION FUNCTIONAL SECURITY PILOT**。密钥全为系统临时目录内的合成密钥，
独立 pin 与 continuity expectation 由测试 harness 预置。没有注册生产根、选择 KMS/HSM、分配真实 owner、
修改生产入口、修改 Canonical/模型/结算、创建生产 API 或部署服务。

| 证据状态 | 本次含义 |
| --- | --- |
| DESIGNED | 根 bundle、transition、独立 approval、连续性及固定适配器契约 |
| OWNER_ASSIGNED | 生产仍 NOT_ESTABLISHED；synthetic actor 不代表真实职责接受 |
| APPROVED | 仅合成签名批准；生产根/组织 owner 未批准 |
| IMPLEMENTED | 隔离 Python 组件已实现，生产部署未实现 |
| VERIFIED_IN_TEST | 以实际专项、完整回归及功能 JSON 为依据；精确计数在最终交付回复 |
| VERIFIED_IN_DEPLOYMENT | 未达到；没有实际生产平台验证 |
| PRODUCTION_AUTHORIZED | NO；禁止从试验成功推断授权 |

**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO。**

## 2. 实施前复现 RB-01–RB-06

在新增 bootstrap 模块前，使用仓库既有签名原语与 TrustedTimeVerifier、仓库外临时合成密钥实际复现。
此时没有 root-bundle API；RB-04 使用既有允许用途下的 bundle-shaped payload，不能冒称原本已有根 bundle 验证器。

| 编号 | 实际结果 | 已有保护与新增工作的边界 |
| --- | --- | --- |
| RB-01 | caller 控制可信公钥时，匹配签名工件通过 | 密码学验证在所给根下有效，不证明根来源 |
| RB-02 | 同时替换工件及 caller 公钥可通过 | 固定原公钥时已拒绝 TRUSTED_ARTIFACT_SIGNER_UNTRUSTED |
| RB-03 | 任意合法本地公钥可载入；返回 key/raw/public_key_id | 不包含生产注册或职责接受证明 |
| RB-04 | 接受 generation=2 的泛型签名载荷后，旧 generation=1 仍可验签 | 原语没有根版本语义，不把此事实称为签名算法缺陷 |
| RB-05 | 同会话回退拒绝；旧 challenge 在新会话拒绝；新 challenge 下重签 generation=1 可接受 | 已有 nonce/会话保护真实有效，但没有跨重启独立版本锚 |
| RB-06 | 有效签名跨用途拒绝 TRUSTED_ARTIFACT_TYPE_MISMATCH | 保留既有 domain separation，不重写验签引擎 |

`baseline_reproduction()` 可再次执行这些未改变的原语边界；上表和下面登记记录的是本次实施前实际结果。

## 3. Bundle schema 与确定性编码

`utils/trust_root_bootstrap.py` 仅复用 `trusted_artifacts` 的 Ed25519 签发/验签。
既有原语只增加三个允许用途：OFFLINE_TRUST_ROOT_BUNDLE、OFFLINE_TRUST_ROOT_TRANSITION、
OFFLINE_TRUST_ROOT_CHANGE_APPROVAL；签名实现与旧工件规则不变。

| 字段 | 约束 |
| --- | --- |
| schema_version | 精确 1.0.0；未知 schema 拒绝 |
| bundle_identity | 非空身份；转换时不同于前任 |
| environment_identity / dataset_identity | 必须等于独立 BootstrapPolicy 的范围 |
| root_policy_version | 精确 offline-root-policy-v1；只代表合成试验政策 |
| root_generation | 严格正整数，转换只允许前任加一 |
| minimum_accepted_version | 不超过 generation，满足外部最低版本且不得回退 |
| authorities | 唯一 authority 身份、唯一角色与独立公钥；不接受冲突/缺失必需角色 |
| public_key_pem / public_key_fingerprint | 规范 Ed25519 公钥，指纹以既有 public_key_id 算法重算 |
| authorized_purposes | 精确匹配该已注册角色的现有用途，不准增添任意 grant |
| signing_not_before / signing_not_after | 严格 UTC、开始早于结束；根与工件签发/当前用途按边界检查 |
| revoked_key_identities | 排序、唯一、有界的指纹列表；根吊销阻断 bootstrap，角色吊销阻断当前使用 |
| predecessor_bundle_fingerprint | 初始为空或明确前任摘要；转换必须精确引用当前 bundle |
| issuer_identity | 必须匹配 root-bundle 角色 |
| independent_approval_evidence_reference | 与独立签名 approval_id、目标 bundle 和角色变化共同绑定 |
| bundle_fingerprint | canonical_json 后 SHA-256；自身字段不参与自身摘要 |
| issued_at | 受控合成时间；由既有 freshness 验证器验证 |

对象键使用既有 canonical_json；authority 数组按角色排序、撤销与用途数组排序唯一。
SignedTrustedArtifact.to_json 提供规范 wire 序列化，VerifiedBootstrap 以不可变 canonical JSON 保存副本。
修改调用方字典、返回的 body 副本或请求 key map 不能改写已验证配置。

必需角色为 root-bundle 加 owner-approval、activation、governance-view、trusted-time、lifecycle、
revocation、audit-checkpoint、control-checkpoint、recovery。另支持八个已存在的 ledger/Canonical/
治理撤销/观测/lifecycle-checkpoint/revocation-checkpoint/publisher/selector 角色。
lifecycle 和 revocation 各保留其既有 event/state 双用途，不发明新的权威语义。
独立 root-change approver 不在普通 bundle 内授权，其公钥和身份来自第二个带外 pin。

## 4. 独立 bootstrap 与状态

BootstrapPolicy 是可信 harness 的部署前输入，不是应用请求。initial root fingerprint 先从独立生成的
公钥文件登记，再构建 bundle；验证器不从未验证 bundle 中推导 expected pin。
独立 approver 使用另一把 pin 固定的公钥。初始注册也必须有其真实合成签名批准。

| 状态/拒绝码 | 发生条件 |
| --- | --- |
| BOOTSTRAP_NOT_ESTABLISHED | 尚未建立会话、缺 pin 或试图从未验证状态建 context |
| BOOTSTRAP_VERIFIED | 根签名、独立批准、范围、角色、版本、签发期与外部 continuity 全部通过 |
| ROOT_TRANSITION_REQUIRED | 活动会话中企图直接接纳新根，或缺少 transition |
| ROOT_UNTRUSTED | pin、实际文件、已认证 signer 指纹不符 |
| ROOT_VERSION_ROLLBACK | 旧 generation、旧 handle 或低于独立期望的版本 |
| ROOT_REVOKED | 根/角色签名身份已吊销 |
| CONTINUITY_UNVERIFIED | 独立 expectation 缺失/不匹配，或已建立会话丢失高水位 |

错误以稳定 rejection_codes 返回；无效请求不覆盖已经成立的根。status 表示最后有效会话或连续性丢失，
不会因攻击者提交一个无效 bundle 自动撤销仍有效的会话。
公钥文件在 pin 检查与共享验签之间被替换时，仍须将实际认证 signer 与固定指纹再次比对，不能仅信路径。

## 5. 根转换、吊销与历史检查

转换同时要求：旧根签名 transition、新根签名 bundle，以及独立 approver 的 approval。
approval 绑定 old/new bundle 指纹和 generation、environment/dataset、policy、完整角色 assignment 变化、
approval_id、requester/executor/approver；三种 actor 必须不同，approver key 不能与任何角色 key 相同。
transition 还绑定 old/new identity、predecessor、approval 指纹与明确角色变化。
仅旧根签新根、仅自签新根或仅替换全部公钥均不构成授权。

新 bundle 必须保留全部撤销并退休被替换/删除的旧 key，旧根不能悄悄返回。
已验证旧 bundle、原始签名 envelope、独立 approval/transition 和旧公钥在本会话历史中保留，inspect_historical 执行真实原始签名与签发期检查，
但始终返回 current_authorization_eligible=false。历史密码学有效不代表失陷 key 当前仍获授权。
这里没有实现生产吊销服务、运营密钥轮换或全局失陷裁决。

## 6. Continuity 与重启限制

ContinuityEvidence 必须由独立 harness 提供 environment、dataset、expected generation、
expected bundle fingerprint、minimum version 与 evidence reference。reference 只是审计标签，
不是外部真实性的密码学证明；独立性来自受信 harness 的配置/分发边界。
不得由被验证请求生成这些字段，也不将可写本地 metadata 伪装为独立生产锚。

高水位仅在内存；状态丢失后本会话 fail closed，不能再次调用 bootstrap 偷偷重置。
重启必须重新取得独立当前 expectation。只有旧 pin/旧 bundle，不能接纳新根；只有旧 expectation，
也无法证明全局当前。试验中的重新引导要求新带外 pin、独立重新注册批准及当前版本/摘要 expectation，
不能自动把普通请求带来的“新根”变成可信配置。
已有 handle 在转换后失效；role 验证和严格读取返回前再次 fence 根状态，拒绝校验中发生的转换。

**不声称生产持久防回退、跨主机线性一致性或生产全局最新性。**

## 7. 固定权威 context 与现有严格链路

FixedAuthorityContext 只接受仍属于当前会话的 VerifiedBootstrap，固定 authority identity、
role→key、环境、dataset 和绝对公钥路径；映射与字段不可普通赋值修改。
角色验证必须从 bundle 解出固定 key，再调用既有 verify_trusted_artifact；owner approval 仍调用
verify_mapping_approval；时间仍使用 TrustedTimeVerifier，不引入另一套批准/治理判定规则。

bind_read_context 对 activation/view/observation/recovery 四个根字段逐项检查；
不同 key、不同路径（包括同一公钥的另一个文件）与 authority_paths 覆盖全部拒绝。
read_shadow 要求原 Reader 全部严格模式、固定原 verifier 图与身份，核对 lifecycle/revocation/
control 路径，真实验证观测角色后调用原 Reader，再 fence 当前根版本。
旧 module 的入口和默认参数均未改；裸 verifier 或旧结果不是当前 bootstrap 授权证明，
应用应通过受控适配器逐次消费，不能把一次取出的公钥路径当永久许可。

生产强制部署仍未实现：任意可信 Python 代码若可自行创建新 policy，仍可选择自己的 pin。
本试验验证受信构造边界与不可信请求边界，不宣称抵抗进程任意代码执行、管理员目录替换或真实多服务串谋。
旧工件没有统一 environment wire 字段；环境来自固定的签名 bundle/key 授权及明确请求范围核对。
若存在 environment 字段则必须匹配。跨环境复用同一私钥的全局发现/治理不在本地试验保证内，
生产必须防止这种复用并建立独立注册来源；不能据此声称旧工件已新增环境签名字段。

RoleSeparatedShadowScenario 仅在合成工具中复用原场景，并为原夹具共享的 ledger/Canonical/
治理撤销/lifecycle checkpoint 分配独立临时 key；原安全模块不变。
真实链路仍执行完整 owner→ledger/view→activation→time→lifecycle→revocation→snapshot→selection→Reader，
结果仅为 RESOLVED_SHADOW；更换 read context 的 observation key 在适配器边界拒绝。

## 8. 历史冻结证据的版本化兼容

实施前原测试实际得到 **64 passed、1 failed**。TC-15 将当前被忽略的生产 JSON 与上次记录的固定哈希比较，
五个文件在任务之间已有变化；会话前后检查一致。本次不还原、修改或解释为测试写入。
旧 TC-16 在原树通过，但原七路径 allowlist 必然排斥后续授权模块；这是任务范围快照，不能永久冻结仓库。

本次保留四份原契约文档的原始内容、原 7c8e28f 起点、4f4da993 冻结提交、247 文件范围、七路径例外、
原摘要和生产数据历史哈希。tests/test_production_trust_contracts.py 用只读 git archive 读取精确
4f4da993 提交并验证原规范化路径/内容摘要，保留全部原契约对抗校验。
CI 两个 checkout 只增加 fetch-depth:0，让 pytest 之前已有历史；测试本身不联网、不 fetch、不提取文件到磁盘。
历史缺失必须失败，不跳过，不改用当前文件或任意 ref。

TC-15 保留历史哈希作为原始记录，而不声称 Git 保存了被忽略的旧生产字节。
活跃检查比较当前运行前后七库原始 SHA-256；新增七个针对临时副本的破坏测试逐项证明写入仍会被检出。
本次生产初始/最终原始哈希另记录在下面和最终报告中。
新实现以 offline-root-pilot-v1 的八文件 allowlist 和精确基线树检查当前改动，
替代旧历史规则对当前树的约束。原历史证据不会为让 CI 通过而删除或重签。

旧 owner 全未知的断言现在只验证旧历史快照。本次实际生产 owner 仍未知且不能被 synthetic actor 晋级；
未来真实 ownership 需另一个经授权的证据版本，以实际接受记录、独立批准及部署证据驱动状态迁移，
不要求组织永远未知，也不在本次新增通用 owner/governance 框架。

## 9. 可执行验收与证据范围

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_trust_root_bootstrap.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_production_trust_contracts.py
.\.venv\Scripts\python.exe -m tools.trust_root_bootstrap_validation
```

RB-07–28 是实际根 pin、schema、scope、purpose、role、transition、approval、revocation、
version、continuity 和 override 对抗用例；RB-29–32 实际调用原 time、owner、revocation 与完整严格 Reader。
额外覆盖 malformed key、重复 authority、错误指纹、错误/缺失角色、角色共享、错误 signer、
transition 重新签名后仍错 scope/目标、退休丢失、根校验间公钥替换和读取中根代际切换。
RB-33 验证本次改动范围与生产 import closure；RB-34 比较实际七库；
RB-35 的 pytest 仅检查双平台 CI 配置，**真实四作业及整体 workflow 成功须在最终提交后独立查询**。
不把静态配置测试当作远程运行结果。

功能入口在系统临时目录创建全部凭据，输出确定的 JSON schema/场景顺序和实际拒绝码；
不输出随机 key 指纹、私钥或临时目录。任何失败以非零退出，不使用静态成功表。
精确本地专项、完整回归、最终 SHA 的 CI 与 A–U 门禁结论均在最终交付报告记录。

## 10. 实施前登记与本次数据基线

下面保存实施前实际结果与本次七库原始哈希；它是审计证据，不是应用配置或新的生产授权源。

```json
{
  "schema_version": "1.0.0",
  "scope_version": "offline-root-pilot-v1",
  "baseline": "4f4da993a27dc0c5362f38bc228b463ba364999b",
  "historical_contract_revision": "4f4da993a27dc0c5362f38bc228b463ba364999b",
  "historical_original_scope_paths": 7,
  "historical_frozen_files": 247,
  "initial_contract_test": {
    "passed": 64,
    "failed": 1,
    "failure": "TC-15 historical production hash differs between tasks"
  },
  "baseline_results": {
    "RB-01": {
      "caller_selected_key_verifies": true
    },
    "RB-02": {
      "replacing_artifact_and_key_verifies": true,
      "existing_fixed_key_rejects": [
        "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED"
      ]
    },
    "RB-03": {
      "local_key_loads": true,
      "production_registration_established": false,
      "loader_fields": [
        "key",
        "public_key_id",
        "raw"
      ]
    },
    "RB-04": {
      "older_signed_payload_still_valid": true,
      "no_root_bundle_api_at_baseline": true
    },
    "RB-05": {
      "session_rollback_rejects": [
        "TRUSTED_TIME_GENERATION_ROLLBACK"
      ],
      "old_challenge_rejects": [
        "TRUSTED_TIME_SESSION_MISMATCH"
      ],
      "fresh_challenge_new_signature_generation_one_accepted": true,
      "persistent_continuity_established": false
    },
    "RB-06": {
      "wrong_purpose_rejects": [
        "TRUSTED_ARTIFACT_TYPE_MISMATCH"
      ]
    }
  },
  "seven_store_sha256_before": {
    "daily_matches.json": "245b3c46f4805e88fa2f75c4caaac949c0e20ef782b045c309bca9ed12c17c60",
    "team_strength.json": "8c3897c34001ba2453917fe93dae6d5a2f24f46464ef5841128fd2f8b9b7afd6",
    "calibrated.json": "512785b1da9853eb02384ed1a827daf2f713b0304b7179d03bfa2abe12ccbcd0",
    "prediction_snapshots.json": "45228130ff4eab87524e4bf601e725274bf52f44d3b78a11c6687471065128d4",
    "odds_snapshots.json": "9e260c73ea7ecbca5c6a2f7628803949b636231552048e4fb8905c9432bd518b",
    "settlements.json": "d1d4b0e2504f727d4364c46175e19d29e39066286422aa28298624459c1539f8",
    "evaluation_rows.json": "b114c1b1e0f0fbb6c297f1127849b7bed1ac886d0fd0631b8a7b441afb7398bd"
  },
  "production_activation_ready": false,
  "decision": "NO-GO"
}
```

## 11. 生产阻断与停止线

带外生产根与独立 approver 的真实身份/注册、组织职责接受、KMS/HSM、独立根分发/轮换/吊销、
可信当前时间、跨重启持久连续性、完整权威 Canonical/治理来源、独立 checkpoint/audit 服务、
实际平台耐久性、监控/事故响应与不可绕过的生产部署策略均未建立。
原生产契约中的 BT-ROOT/OWNER/CONTINUITY/DURABILITY/POLICY/DEPLOYMENT/REVIEW 均未生产关闭。

本次仅增加可执行的合成引导与适配验证证据，不授权生产 root、生产 Reader 或 mapping writes。
停止于试验实现、负向测试、严格兼容、功能演示、全量回归、前向提交、最终 CI 与报告。
**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO。**
