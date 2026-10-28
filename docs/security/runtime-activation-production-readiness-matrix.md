# 运行期激活：生产信任来源与部署就绪矩阵

## 1. 判定口径

审计基线：`3fc6683081560936dc0ea6e7650155e8cb4123ee`，`develop`。
**SHADOW_READER_IMPLEMENTED / PRODUCTION_READER_NOT_ENABLED**。
**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO**。

`COMPONENT_IMPLEMENTED` 表示存在代码；`COMPONENT_VERIFIED` 表示组件测试实际验证；
`INTEGRATION_VERIFIED` 只用于完整合成链路通过的边界，不能推导生产权威存在。
本表所有生产来源的运维责任人均 **NOT_ESTABLISHED**，生产信任根来源均 **NOT_ESTABLISHED**，
部署状态均 **NOT_ESTABLISHED**，生产安全验证均 **NOT_VERIFIED**。
这是仓库及本次证据的结论，不声称已调查仓库以外的组织基础设施。

合成密钥由临时夹具在系统临时目录显式生成；公钥通过构造参数或 read context 提供。
这些是可验证的**测试信任根**，不是已建立的生产根。

## 2. 十八项来源、责任和最小验收

| 编号 / 权威 | 仓库实现位置 / 已存在能力 | 生产来源 / 证据来源 | 必须建立的控制与剩余阻断 | 最小功能验收 |
| :--- | :--- | :--- | :--- | :--- |
| A 治理账本 | `mapping_governance.GovernanceLedger`、`authoritative_governance.build_authoritative_governance_view`；一致历史、签名检查点、批准审计 | **NOT_ESTABLISHED**；只有临时账本证据 | 固定账本根、只读服务、权限、保留、独立检查点、当前状态分发 | 真实环境推进批准后，旧视图不可消费；截断和错根拒绝 |
| B Canonical 所有权 | 签名 `CANONICAL_BASELINE_MANIFEST`、视图 Canonical/active_bindings | **NOT_ESTABLISHED**；合成 Canonical 清单 | 受控基线计算、完整所有权来源、与生产当前数据绑定、独立发布 | 错数据集/基线/未知或冲突 owner/空 Canonical 集合均拒绝 |
| C 认证观测 | `observation_authority`、作用域键 v3 | **NOT_ESTABLISHED**；合成 observation 角色 | 认证采集边界、原始证据保留、adapter/sport/date/provider scope、根分发 | 非采集角色签名、变更 scope、过期证明拒绝；受控真实观测可验 |
| D owner key 管理 | Ed25519 approval、历史批准审计 | **NOT_ESTABLISHED**；临时 owner keypair | 身份注册、最小权限、离线/硬件保护、轮换、吊销、主体分离 | 未注册 owner、旧吊销 key、重放批准拒绝；原历史仍可审计 |
| E activation 签发 | `StrictActivationVerifier`、批准绑定 body builder | **NOT_ESTABLISHED**；临时 authorizer | 独立签发服务、用途/范围策略、审批、审计、禁止自动签发 | 只有当前认证批准可签发；错误指纹和超寿命授权拒绝 |
| F 可信时间 | `TrustedTimeVerifier`；独立 freshness callback | **NOT_ESTABLISHED**；独立推进的测试时钟 | 认证时间提供者、独立 reference、失效策略、生产寿命策略批准 | token 重放不能冻结时间；过期/回拨/断服务均拒绝 |
| G 时间持久高水位 | 会话内 generation/challenge/高水位 | **NOT_ESTABLISHED**；没有生产持久化实现 | 受保护持久连续性、重启取新鲜当前证明、持久化失败停用 | 回滚持久状态/重启重放旧 token 不可消费 |
| H supersession | `ActivationSupersessionVerifier`、显式 A→B 和完整 inventory | **NOT_ESTABLISHED**；临时 lifecycle/checkpoint | 独立生命周期注册表、完整历史、高水位、发布策略 | A→B 后 A 永不因 B 失效而复活；跨重启旧检查点拒绝 |
| I 独立撤销 / 全局当前性 | `IndependentRevocationVerifier`；完整状态与独立检查点 | **NOT_ESTABLISHED**；临时撤销与 checkpoint 角色 | 真实撤销服务、全局状态、高水位、事件保留、可用性/SLA | B 撤销后所有消费者拒绝；重放空状态和缺事件状态拒绝 |
| J 持久快照发布 | `PersistentShadowSnapshotStore`；认证命令、不可变快照 | **NOT_ESTABLISHED**；临时本地文件存储 | 独立 publication 服务、ACL、耐久性、备份、生产目录策略 | 发布不选择；错误发布签名/不完整证据/断电未提交文件拒绝 |
| K 受控 selector | `PersistentShadowSnapshotController.select`；签名 CAS | **NOT_ESTABLISHED**；临时 selector | 操作者身份、权限、显式变更、双人控制策略、可追溯性 | 未认证身份无权选择；竞争 CAS 唯一提交；直接指针修改拒绝 |
| L control checkpoint | 精确 journal/audit/pointer/inventory/challenge 检查点 | **NOT_ESTABLISHED**；临时独立 control key | 独立观察、签发者持久高水位、当前性服务、根配置 | 新提交后旧 checkpoint 拒绝；重启不得仅给旧状态重签 |
| M audit checkpoint | `ShadowActivationAuditStore.export_checkpoint/verify_external_checkpoint_snapshot` | **NOT_ESTABLISHED**；临时 checkpoint key | 独立审计基础设施、不可变保留、根分发、外部检查点 | 截断、追加、重写、跨存储证据均拒绝，不能自证最新 |
| N recovery | `recovery_authority`、disabled reconciliation、`re_enable` | **NOT_ESTABLISHED**；临时 recovery signer | 恢复授权服务、独立对账、事件/指针对齐、显式再启用 | 恢复提交保持停用；跨 store/旧证据/撤销/替代/指针失败拒绝 |
| O KMS / 轮换 / key revocation | 仓库外私钥路径守卫、构造时固定部分公钥身份 | **NOT_ESTABLISHED**；只有文件 keypair 接口 | KMS/HSM、密钥生命周期、信任根发布、分角色访问和失陷处置 | 轮换保留历史可审计；吊销生效；未授权服务不能签名 |
| P 崩溃耐久性 | flush/fsync、有序日志/审计/指针、故障和真实进程退出测试 | **NOT_VERIFIED**；本机合作进程证据，不是硬件断电证据 | 生产存储契约、断电测试、备份恢复、平台目录同步差异评估 | 每个提交边界断电重启，只读或受控恢复；绝不自动消费 |
| Q 监控 / 事件响应 | 显式 disable、拒绝码、结构化合成报告 | **NOT_ESTABLISHED**；无部署监控证据 | 告警、责任轮值、运行手册、审计保留、停用演练、SLO | 撤销或 checkpoint 服务异常触发告警并停用；恢复需当前证据 |
| R 部署策略强制 | 三个常量关闭、生产入口无 shadow 导入；严格模式 opt-in | **NOT_ESTABLISHED**；静态源码及集成测试 | 生产不可绕过的构造策略、配置准入、发布审批、独立安全复评 | 无法以 legacy/non-strict 模式绕过；默认生产仍关闭 |

