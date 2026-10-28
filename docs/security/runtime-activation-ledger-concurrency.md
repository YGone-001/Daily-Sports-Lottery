# 治理与 Shadow 账本的同步读取和有界锁

## 1. 范围与安全结论

起点为 `6146ff11c5eeb43a45b9ede19ae28241e7ee38be`。本次只调整本机合作进程的同步、
一致读取及必要的锁文件安全，不改变治理迁移、批准签名、激活/替代/撤销/时间语义或任何持久数据 schema。
**生产激活与生产 Reader 保持 NOT_ENABLED，生产映射写入 NOT_GRANTED，生产结论 NO-GO。**

| 属性 | 证明内容 | 不能替代 |
| :--- | :--- | :--- |
| 一致文件副本 | 一次读取包含同一已提交前缀 | 结构或签名有效性 |
| 结构有效历史 | 序号、链、事件身份、格式自洽 | 独立签名真实性 |
| 已认证历史 | 现有批准/工件签名可验证 | 全局当前性 |
| 独立锚定历史 | 检查点匹配这份副本 | 生产权威服务部署 |
| Shadow 选择权威 | 原有独立发布/选择/检查点及当前授权 | 生产激活权限 |
| 生产激活权威 | 本组件没有提供 | **NO-GO** |

## 2. 精确基线与已确认问题

Windows 基线探针在实现修改前执行。治理写入是临时文件加原子替换，不应凭猜测宣称它必然暴露半条记录。

| 探针 | 实际基线 |
| :--- | :--- |
| LC-01 | 原子替换前暂停，verify 不等待但返回完整旧前缀：count 1，最终 2；这是已有保护。另协调追加发生在报告派生过程中，确实得到 **entry_count 1 / binding_count 2** 的混合报告 |
| LC-02 | 合作审计 writer 持锁且只写出半条时，verify 不等待，报 TORN_FINAL_RECORD / UNTERMINATED_FINAL_RECORD；完成后完整计数为 2 |
| LC-03 | 将锁截止配置为 0.1 秒，线程 guard 等待 0.3 秒后仍未结束；源码 acquire 无 timeout |
| LC-04 | 本机没有可用 POSIX 环境；未把 Windows 结果冒充 POSIX。固定原锁源码在 Ubuntu CI 子进程中另执行，源码指纹如下 |
| LC-05 | Windows 实际 msvcrt 路径约 0.109 秒退出，3 次尝试，errno 13，winerror 未提供；释放后一次尝试成功 |
| LC-06 | 原始 append 持锁验证成功；若公共 verify 直接加同一非递归锁，会重复获取同一 guard，需拆分私有辅助 |
| LC-07 | 混合选择、追加、验证及只读恢复未观察到死锁；竞争会导致检查点失配、拒绝消费，不能误报为已复现死锁 |
| LC-08 | writer 释放后人为写入真正 torn tail，原实现仍拒绝且不截尾；此保护继续保留 |

`tools/shadow_ledger_lock_baseline.py` 固定了实现修改前 `inspect.getsource(_ledger_lock)` 的原始源码，
SHA-256 为 `5973d08e67e7ebb4f6b13ab8e0d4db839f98066e5f4bfb94f8167f87870ccf78`。
它只用于临时子进程基线探针，不被运行期模块引用，不是第二套运行期锁。
原型基线完整复现器要求精确起始实现，拒绝在修改后的实现上仍标注原 SHA。

还确认原锁在 `os.open` 抛错时没有进入后续 cleanup，可能留下已获取的线程 guard。
新失败注入对照固定基线复现此遗漏，并验证新实现不会残留 guard。

## 3. 锁架构与预算

`utils/ledger_lock.py` 提供 `local-ledger-lock-v1`。原 `_ledger_lock` 和隔离 `BoundedShadowLock`
均使用同一按规范路径索引的线程 guard 注册表，再获取同一锁文件的 OS 锁。
全局注册表互斥只保护极短的映射操作，**不在文件 I/O 或验证期间持有，不串行化不相关账本**。

| 调用方 | 默认预算 | 兼容性 |
| :--- | :--- | :--- |
| GovernanceLedger / 默认 shadow audit | 120 秒 | 保留原 Windows OS 获取预算数值，现在同时约束线程和 OS 排队 |
| 隔离 snapshot controller/store/journal/audit | 5 秒 | 原线程与 OS 各可等待 5 秒；现在合计 5 秒，争用时可能更早拒绝 |
| 重试间隔 | 原治理 0.05 秒；隔离 0.01 秒 | 非阻塞尝试后休眠，不 busy-spin |

注册表、线程 guard、OS 重试共用 monotonic 截止时间；前一部分耗时从后续剩余预算扣除。
**它不是对任意阻塞文件系统调用或任意系统负载的硬实时保证**。本地操作也可能因争用超时而拒绝。
没有隐式共享到独占升级，没有强制解锁，重复获取同线程同路径直接报递归锁错误。
锁文件路径排除 traversal、UNC、symlink/reparse point、硬链接别名；存储边界仍使用原生产保护守卫。

