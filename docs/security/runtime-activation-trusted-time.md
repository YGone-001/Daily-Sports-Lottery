# 可信时间与激活授权寿命：隔离原型

**基线**：`fb4c681b0467f11a4a58cc71a049021ddd64b6a9`（fetch 后最新 develop；用户已授权从最新提交开始）。
**范围**：B-06 的可执行隔离实现、shadow 读取与恢复集成。生产激活与生产读取器仍为 `NOT_ENABLED`。

## 1. 基线复现

在修改既有授权验证路径之前运行六项基线测试，结果为 **6 passed**。首次尝试因系统 Python
缺少已声明的 cryptography 依赖发生六项 setup error；随后在仓库 `.venv` 安装
requirements.txt / requirements-dev.txt 后重跑。错误未被计为功能证据。

| 编号 | 实际基线行为 | 可执行证据 |
| :--- | :--- | :--- |
| TE-01 | 同一短期授权在调用方时间位于窗口内时通过，在到期时拒绝 | `test_te01_legacy_depends_on_caller_time` |
| TE-02 | 旧验证器接受一个月的授权窗口 | `test_te02_legacy_accepts_excessive_lifetime` |
| TE-03 | 先观察过期，再传入早期时间，旧接口重新通过 | `test_te03_legacy_earlier_time_can_revive_expired_authorization` |
| TE-04 | 旧接口没有独立 freshness 输入或签名时间证据要求，不能执行可信时间证据过期验证 | `test_te04_legacy_has_no_independent_freshness_input` |
| TE-05 | 有效授权签名与调用方时间能通过旧接口，不能据此宣称存在可信时间验证 | `test_te05_legacy_valid_signature_is_not_authenticated_current_time` |

TE-03～05 的原基线没有可信时间契约；没有伪造旧接口支持这些能力。严格模式对应行为由 TT 测试验证。

## 2. 时间证据与信任锚

`utils/trusted_time_authority.py` 复用 SignedTrustedArtifact、sign_trusted_artifact、
verify_trusted_artifact 与既有 Ed25519 后端。签名覆盖外层 schema、artifact_type、
signer_public_key_id 及完整 payload；外层不携带可自证权威的公钥。

| 位置 | 字段 / 约束 |
| :--- | :--- |
| 外层 | `schema_version=1.0.0`、`artifact_type=RUNTIME_ACTIVATION_TIME_ATTESTATION`、`signer_public_key_id`、Ed25519 `signature` |
| 载荷契约 | `time_schema_version=1.0.0`、同名 `purpose` |
| 身份 | `dataset_identity`、`authority_identity`、`evidence_id` |
| 时间 | `attested_at`、`issued_at`、`expires_at`；UTC RFC3339，必须显式使用 Z 或 +00:00 |
| 连续性 | 非负 63 位 `generation`、当前验证会话的 `session_challenge` |

本原型 profile 要求 attested_at 等于 issued_at，expires_at 严格晚于 issued_at，
有效区间不超过 verifier 固定的新鲜度窗口。默认窗口 **60 秒**，构造上限 **300 秒**。
这不是生产时间服务协议的批准。

公钥路径、数据集、authority 身份、reference provider、窗口与回拨容差在 verifier 构造时固定。
初始公钥身份另行固定，替换公钥文件不能轮换这个会话的信任锚。
序列化回归修正了通用工件 from_dict 原先错误要求内部 payload_json 的问题；实际 wire 字段为 payload。

## 3. 独立新鲜度与连续性

**签名时间戳不证明当前时间。** TrustedTimeVerifier 必须安装单独受信任的
`freshness_reference: Callable[[], datetime]`。没有默认系统时钟，也不接收 read 调用方的
current_time 来建立信任；没有 reference、reference 不可用、非 UTC 或回拨都拒绝。
测试 provider 为单独推进的 SyntheticReferenceClock，绝不从证据反向设置它。

时间验证依次检查外层签名、用途/schema、数据集、authority、session、有效区间、
generation 和时间高水位。有效消费时间随独立 reference 经过的时间推进，且不得低于已接受高水位。
相同 token 仅可在 freshness 范围内重复验证，不能冻结授权时间。

每个会话保留最后接受的时间、reference、generation、证据指纹及最多 **1024** 个证据身份。
降低 generation、同 generation 的矛盾证据、跨 generation 重用 evidence_id 均拒绝；
容量耗尽要求新会话，不能无界增长。默认回拨容差 0 秒，显式配置上限 5 秒；
容差内接受较早的 attested 时间也不能降低实际消费时间。锁串行化会话检查与高水位更新。