## 3. 调用方仍提供的权威输入

| 接口 | 仍由调用方提供 / 配置的输入 | 实现能证明什么 | 生产缺口 |
| :--- | :--- | :--- | :--- |
| `build_authoritative_governance_view` | ledger path、dataset、current baseline、owner/ledger/Canonical/revocation/view 公钥、签名清单、view 私钥、当前时间及 freshness | 在所给根与时间下验证历史并派生视图 | 不能证明路径、根、Canonical 清单或时钟就是生产权威 |
| `RuntimeMappingContext` | 观测、批准、proposal、记录状态、provenance、Canonical/所有权集合、view 公钥、评估时间 | 严格视图与事实逐项一致，真实 owner 签名可验 | provenance 字符串不能自证；缺生产来源 |
| `ActivationVerificationContext` | dataset/baseline、activation 公钥、decision/proposal、legacy 时间/撤销集合 | 既有签名与绑定验证 | legacy 检视不可变成生产权限 |
| `TrustedTimeVerifier` | authority identity、公钥、独立 reference callback、窗口 | 会话 challenge、签名、新鲜度、单调性 | callback 本身必须独立可信；无生产服务或持久高水位 |
| `StrictActivationVerifier` | time verifier、governance 根、最大寿命/视图窗口 | 固定根、批准与授权绑定、会话连续性 | 24h 是合成上限，不是生产风险政策批准 |
| lifecycle / revocation verifier | authority identity、owner/activation/lifecycle/view/time/checkpoint 公钥、完整原始证据 | 固定角色分离与精确 inventory、单调历史 | 签名合成“当前状态”不证明生产全局最新 |
| controller / store | 隔离 workspace、registry、audit 对象、publisher/selector/checkpoint 公钥、请求与检查点 | 精确提交与选择证据，受保护目录、有界合作锁 | 同进程夹具不等于独立运维服务；初始根仍来自配置 |
| `ShadowReadContext` | dataset/baseline、activation/view/observation/recovery 公钥、各 freshness 窗口、原始证据 | 每次消费重新验证；严格分支不信任 caller 空撤销表 | observation/recovery 公钥仍可逐调用传入；须由生产固定配置封装，不能让不可信调用者替换整组根与证据 |
| recovery builder | audit 对象/检查点公钥、recovery 私钥、选择/有效指针、时间 | 当前审计副本与证据绑定；Reader 再独立比对实际状态 | 不能凭一个所给公钥宣布恢复签名者是正确生产根 |