文件描述符以读写、创建、O_NOFOLLOW（可用时）打开，权限 0600；不替换锁文件。
Windows 使用 offset 0、length 1 的 `LK_NBLCK`，POSIX 使用 `flock(LOCK_EX | LOCK_NB)`。
**两平台读写均采用独占合作锁**，优先可验证性，不宣称共享读取吞吐提升。
Windows 可锁定 EOF 之后的字节，初始化字节移到成功获取 OS 锁之后，避免并发创建者写入别人已锁定的字节。

## 4. 所有权、异常与诊断

分别记录 thread owned、OS owned、fd 和拥有线程。只有确认拥有的锁才显式 unlock；
在 OS 成功但尚未返回确认的中断窗口，关闭本操作 fd，由内核释放其锁，不猜测性 unlock。
打开失败、初始化失败、OS 错误、中断、主体异常及 unlock 错误均执行 fd/guard 清理。
unlock 失败也关闭 fd，再释放线程 guard；原主体异常优先保留，release error 另有诊断。
不同线程不能替当前操作执行 exit；同一路径递归获取被拒绝。

| 分类 | 代码 / 行为 |
| :--- | :--- |
| 有界争用 | LEDGER_LOCK_TIMEOUT，phase 区分 registry / thread / os |
| 非预期 OS 错误 | LEDGER_LOCK_OS_ERROR，不当成普通 busy 重试 |
| 递归获取 | LEDGER_LOCK_RECURSIVE |
| 非拥有线程释放 | LEDGER_LOCK_OWNER_MISMATCH |
| 释放错误 | LEDGER_LOCK_RELEASE_FAILED |
| 路径问题 | LEDGER_LOCK_PATH_INVALID / LEDGER_LOCK_PATH_ALIAS |

诊断保留 policy、phase、elapsed、attempts、errno、winerror、锁定字节范围和底层错误。
隔离控制面保留 CONTROL_LOCK_TIMEOUT 等拒绝码；只读文件的原 `PermissionError` 类型继续保留，并附诊断。
Windows 仅在实际 OS 加锁阶段将预期 msvcrt busy errno 作为争用处理；打开失败和替换失败不使用此重试。
POSIX 仅非阻塞 flock 的 EAGAIN/EWOULDBLOCK 重试，权限、I/O 等错误立即分类退出。

## 5. 一致读取与无递归机制

GovernanceLedger 的 entries/raw_lines 与 writer 使用同一 `.lock`，在锁内读完并解析一份完整内容。
`verified_snapshot()` 对这一捕获 tuple 做纯结构验证，报告 count、tip、契约统计、缺证据序号都从同一 tuple 派生。
报告以序列化副本保存，调用方取 report 得到独立字典。状态、活动/旧绑定和当前资格视图也只派生一次读取。
治理 append 持锁期间只用 `_entries_unlocked()`、纯验证和纯状态辅助，不递归调用公共读取。

审计 records 在 writer 的精确锁身份下严格解析；verified_snapshot 对捕获记录校验完整链，
report、event types、chain tip、checkpoint payload 由这一副本派生。
append 继续在一份写锁内完成历史读取/验证、追加、flush/fsync，使用原私有读取和验证辅助。
**严格 LF、空白行、torn tail、身份、序号和全链检查没有放宽**，只读操作不改写历史数据文件。
missing 和 corrupt 保持不同；治理历史格式兼容保留，不为同步而规范化旧字节。

历史批准审计和治理视图构造复用同一 entries/report 副本，执行原 Ed25519 批准验证算法；
没有新的批准或签名引擎。外部审计检查点验证返回它实际认证的那份副本供恢复使用，
不会验证旧 count/tip 后再读取新 records 来构造恢复证据。
副本释放文件锁后可以继续做纯计算：其语义是**读取期间某个时点的已提交前缀**，不是持续锁住全局当前历史。
此后追加属于后继状态，不能混进这份较早报告。

权威批准视图还保留原有**当前性 fence**：签名审计使用显式只读副本来源，审计后再观察账本，
若历史推进则拒绝签发视图。后一次观察只用于拒绝，不参与任何字段派生；
这与把多次读取的 count、tip、批准记录混合构造一份报告不同。原安全回归断言保持不变。

## 6. 控制面锁图与检查点

实际顺序是 Controller → Snapshot Store / Control Journal / Shadow Audit。三个叶子存储不反向获取 Controller，
也不在持有其中一个叶子锁时请求另一个叶子锁。治理账本锁独立；解析、批准验证的纯计算不获取控制锁。
同一存储的 append 内部使用私有辅助，避免 store.get、journal.records 的公共方法重入。

Controller._inspect、审计提交读回、恢复证据、严格 re-enable 改用完整 snapshot，消除 count/tip/records 混合读取。
legacy 审计适配器仍需比对返回 report 的 count/tip 与 records，不接受不相干组合。
不同存储仍不是原子数据库事务：独立控制检查点必须匹配观察到的 store/journal/audit/指针状态；
外部 audit advance 使旧前态失效时，选择 CAS 和后续严格读取仍拒绝。
registry bypass、撤销/替代、当前批准、可信时间、显式恢复和先停用后 I/O 均继续强制。

