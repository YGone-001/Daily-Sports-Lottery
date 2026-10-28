# 隔离 Shadow 快照持久发布与受控选择

## 1. 范围与结论

本组件以 `a920129606efcc444bd385a1f046f334e9ef7300` 为起点，实现隔离临时工作区的持久快照、
签名发布、签名选择、控制日志、独立检查点及严格 Reader。**生产激活仍为 NO-GO**。
生产 `data/`、Canonical 身份、采集入口、模型数学、历史结算和前端均不参与该控制面。

| 属性 | 所需证据 | 单独成立时的权限 |
| :--- | :--- | :--- |
| 文件存在 | 可读取文件 | 无发布、选择或消费权限 |
| 已认证发布 | publisher 签名、完整工件、当前授权、发布提交 | 保留快照，不改变选择 |
| 选择意图 | selector 独立签名、前态 CAS | 允许尝试提交，不证明提交成功 |
| 持久提交 | 精确审计回执、读回、控制提交事件 | 记录状态转换，还需有效指针和检查点 |
| 当前有效选择 | 日志、审计、指针、独立当前检查点、registry 一致 | 继续验证消费条件 |
| Shadow 消费 | 当前批准、激活、治理、生命周期、撤销、时间、观测认证 | 仅隔离 Shadow 解析 |
| 生产权限 | 本组件没有此权限 | **NOT_GRANTED / NOT_ENABLED** |

## 2. 精确基线复现

复现器在实现前运行；另通过 `git archive` 导出精确起始 SHA，仅复制复现器后再次运行。
导出目录不含被 Git 忽略的生产 JSON；七库完整性单独在实际工作区检查。

| 编号 | 实际基线结果 | 解释 |
| :--- | :--- | :--- |
| PP-01 | 无签名选择成功 | legacy registry 无选择认证 |
| PP-02 | 新 registry 数量 0，选择 null | 注册/选择均无跨重启持久性 |
| PP-03 | 指针改变，审计不变 | 直接内存选择无持久审计 |
| PP-04 | `RESOLVED_SHADOW` | 旧严格 Reader 未要求独立选择权威 |
| PP-05 | `ROLLBACK_AUDIT_FAILED / UNCERTAIN`，Reader 停用、指针 null、审计前缀保留 | **已有保护，不宣称为新缺陷** |
| PP-06 | 三份外部签名可验、旧 Reader 可解析，但无 durable controller | 不把缺少此 API 写成受控选择成功 |

## 3. 存储格式与文件系统边界

`PersistentShadowSnapshotStore` schema `1.0.0`：`snapshots/<64位指纹>.json` 顶层严格包含
`schema_version / snapshot / publication / evidence / published_at`，编码为规范 JSON 加单个 LF。
快照保持原结构，ID、声明指纹、重算内容指纹必须一致。字符串、binding、授权字段不进行容错类型转换。
未知字段/schema、非规范编码、截断、缺少引用证据均拒绝。证据保留原授权、资格决定、提案、治理证明、
生命周期/撤销状态与事件/检查点、可信时间工件；不保存私钥。

同 ID、相同内容再次发布保留首份文件字节；原命令重试也须通过当前检查点和当前安全验证，才返回
`IDEMPOTENT`。同 request ID 不同内容、同 ID 不同快照内容拒绝。无控制提交的文件不属于逻辑发布集合。
`.pending-*` 不会自动采纳或删除。

工作区必须预先存在且为绝对路径，复用生产路径守卫，排除仓库、生产目录、`..`、ADS、符号链接、
NTFS junction/reparse point、文件硬链接别名；不默认写入 cwd。使用 O_EXCL 临时文件、flush/fsync、
不可覆盖的 hard-link 发布或原子 replace。失败保留证据并拒绝消费。

> 边界假设隔离目录由可信本机账户持有，参与进程遵守合作锁。
> 不宣称抵御拥有目录替换权限的敌对账户在任意系统调用间隙发动的所有 TOCTOU 攻击。

## 4. 签名命令与角色