> **公钥可验不等于公钥正确；新签名不等于全局当前。**
> 所有组件在一个 Python 进程的不同对象内执行，角色密钥分离是逻辑分离，
> 没有宣称物理独立、跨主机线性一致性或能够抵御进程内任意代码执行。

## 4. B-01 至 B-14 的生产结论

| 阻断项 | 隔离组件与验收范围 | 生产状态 |
| :--- | :--- | :--- |
| B-01 | 已实现账本、签名检查点、历史批准到治理视图；完整链路验证 | **PRODUCTION_TRUST_SOURCE_MISSING / PRODUCTION_CONTROL_NOT_READY** |
| B-02 | 已实现签名所有权/Canonical/撤销事实及消费复核 | 同上；根与完整生产来源未建立 |
| B-03 | 已实现观测签名及 scope 验证；独立合成来源角色参与集成 | 同上；生产采集权威未建立 |
| B-04 | 已实现外部审计检查点、精确当前副本与恢复绑定 | 同上；独立审计服务未建立 |
| B-05 | 已实现完整严格恢复和显式 guarded re-enable | **PRODUCTION_CONTROL_NOT_READY**；部署强制尚未建立 |
| B-06 | 已实现可信时间、寿命、会话连续性 | 同上；生产时间与持久高水位未建立 |
| B-07 | 已实现 owner→decision→activation 认证批准指纹绑定 | 同上；生产 owner/activation 管理未建立 |
| B-08 | strict 签名视图要求非空 Canonical 且核对唯一所有权；legacy 仍仅检视 | 同上；真实完整所有权源及强制策略未建立 |
| B-09 | 已实现持久发布、签名选择、检查点、CAS | 同上；独立发布/选择运维服务未建立 |
| B-10 | 已实现 UNCERTAIN、差异检测、保留字节及受限恢复 | 同上；生产存储与断电耐久性未验证 |
| B-11 | 已实现有界合作锁、一致读副本；真实进程与双平台 CI 验证 | 同上；多主机/网络文件系统不在保证内；原间歇 Windows 根因仍未复现 |
| B-12 | 本次文档明确 SHADOW_READER_IMPLEMENTED 与 PRODUCTION_READER_NOT_ENABLED | 文档改进不关闭部署控制；旧文档的历史叙述不能当作最新架构 |
| B-13 | 已实现显式 supersession、完整历史与单调检查点 | **PRODUCTION_TRUST_SOURCE_MISSING / PRODUCTION_CONTROL_NOT_READY** |
| B-14 | 已实现独立撤销状态、检查点、重放拒绝与消费再验证 | 同上；全局当前撤销权威未建立 |

## 5. 优先依赖顺序

1. 确定生产责任人与威胁模型，建立 dataset/Canonical 所有权和治理账本的真实权威来源。
2. 建立 KMS、分角色身份、公钥发布/轮换/吊销、认证采集、owner 和 activation 签发边界。
3. 部署可信时间、持久连续性、生命周期和撤销权威；独立检查点签发方必须维护真实高水位。
4. 验证存储耐久性，部署独立发布、选择、审计、对账与恢复基础设施。
5. 建立监控/事件响应和不可绕过的严格部署政策，执行真实环境故障及跨平台验收，再独立安全复评。

本次仅登记依赖，**没有执行上述生产部署，也没有授权实施下一项工作**。