## 7. Windows 调查结论

**ROOT_CAUSE_NOT_REPRODUCED**：没有证据把原间歇问题归因为杀软、文件替换或某个特定 WinError。
没有修改杀软、系统代理或全局权限。

实际可重复的独立观察包括：msvcrt 锁 byte 0、O_RDWR 句柄、errno 13 的有界争用、释放后再获取、
持锁进程终止后内核释放，以及 byte 1 与 byte 0 不冲突的范围实验。
另有意打开不允许 delete sharing 的数据文件句柄，本机 os.replace 实际报 **WinError 5**；
历史不变，关闭该句柄后相同追加成功。Windows 也可能返回明确 sharing violation 32，因此探针保留真实码。
这是受控共享条件的证据，**不是原间歇根因已证明或修复的声明**。
只读环境核验显示本机 C/F 均为 Healthy NTFS，WinDefend 服务为 Stopped；
Defender Operational 事件读取不可用。此信息既不证明存在干扰，也不能排除其它安全软件，不能用来归因。

## 8. 失败、压力与功能验证

LC-01~LC-50 使用独立监督子进程，每例 95 秒截止时间；卡住时只终止该例的自有进程。
跨进程 worker 使用 spawn、显式 pipe readiness 和 barrier，多 writer 与五 reader 在每轮同步，
检查每次 count/tip/sequence、前缀单调及最终追加数量。每组有独立总截止时间和进程清理。
功能入口自身也由 180 秒父进程监督，不依赖无限等待或仅靠任意 sleep 调度。

覆盖 before/after thread acquire、OS 获取和争用、中断、初始化、read/parse/validate、
append/flush/fsync、release、checkpoint、并发选择、持锁进程终止及重启。
真实 torn history 继续拒绝，不截尾、不自动修复。确认丢失后控制面保留原 UNCERTAIN 与非消费状态。
另在实际审计 append 写出半条并仍持锁时终止自有子进程：重启可重获锁，但仍拒绝 torn tail，原字节保留。

`python -m tools.shadow_ledger_concurrency_validation` 实际执行四组跨进程场景：
治理与审计各 1/2 writers + 5 readers、每组四轮和 40 次一致读取；最终记录数为 4 / 8。
另验证获取超时、释放后再获取、终止 holder、选择与审计验证并发、确认丢失停用和七库哈希。
输出动态 JSON，包含实测等待和错误码；不是静态预期表，也不是吞吐基准。
CI 两平台运行同一功能入口，并将真实 pytest 数量及实测诊断写入 notice，便于独立核对最终 SHA。

本地 Windows / Python 3.12.10 最终专项：**59 passed、2 skipped、0 failed（155.14 秒）**。
两项跳过仅为 LC-04 / LC-26 的 POSIX 专用路径；Windows msvcrt、字节范围与 deny-delete 实验均实际执行。
17 个指定聚焦文件合计 **839 passed、2 skipped**；另补跑原审计/回滚修正测试 **50 passed**，
总聚焦为 **889 passed、2 skipped**。完整功能入口最终 success=true，四组进程压力场景全部通过，
七个实际生产 JSON 与实现前 SHA-256 一致。
完整本地与最终 SHA 两平台 CI 数量在最终验收报告核验；不将本机数字推算为远程统计。
最终完整本地执行 `python -m pytest -q`：**2059 passed、2 skipped、0 failed（746.78 秒）**。
验证器另有一项负向用例：只有 LEDGER_LOCK_TIMEOUT 才可作为争用超时证据，
非预期 I/O 错误必须使验收失败。运行期锁的错误分类保持不变；修正以前向提交完成，不改写历史。
完整平台对照由最终 SHA 的远程实际 notice 核验，需同时说明相反平台专用用例的跳过原因。
平台专用用例只允许在相反平台跳过，并由对应平台 CI 实际执行。

## 9. 生产限制与阻断项

合作锁无法约束不遵守协议的 writer，不能防止拥有目录替换权限的敌对账户制造所有 TOCTOU 攻击。
UNC/共享网络文件系统和多主机协调不在支持范围；不能从本机 flock/msvcrt 推导网络语义。
fd close 或 I/O 的真实底层硬件故障可能使状态不可确认，必须拒绝而非猜测恢复。
进程退出实验不证明突然断电后的文件系统持久性；Windows 目录 fsync 限制仍存在。
独立检查点签发方、持久高水位、生产部署与安全评审仍未建立。

B-11 仅记录 **COMPONENT_IMPLEMENTED / COMPONENT_VERIFIED / ISOLATED_PROTOTYPE_ONLY / PRODUCTION_CONTROL_NOT_READY**
（COMPONENT_VERIFIED 须以最终实际测试通过为依据）。B-10 仍是独立事件/指针和部署控制风险，不能生产关闭。
**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO。**