| 用途域 | 角色 | 绑定内容 |
| :--- | :--- | :--- |
| `SHADOW_SNAPSHOT_PUBLICATION_REQUEST` | `snapshot-publisher` | 数据集、ID/指纹、基线、策略/评估器、激活 ID/指纹、当前生命周期/撤销/治理指纹、actor、request ID、原因、签发/过期时间 |
| `SHADOW_SNAPSHOT_SELECTION_REQUEST` | `snapshot-selector` | 同上，另含 `SELECT / ROLLBACK / RECOVER` 和预期前指针、generation、audit tip/count、selection sequence |
| `SHADOW_SNAPSHOT_CONTROL_CHECKPOINT` | `control-checkpoint` | store/journal/audit 身份、日志 tip/sequence、generation、inventory、选择 ID/指纹、审计 tip/count、指针字节指纹、差异、会话 challenge、时间窗口 |

三个控制角色公钥彼此独立，且不能与 owner、activation、lifecycle、revocation、governance、time 和已有检查点根互换。
构造时固定根指纹，验证时再核对公钥内容，禁止嵌入公钥自认证。复用既有 `SignedTrustedArtifact` Ed25519。
命令字段集合严格相等，额外字段拒绝。默认窗口 60 秒、构造上限 300 秒；当前时间来自既有可信时间验证器。
核心模块不签名，合成工具仅在系统临时目录显式生成各角色密钥。

## 5. 日志、审计与精确提交点

控制日志 schema `1.0.0` 为严格规范 JSONL，完整验证既有历史后追加。事件包含 event ID、sequence、
previous hash、entry hash、完整签名命令/指纹、request ID、快照、前/有效指针、actor/role、认证时间、
outcome、审计 ID/回执、generation、reconciles。重复身份、序号缺口、损坏尾部拒绝，不截尾、不重写。
事件覆盖发布 requested/committed/rejected、选择 requested/committed/rejected/uncertain、disable、recovery verified。
已认证但 CAS 失败的选择追加 REJECTED；无效签名在写日志前拒绝。

既有 `ShadowActivationAuditStore` schema/契约不变，新控制面显式注入有界锁。
对应 `SHADOW_SNAPSHOT_REGISTERED / SELECTED / SWITCHED`；逐项匹配命令指纹、request ID、数据集、
快照指纹、前/有效指针、actor/role、认证时间。**发布审计的有效指针保持原值**。
只接受真实 record、真实 `persisted=True` receipt，并验证身份、序号、哈希、全链和读回。
None、错回执、无关事件、追加竞争改变已签名前态都不能证明提交。

1. 验证命令、当前批准/激活/治理/生命周期/撤销、可信时间和独立检查点。
2. CAS 比较前选择、generation、audit tip/count、selection sequence，检查 replay。
3. 持久 REQUESTED；发布另写不可变快照。
4. 持久审计事件，验证精确 receipt、全链、读回。
5. **持久 COMMITTED / RECOVERY_VERIFIED 是控制转换提交点**，generation 加一。
6. 原子写指针，更新 registry，再核验一致性；旧指针另存证据。
7. 消费必须提供独立签名的**提交后当前检查点**。

**这是多存储有序协议，不是原子多文件事务。** 审计已落盘而控制未提交、控制已提交而指针未应用均可能发生。
返回的 committed 标志仅表示已核验确认，不能从异常猜测物理写入。审计可能变化、读回不可用或控制提交后失败
分类 `UNCERTAIN`；仅确认无审计/控制提交才为 `NOT_COMMITTED`。持久化失败先停用 Reader、清除内存指针，
保留原字节，不回滚审计，不重写日志。

## 6. 检查点、重启与恢复

本地哈希链不是独立信任锚。检查点由外部独立角色显式签发，逐字段匹配实际文件、逻辑发布集合、
日志、审计、指针和会话 challenge；验证器保持会话内单调高水位。旧 tip/generation、截断、历史替换、
陈旧时间、旧 challenge 重放均拒绝。`checkpoint_payload()` 只是待签载荷，不签名、不自动信任。
生产签发方仍需独立观测与持久高水位，本组件未提供。