**重启边界**：没有生产持久高水位。新 verifier 自动生成不可预测的新 challenge，
必须重新取得 authority 为该 challenge 签发的当前证据；旧会话 token 即使尚未过期也不能初始化新会话。
已建立会话丢失状态或独立 reference 回拨会 fail closed。该设计不是断电后的生产防回拨基础设施。

## 4. 严格授权验证与 shadow 集成

StrictActivationVerifier 是既有 verify_runtime_activation_authorization 的窄包装，
没有新增映射资格或授权政策引擎。它先验证可信时间，替换验证上下文时间，再运行既有签名、
提案/资格指纹、数据集、Canonical 基线、用途、版本、撤销与生效/到期检查。
时间失败时有效时间为空、结果拒绝，绝不回退到调用方 current_time。
严格包装同时固定激活用途与 schema，调用方不能通过改 expected_purpose/schema 重定义签名域。

版本化寿命策略为 `synthetic-activation-lifetime-v1`，默认最大 **86400 秒（24 小时）**。
构造时可选择更短的明确上限，但不能超过 24 小时，不能在 verify 调用中覆盖，不能修改已建立 verifier。
零/负寿命拒绝 `ACTIVATION_LIFETIME_INVALID`，超上限拒绝 `ACTIVATION_LIFETIME_EXCEEDED`，
非严格 UTC 时间拒绝 `MALFORMED_TIMESTAMP`。恰好 24 小时可通过寿命门；生效边界包含起点、排除到期点。
这仅为合成策略，没有新增风险身份分类，也没有批准生产寿命。

严格结果分别报告 `signature_valid`、`trusted_time_verified`、`is_consumable` 与证据指纹，
因此真实签名但过期的授权不会被报告为可消费。验证器不会自动重签授权。

ShadowRuntimeReader 显式设置 `require_trusted_time=True` 并安装固定 StrictActivationVerifier，
每次 read、re_enable 和 simulator 回滚都重新验证时间。治理视图、观测证明、恢复证明与授权验证
始终使用认证后的时间。缓存键仅用于诊断，结果缓存不能跳过当前检查。
开启可信时间会强制保留恢复证据前置，不能通过漏传旧 recovery 开关绕过严格再启用契约。
旧的 isolated 接口、授权 schema、policy/evaluator 版本及默认行为保留。

## 5. 真实审计恢复缺陷与最小修正

基线使用实际 simulator 与 audit store 执行停用，确认写入 SHADOW_RUNTIME_DISABLED。
随后普通回滚首先失败于 **ROLLBACK_READER_DISABLED**；无法生成当前完成状态。
旧测试中用 audit_store=None 停用以维持 COMPLETED 链尖的捷径未用于本次完整生命周期。

最小修正增加单独显式请求的 `reconcile_disabled_reader=True`：

1. 强制验证外部审计检查点，不能无审计运行。
2. 要求 reader 当前停用；普通回滚仍然拒绝停用 reader。
3. 按当前上下文验证目标与授权；启用可信时间的 reader 会执行严格时间和寿命验证。
4. 按既有收据/回读机制持久提交 REQUESTED、COMPLETED，再选择恢复目标。
5. reader 与旧句柄持续不可消费；只有携带当前恢复证据的 re_enable 才恢复消费。

这是一项显式的隔离对账操作，不构成生产恢复授权 API、生产角色体系或自动再启用。
合成演示全程启用已验证治理、签名观测、可信时间、外部审计检查点、恢复前置。
测试验证恢复后读成功，历史句柄失效，旧恢复证据/旧快照/撤销/过期不能恢复消费。

## 6. 可执行验收与安全矩阵

```powershell
.\.venv\Scripts\python.exe -m tools.shadow_trusted_time_validation
.\.venv\Scripts\python.exe -m pytest -q tests/test_trusted_time_authority.py
.\.venv\Scripts\python.exe -m pytest -q
```

CLI 在 TemporaryDirectory 内生成全部一次性密钥、治理/审计夹具，退出时清理。
输出确定性 JSON，11 项断言覆盖任务指定的十种演示结果与七存储完整性；任何失败返回非零。
在 python -O 下仍执行生命周期不变量检查。没有网络抓取、生产密钥或生产写入。
CI 的干净 checkout 没有受忽略的生产 JSON 时，七存储检查记录 absent 并要求仍然 absent。