重启只重建通过工件/历史命令/审计验证的 registry，**不恢复内存有效指针，不自动启用 Reader**。
`recover_read_only()` 总是 `consumption_enabled=False`；普通启动不改写损坏文件。
`inspect_restart_public()` 在新进程重建全部验证器，只加载公钥。

| 差异 | 行为 |
| :--- | :--- |
| `CONTROL_INTENT_WITHOUT_COMMIT` | 孤立意图或不确定操作；要求对账 |
| `AUDIT_WITHOUT_CONTROL_COMMIT` | 保留审计，禁止消费 |
| `COMMIT_WITHOUT_POINTER` | 控制提交而指针缺失；只读报告 |
| `POINTER_WITHOUT_MATCHING_COMMIT` | 指针与提交不符；拒绝消费 |
| 损坏/撕裂历史 | `CORRUPT`，不自动截尾或重建 |

恢复需 selector 签名 `RECOVER`、当前差异检查点、CAS 和全部当前安全证据。
追加选择审计及既有 `SHADOW_ROLLBACK_COMPLETED`，再追加 RECOVERY_VERIFIED 引用待对账请求，保留旧事件。
恢复后仍停用；需新的提交后控制检查点、原有独立审计检查点及签名恢复证据，通过既有 `re_enable()`。
不修改原 recovery schema，不绕过原批准、时间、撤销校验。

## 7. Reader、回滚与紧急停用

`require_controlled_snapshot_selection=True` 必须组合全部既有严格选项和同一 registry/audit/verifier。
每次读验证当前检查点、durable selection，重载快照、比较 registry、复核当前授权，返回成功前再验证控制状态。
读期间选择/审计变化使原检查点失效。generation 变化使旧句柄失效；`is_generation_current()` 仍仅证明代际，
不是持续当前授权，当前消费须重新 `read()`。直接 legacy `registry.select()` 无法建立独立选择权威。

正常回滚用签名 `ROLLBACK`，历史快照须当前可消费；停用后使用 `RECOVER` 和受限再启用。
旧 simulator rollback 对受控 Reader 返回 `CONTROL_SELECTION_AUTHORIZATION_REQUIRED`。
紧急 disable 先停用 Reader/内存指针再尝试日志/审计；签名服务、时间或审计故障都不能撤销停用。

## 8. 并发、平台与剩余限制

锁顺序 controller → store / journal → audit，内层不反向取得 controller。
线程锁和 Windows `LK_NBLCK` / POSIX `LOCK_NB` 均有等待上限，超时拒绝。
并发同命令可在取得当前检查点后重试；冲突命令仅一个 CAS 成功。外部 audit writer 不受 controller 锁保护，
因此追加后仍验证签名 audit tip/count、精确链位置和读回，竞争时拒绝。

Windows 执行文件 flush/fsync、原子替换；Python 标准接口未提供等同 POSIX 的目录 fsync。
POSIX 另 fsync 父目录。**不声称进程崩溃测试证明硬件突然断电的持久性**。
仅支持本机合作进程和可信隔离目录；共享网络文件系统、多主机线性一致性未经支持，不得据此批准生产。
B-11 全局 ledger/读一致性仍是单独待办，本次只提供隔离控制面的有界锁与竞争检测。

## 9. 稳定拒绝码与兼容性

| 类别 | 代表代码 |
| :--- | :--- |
| 路径/文件 | `CONTROL_PATH_ALIAS`、`CONTROL_WORKSPACE_INVALID`、`PERSISTENT_STORE_UNCOMMITTED_FILE` |
| 工件 | `PERSISTENT_SNAPSHOT_SCHEMA_INVALID`、`PERSISTENT_PUBLICATION_SCHEMA_INVALID`、`PUBLICATION_EVIDENCE_MISSING` |
| 命令 | `CONTROL_COMMAND_MISSING`、`CONTROL_COMMAND_SCHEMA_INVALID`、`CONTROL_COMMAND_BINDING_MISMATCH` |
| CAS/replay | `CONTROL_COMPARE_AND_SWAP_FAILED`、`CONTROL_REQUEST_ID_CONFLICT`、`CONTROL_REQUEST_REPLAY` |
| 审计 | `CONTROL_AUDIT_RECEIPT_INVALID`、`CONTROL_AUDIT_READBACK_MISMATCH`、`CONTROL_AUDIT_COMPARE_AND_SWAP_FAILED` |
| 当前控制 | `CONTROL_CHECKPOINT_MISMATCH`、`CONTROL_CHECKPOINT_REPLAY`、`CONTROL_RECOVERY_REQUIRED`、`CONTROL_REGISTRY_BYPASS` |
| 无法核验 | `CONTROL_STORAGE_UNVERIFIABLE`、`CONTROL_JOURNAL_TORN`、`CONTROL_LOCK_TIMEOUT` |

签名、可信时间、批准、生命周期和撤销仍返回原模块拒绝码。
新 envelope/命令/journal/checkpoint 版本均 `1.0.0`，不变更已有 snapshot/authorization/audit/recovery schema。
legacy 非受控 registry 只作隔离历史兼容。

## 10. 可执行验收

新增测试覆盖 PC-01~PC-60，并扩展命令 schema/过期、角色互换、真实进程锁、逐提交步骤真实进程退出、
公钥重建、fsync/读回/权限失败、孤立文件、外部审计竞争、显式对账。
PC-60 配置检查不代替真实全量和最终 SHA 四个 CI job。功能演示实际执行签名、磁盘、审计、切换、重启、
恢复和确认丢失，输出结构化结果，比较七个生产库 SHA-256；不是静态预期表。

本地 Python 3.12.10 最终专项执行：**124 passed，0 failed，0 skipped（332.42 秒）**。
其余 15 个指定聚焦文件逐个执行，合计 **615 passed**，总聚焦覆盖 **739 passed**。
另以实际临时夹具并发执行两份签名 RECOVER（相同前态检查点）：一份 RECOVERY_VERIFIED，
另一份 CONTROL_CHECKPOINT_MISMATCH，generation 仅从 2 增至 3，Reader 始终停用。
这是单独执行的本地功能验证，**不计入 pytest 数量，不冒充远程 CI 用例**。
功能演示九个场景全部通过，七个实际生产 JSON 与初始 SHA-256 完全一致。
演示逐行的 `audit_committed / control_committed` 表示**该步骤得到验证的新提交确认**；读取步骤不追加事件，
因此为 false。不确定步骤可能已有物理写入，但未得到可靠确认，仍为 false。
操作返回的 `checkpoint_verified` 指操作前态检查点；后续读取另验证提交后的当前检查点。
输出同时列出 durable selection、registry pointer 和两者一致的 effective selection，避免混淆。
本地完整执行 `python -m pytest -q`：**2000 passed，0 failed，0 skipped（567.14 秒）**。
最终 SHA 的四个远程 CI job 与 workflow 状态在最终验收报告独立核验，不用配置检查代替远程执行。

| 证据 | 范围 |
| :--- | :--- |
| PC-01~10 | 不可变发布、签名、数据集/基线、内容指纹、幂等 |
| PC-11~20 | selector 角色、发布前拒绝、CAS、request ID、合法选择 |
| PC-21~30 | 绕过、选中快照、精确审计、无效回执、不确定提交、历史篡改、检查点根 |
| PC-31~40 | 检查点当前性/replay、只读重启、指针差异、显式恢复 |
| PC-41~49 | 撤销/替代/到期、当前批准/时间/撤销/生命周期、回滚、紧急停用 |
| PC-50~60 | 并发、符号链接/NTFS junction、路径、部分写入、有界锁、非权威检查、七库、生产导入、CI 配置 |
| 额外 64 项 | schema、角色根互换、真实进程锁、八处真实进程退出/公钥重建、权限/fsync/读回/日志失败、竞争、对账、当前水位、幂等认证 |

B-09 仅组件验证；B-10 只有组件级差异检测/对账改进；B-11 保持独立待办。
生产当前性、持久验证器高水位、独立审计基础设施、KMS/轮换、部署控制、恢复评审、多主机协调和断电保证均未建立。

**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO。**