| 编号 | 新测试文件中的可执行覆盖 |
| :--- | :--- |
| TT-01 | 有效签名、不可变证据、序列化 roundtrip |
| TT-02～05 | 未知签名者、跨用途、跨数据集/authority、篡改 |
| TT-06～09 | UTC 格式/时区、未来、陈旧、恰好证据过期 |
| TT-10～12 | generation 回退、时间回退、相同序号矛盾 |
| TT-13～14 | 缺证据/reference/verifier、current_time 无法覆盖 |
| TT-15～18 | 固定寿命策略、精确上限、超限、零/负寿命 |
| TT-19～22 | 生效前、生效点、到期点、到期后 |
| TT-23 | 当前签名治理视图中的撤销仍有权威性 |
| TT-24～25 | 严格读取通过、陈旧 token 不能复用缓存 |
| TT-26～29 | 完整真实审计生命周期、回拨再启用拒绝、重启/丢状态拒绝 |
| TT-30～31 | 生产入口无新导入、标志关闭、七存储不变、功能演示 |
| TT-32 | 原 Ubuntu/Windows Python 3.12 四 job 矩阵保留；实际远端结果另见最终报告 |

额外覆盖会话容量、跨 generation 重用证据身份、reference 回拨后的关闭状态、容差不降低消费时间、
同 token 到期推进、缺失检查点、再启用缺失/过期/超寿命/旧恢复/旧快照，以及演示失败退出。
未增加 skip 或 xfail，未削弱 AC/GC/RC/RE 断言。

### 本地执行证据

本地环境为 Windows、Python 3.12.10、仓库 `.venv`。最终安全测试文件 **73 passed**；
全套回归在最终源码上执行到结束为 **1632 passed**。指定测试文件结果如下（合计 371）：

| 测试文件 | passed |
| :--- | ---: |
| test_trusted_time_authority.py | 73 |
| test_runtime_activation_authorization.py | 29 |
| test_runtime_activation_recovery_binding.py | 16 |
| test_runtime_activation_trust_hardening.py | 29 |
| test_shadow_runtime_reader.py | 20 |
| test_shadow_activation_simulation.py | 18 |
| test_shadow_activation_audit_store.py | 12 |
| test_shadow_runtime_authority_corrections.py | 50 |
| test_runtime_mapping_readiness.py | 59 |
| test_runtime_activation_security_review.py | 7 |
| test_canonical_mapping_governance.py | 45 |
| test_concurrency.py | 13 |

七个本机生产存储实施前后 SHA-256 全部一致（CI 干净 checkout 中这些文件不受 Git 跟踪）：

| 存储 | 实施前 = 实施后 SHA-256 |
| :--- | :--- |
| daily_matches.json | `70ca32e2bffde614081c1d83a7056f863008006d3602ee2012452db2384eee8b` |
| team_strength.json | `9a9cb2a7c10aec57412423c65847afef4cefa51e54b03ca55421a3b33c9dfe22` |
| calibrated.json | `b5bd53a9476c5ab5f2c9480cc652ecb4a12f3ade8903a3985a97aa8f44c42c0d` |
| prediction_snapshots.json | `24d44b3a439f7d47ebf1ed525aac8387c9ec35d6a5524f779a283e18e3b73fa2` |
| odds_snapshots.json | `1d7ac6d9edee0997d6413fc4c8c86ba8addd7530dc88b6b887956edfb76b422b` |
| settlements.json | `bf29628eba8ef662d21e94f63ed2c2482ca572ba54ad1f4769ef7314425e7280` |
| evaluation_rows.json | `6ccbf7a2870199e8f06e0da1c55f537df6bf77f4af1266a0df9351ead8571ef0` |

前一基线 CI run 39820611618 已核实为 completed / success，四个 job 均 success；
本次提交 SHA 与对应远端 CI 的实时结论在最终交付报告中记录，不能用本地结果替代。

## 7. 生产边界

| 层次 | 本次结论 |
| :--- | :--- |
| synthetic signed time | 已实现并可重复运行 |
| 独立公钥验证与明确独立 reference | 已有隔离验证契约；可信性依赖外部配置的 provider |
| 进程内单调连续性 | 有界状态与锁保护；不等同于持久权威 |
| 生产时间权威 | 未部署；无 NTP、生产服务、生产高水位或生产密钥 |
| 生产激活许可 | 未授予 |

**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO。**
本次仅补齐 B-06 的隔离组件。生产时间 reference 的认证/运维、持久连续性与其余生产阻断项仍需独立评审。
没有实现 B-07，没有修改刷新、模型、Elo、Canonical、历史记录或结算。
