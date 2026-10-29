# 生产信任契约的验收门禁与离线证据范围

## 1. 精确预检

起点 `7c8e28f395cfb0edacd7e1e7055e254d14dbd43b`；保留 main `bb8bbe542449ceca3964bae42ad6f132b33b9af2`。
初次预检发现本地停在 `3fc6683081560936dc0ea6e7650155e8cb4123ee`，按要求停止。
用户随后明确授权 git pull；执行 `git pull --ff-only` 快进两个提交，再 fetch 后 HEAD=origin/develop=指定起点，
`git status --short` 为空、divergence 为 0/0。没有改写历史或合并 main。重新读取 AGENTS.md。
前次 [CI 40792435767](https://github.com/YGone-001/Daily-Sports-Lottery/actions/runs/40792435767)
实时核验整体 completed/success，head_sha 精确等于起点，四个 job 均 completed/success。

| 作业 | ID | 结果 |
| --- | --- | --- |
| Ubuntu full pytest | 123180663073 | completed/success |
| Windows Canonical lifecycle | 123180663147 | completed/success |
| Ubuntu Canonical lifecycle | 123180663220 | completed/success |
| Windows full pytest | 123180663291 | completed/success |

这些是前置 CI，不是本次最终提交的 CI。最终 SHA、真实本地计数与最终 workflow 全部完成状态在交付回复记录，
避免提交文档自引用尚未生成的 SHA 或预先宣布 CI 成功。

## 2. 门禁的证据分层

文档 gate 可在契约完整、准确、可审计时 PASS；这仅表示 DESIGNED。
实际部署门禁没有运行，统一 NOT_VERIFIED；十八项生产验收结果均 NOT_EXECUTED。
**OWNER_ASSIGNED、APPROVED、IMPLEMENTED、VERIFIED_IN_TEST、VERIFIED_IN_DEPLOYMENT、PRODUCTION_AUTHORIZED
不能从文档/合成证据推断。** 这里 VERIFIED_IN_TEST 指生产控制的测试证据，离线契约测试只验证登记一致性。
无论 A–W 如何通过，都不能自动授权生产；X 始终要求明确 NO-GO，本次不得触发 W14 实施。

| Gate | 范围 | 本次应取得的证据 |
| --- | --- | --- |
| A | Exact Baseline | 本次真实执行结果；不得从其他 gate 推断 |
| B | Eighteen-Authority Inventory | 文档准确且关联可验证代码/未来负向验收 |
| C | Trust-Root Bootstrap Contract | 文档准确且关联可验证代码/未来负向验收 |
| D | Actual Versus Conceptual Owners | 文档准确且关联可验证代码/未来负向验收 |
| E | Canonical and Governance Authority | 文档准确且关联可验证代码/未来负向验收 |
| F | Source Observation Provenance | 文档准确且关联可验证代码/未来负向验收 |
| G | Signing Role Separation | 文档准确且关联可验证代码/未来负向验收 |
| H | Key Rotation and Revocation | 文档准确且关联可验证代码/未来负向验收 |
| I | Trusted Time and Continuity | 文档准确且关联可验证代码/未来负向验收 |
| J | Current Lifecycle and Revocation | 文档准确且关联可验证代码/未来负向验收 |
| K | Independent Checkpoint Issuance | 文档准确且关联可验证代码/未来负向验收 |
| L | Audit and Crash Durability | 文档准确且关联可验证代码/未来负向验收 |
| M | Controlled Recovery Authority | 文档准确且关联可验证代码/未来负向验收 |
| N | Non-Bypassable Strict Deployment | 文档准确且关联可验证代码/未来负向验收 |
| O | Threat and Abuse Case Coverage | 文档准确且关联可验证代码/未来负向验收 |
| P | Dependency and Ownership Mapping | 文档准确且关联可验证代码/未来负向验收 |
| Q | Measurable Production Acceptance | 文档准确且关联可验证代码/未来负向验收 |
| R | Offline Contract Validation | 本次真实执行结果；不得从其他 gate 推断 |
| S | Existing Integration Regression | 本次真实执行结果；不得从其他 gate 推断 |
| T | Seven-Store Integrity | 本次真实执行结果；不得从其他 gate 推断 |
| U | Full Local Regression | 本次真实执行结果；不得从其他 gate 推断 |
| V | Four Remote CI Jobs and Workflow | 本次真实执行结果；不得从其他 gate 推断 |
| W | Forward-Only Git Integrity | 本次真实执行结果；不得从其他 gate 推断 |
| X | Explicit Production NO-GO | 本次真实执行结果；不得从其他 gate 推断 |

## 3. 可度量的未来生产验收

PA-A–R 每项有真实等效环境、正向控制、对抗输入、明确拒绝/一致性 oracle、原始独立证据与责任空缺。
对应角色/根/平台未建立即不能运行或关闭；时间与传播时限必须使用批准政策，未批准不得自行填数字。
TH-01–20 及 SD-01–15 的具体负向用例必须与 PA 类别同时执行，单一 happy path 不满足验收。
B-01–14 保留原编号与严重度；BT-* 补充根/责任/连续性/耐久/政策/部署/独立复评的横向阻断。
每个阻断至少引用一个有正负控制的 PA 测试，全部保持 OPEN。

## 4. 离线检查契约和完整性证明

`tests/test_production_trust_contracts.py` 解析这四份文档的 JSON 评审表，验证十八类与 signer/root/checkpoint
引用、职责分离、连续性要求、根来源未知的状态、依赖 DAG、阻断到负向验收的覆盖、failure state 消费规则、
严格模式降级覆盖以及 NO-GO 不可自动晋级。变异用例须证明删除/破坏约束会导致验证失败。
这些是文档契约校验，不签名、不部署、不新建应用配置或授权引擎。

七库保存开始时 SHA-256；本机存在的库须逐字节匹配，测试会话前后再比较。
CI checkout 不带被忽略的生产 JSON，缺失时只证明仍缺失且测试未写入，**不冒充七个真实库的字节验证**。
本地七库均存在，因此最终报告必须给出七项实测相同结论。
immutable_repository 固定本次允许变更以外的全部既有受控文件，包括旧安全测试、生产入口、工具、模型、
前端与 CI；只将 CRLF 归一为 LF 后计算路径+内容摘要，确保跨平台相同代码，
**七个生产库另用原始字节摘要，不做归一化**。新增受控/未忽略文件也必须落在七文件 allowlist。
该冻结清单是本次契约任务的边界，后续授权改代码时必须独立更新，不能把它当永久应用配置。

TC-01–16 的验证对象如下；检查要求对象间引用和行为约束成立，而不是匹配 Markdown 口号。

| TC | 离线验证对象 |
| --- | --- |
| TC-01 | 十八类 ID 唯一、必填字段与显式状态 |
| TC-02 | 实际 operational/security/implementation/availability owner 均未建立，不能填概念实体 |
| TC-03 | 每个生产根的来源、批准、key ID、分发位置、版本均未建立 |
| TC-04 | signer→authority→verifier→root 对应、全部角色分离及现有 wire purpose 源码对账 |
| TC-05 | 所有 current publisher 有 completeness、replay、persistent continuity 契约 |
| TC-06 | 每个 checkpoint_role 能关联到唯一 root 记录，不能自认证 |
| TC-07 | synthetic 根不可指定为生产根，缺根不得自我授权 |
| TC-08 | BT-ROOT 为 Critical，关联 PA-O/PA-R 可执行负向验收 |
| TC-09 | BT-CONTINUITY 为 Critical，关联跨重启/回滚/重新签发旧状态验收 |
| TC-10 | SD-01–15 全入口否定结果，不允许丢失后台/CLI/重启覆盖 |
| TC-11 | recovery 需独立双职责批准、执行者分离，禁止自批与自动再启用 |
| TC-12 | 每个 blocker 关联有正负控制、oracle、独立原始证据的 PA 测试 |
| TC-13 | gates、PA 与 work package 均不授予激活；NO-GO 固定；依赖图不循环 |
| TC-14 | 导入真实状态常量核对 NOT_ENABLED/NOT_GRANTED、无生产 activation/Reader API |
| TC-15 | 七库原始 SHA-256 与会话前后一致；CI 缺失范围明确 |
| TC-16 | allowlist 之外所有既有受控文件摘要不变且无新增运行期/凭据路径 |

## 5. 结构化门禁、验收和完整性登记

```json
{
  "contract_id": "production-acceptance-v1",
  "baseline": "7c8e28f395cfb0edacd7e1e7055e254d14dbd43b",
  "main_baseline": "bb8bbe542449ceca3964bae42ad6f132b33b9af2",
  "previous_workflow": {
    "id": 40792435767,
    "head_sha": "7c8e28f395cfb0edacd7e1e7055e254d14dbd43b",
    "status": "completed",
    "conclusion": "success",
    "successful_job_ids": [
      123180663073,
      123180663147,
      123180663220,
      123180663291
    ]
  },
  "production_activation_ready": false,
  "decision": "NO-GO",
  "automatic_activation": false,
  "acceptance_tests": [
    {
      "id": "PA-A",
      "authority_id": "A",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "受控真实账本追加 APPROVED 后派生 view",
      "negative_control": "重放旧 view、删除尾部、整链重写、任意 ledger 路径/根",
      "expected_result": "新批准仅在精确历史审计后可见；所有旧/错源拒绝",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-B",
      "authority_id": "B",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "真实全量数据同一快照独立重算 baseline 与 owner inventory",
      "negative_control": "空清单、缺一成员、错 dataset/baseline、冲突 owner",
      "expected_result": "完整性证明计数/成员摘要/版本精确一致；任一缺口拒绝",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-C",
      "authority_id": "C",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "认证采集身份签发带原始响应引用的真实观测",
      "negative_control": "假 adapter/provider、错 scope、过期/重放、provider 错误内容",
      "expected_result": "可认证来源才进入评估；签名不豁免内容错误/身份不足",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-D",
      "authority_id": "D",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "已接受职责的 owner 注册后签合法批准并保留历史",
      "negative_control": "未注册/吊销/失陷 key、跨用途签名、旧批准重放",
      "expected_result": "当前授权拒绝；历史密码学验签与当前权限分别报告",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-E",
      "authority_id": "E",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "独立 activation issuer 根据当前批准指纹签限定授权",
      "negative_control": "缺批准、错指纹、错范围、超政策寿命或越权 signer",
      "expected_result": "合法完整链才可继续；错误授权一律拒绝",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-F",
      "authority_id": "F",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "认证 time issuer 与独立 reference 在批准 drift 内推进",
      "negative_control": "issuer/reference 不可达、token 冻结、回拨、过期",
      "expected_result": "失效边界拒绝并停用，不能采用 caller 时间",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-G",
      "authority_id": "G",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "正常跨重启加载耐久时间/authority 水位并核对外部锚",
      "negative_control": "回滚备份、删除水位、写入确认丢失、旧 token 重签",
      "expected_result": "新会话也拒绝旧状态；不自动启用",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-H",
      "authority_id": "H",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "保留完整历史执行显式 A→B 和唯一 CURRENT",
      "negative_control": "双 CURRENT、环/分叉、删历史、A 旧快照、旧状态新签名",
      "expected_result": "只允许当前 B；B 失效不复活 A，冲突停用",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-I",
      "authority_id": "I",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "真实撤销 B 并传播到全部登记消费者",
      "negative_control": "重放空状态、漏事件、撤销/当前服务不可达、重启回退",
      "expected_result": "在批准传播预算内全部拒绝 B；未知当前性立即拒绝",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-J",
      "authority_id": "J",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "实际存储认证不可变发布且尚未选择",
      "negative_control": "孤立文件、错 publisher、内容替换、断电未提交",
      "expected_result": "发布不自动选择；不完整发布不能消费",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-K",
      "authority_id": "K",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "真实操作者签 SELECT 并完成前态 CAS",
      "negative_control": "同前态并发选择、越权身份、直接 registry/指针写入",
      "expected_result": "只有可证明的唯一提交生效；绕过拒绝且保留记录",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-L",
      "authority_id": "L",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "独立 control issuer 直接观察联合状态并持久水位",
      "negative_control": "旧 checkpoint、旧状态新 challenge 重签、替换 issuer、split brain",
      "expected_result": "精确当前联合状态可验；冲突和陈旧拒绝",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-M",
      "authority_id": "M",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "独立审计服务发布完整已提交前缀 checkpoint",
      "negative_control": "有效前缀截尾、整链重写、假 adapter/回执、跨 store",
      "expected_result": "外部锚检出每种差异，不自动修尾",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-N",
      "authority_id": "N",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "双职责批准 RECOVER，NONE 差异后显式再启用",
      "negative_control": "缺审批、旧/跨 store 证据、指针差异、撤销后恢复",
      "expected_result": "恢复提交仍停用；仅当前完整证据和单独再启用允许读取",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-O",
      "authority_id": "O",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "带外注册根、独立双批准分发并演练正常轮换",
      "negative_control": "整组根替换、旧根版本、失陷 issuer、应急根渠道故障",
      "expected_result": "未知/吊销/回退根拒绝；历史验签不授予当前权限",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-P",
      "authority_id": "P",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "在目标 OS/FS/硬件逐提交边界断电和恢复备份",
      "negative_control": "audit/commit/pointer 各边界断电、确认丢失、敌对目录替换",
      "expected_result": "只返回可证明状态；UNCERTAIN/CORRUPT 不消费，不声明多文件原子性",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-Q",
      "authority_id": "Q",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "轮值实际收到依赖故障告警并执行紧急停用",
      "negative_control": "issuer/checkpoint/audit/告警通道故障与停用时断网",
      "expected_result": "先拒绝消费，独立事故通道保全；重启保持禁用",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    },
    {
      "id": "PA-R",
      "authority_id": "R",
      "environment": "PRODUCTION_EQUIVALENT_REQUIRED",
      "prerequisites": [
        "OWNER_ASSIGNED",
        "APPROVED",
        "IMPLEMENTED",
        "APPROVED_POLICY",
        "INDEPENDENT_ROOTS"
      ],
      "positive_control": "固定配置/环境/根的受控入口在全链就绪时构造严格对象",
      "negative_control": "逐项执行 SD-01–15，包含 HTTP/后台/CLI/重启",
      "expected_result": "启动/消费按预期拒绝所有降级；测试或 gate 不改变生产 flags",
      "evidence": [
        "exact code/config/root versions",
        "authenticated actor identities and approvals",
        "raw independent logs and checkpoints",
        "before/after persistent state and restart observations",
        "independent reviewer signature"
      ],
      "actual_owner": "NOT_ESTABLISHED",
      "result": "NOT_EXECUTED",
      "grants_activation": false
    }
  ],
  "blockers": [
    {
      "id": "B-01",
      "severity": "Critical",
      "authority_ids": [
        "A"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-A"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-02",
      "severity": "Critical",
      "authority_ids": [
        "A",
        "B",
        "O"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-A",
        "PA-B",
        "PA-O"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-03",
      "severity": "Critical",
      "authority_ids": [
        "C"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-C"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-04",
      "severity": "Critical",
      "authority_ids": [
        "L",
        "M"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-L",
        "PA-M"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-05",
      "severity": "High",
      "authority_ids": [
        "N",
        "R"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-N",
        "PA-R"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-06",
      "severity": "High",
      "authority_ids": [
        "F",
        "G"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-F",
        "PA-G"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-07",
      "severity": "High",
      "authority_ids": [
        "D",
        "E"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-D",
        "PA-E"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-08",
      "severity": "High",
      "authority_ids": [
        "B",
        "R"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-B",
        "PA-R"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-09",
      "severity": "Medium",
      "authority_ids": [
        "J",
        "K",
        "L"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-J",
        "PA-K",
        "PA-L"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-10",
      "severity": "High",
      "authority_ids": [
        "N",
        "P"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-N",
        "PA-P"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-11",
      "severity": "Medium",
      "authority_ids": [
        "K",
        "P"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-K",
        "PA-P"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-12",
      "severity": "Informational",
      "authority_ids": [
        "R"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-R"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-13",
      "severity": "Medium",
      "authority_ids": [
        "H"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-H"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "B-14",
      "severity": "High",
      "authority_ids": [
        "I"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-I"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "BT-ROOT",
      "severity": "Critical",
      "authority_ids": [
        "O",
        "R"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-O",
        "PA-R"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "BT-OWNER",
      "severity": "Critical",
      "authority_ids": [
        "A",
        "B",
        "C",
        "D",
        "E",
        "F",
        "G",
        "H",
        "I",
        "J",
        "K",
        "L",
        "M",
        "N",
        "O",
        "P",
        "Q",
        "R"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-A",
        "PA-B",
        "PA-C",
        "PA-D",
        "PA-E",
        "PA-F",
        "PA-G",
        "PA-H",
        "PA-I",
        "PA-J",
        "PA-K",
        "PA-L",
        "PA-M",
        "PA-N",
        "PA-O",
        "PA-P",
        "PA-Q",
        "PA-R"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "BT-CONTINUITY",
      "severity": "Critical",
      "authority_ids": [
        "G",
        "H",
        "I",
        "L",
        "M"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-G",
        "PA-H",
        "PA-I",
        "PA-L",
        "PA-M"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "BT-DURABILITY",
      "severity": "High",
      "authority_ids": [
        "P"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-P"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "BT-POLICY",
      "severity": "High",
      "authority_ids": [
        "F",
        "G",
        "I",
        "Q"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-F",
        "PA-G",
        "PA-I",
        "PA-Q"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "BT-DEPLOYMENT",
      "severity": "Critical",
      "authority_ids": [
        "R"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-R"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    },
    {
      "id": "BT-REVIEW",
      "severity": "High",
      "authority_ids": [
        "A",
        "B",
        "C",
        "D",
        "E",
        "F",
        "G",
        "H",
        "I",
        "J",
        "K",
        "L",
        "M",
        "N",
        "O",
        "P",
        "Q",
        "R"
      ],
      "status": "OPEN",
      "future_acceptance_tests": [
        "PA-A",
        "PA-B",
        "PA-C",
        "PA-D",
        "PA-E",
        "PA-F",
        "PA-G",
        "PA-H",
        "PA-I",
        "PA-J",
        "PA-K",
        "PA-L",
        "PA-M",
        "PA-N",
        "PA-O",
        "PA-P",
        "PA-Q",
        "PA-R"
      ],
      "closure_evidence": "具名批准、实际来源/根/平台、独立部署验收原始证据；离线或合成测试不关闭",
      "deployment_verified": false
    }
  ],
  "gates": [
    {
      "id": "A",
      "name": "Exact Baseline",
      "kind": "GIT",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "本次真实执行结果；不得从其他 gate 推断"
    },
    {
      "id": "B",
      "name": "Eighteen-Authority Inventory",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "C",
      "name": "Trust-Root Bootstrap Contract",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "D",
      "name": "Actual Versus Conceptual Owners",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "E",
      "name": "Canonical and Governance Authority",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "F",
      "name": "Source Observation Provenance",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "G",
      "name": "Signing Role Separation",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "H",
      "name": "Key Rotation and Revocation",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "I",
      "name": "Trusted Time and Continuity",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "J",
      "name": "Current Lifecycle and Revocation",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "K",
      "name": "Independent Checkpoint Issuance",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "L",
      "name": "Audit and Crash Durability",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "M",
      "name": "Controlled Recovery Authority",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "N",
      "name": "Non-Bypassable Strict Deployment",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "O",
      "name": "Threat and Abuse Case Coverage",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "P",
      "name": "Dependency and Ownership Mapping",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "Q",
      "name": "Measurable Production Acceptance",
      "kind": "DOCUMENTATION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "文档准确且关联可验证代码/未来负向验收"
    },
    {
      "id": "R",
      "name": "Offline Contract Validation",
      "kind": "LOCAL_TEST",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "本次真实执行结果；不得从其他 gate 推断"
    },
    {
      "id": "S",
      "name": "Existing Integration Regression",
      "kind": "LOCAL_TEST",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "本次真实执行结果；不得从其他 gate 推断"
    },
    {
      "id": "T",
      "name": "Seven-Store Integrity",
      "kind": "LOCAL_TEST",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "本次真实执行结果；不得从其他 gate 推断"
    },
    {
      "id": "U",
      "name": "Full Local Regression",
      "kind": "LOCAL_TEST",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "本次真实执行结果；不得从其他 gate 推断"
    },
    {
      "id": "V",
      "name": "Four Remote CI Jobs and Workflow",
      "kind": "REMOTE_CI",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "本次真实执行结果；不得从其他 gate 推断"
    },
    {
      "id": "W",
      "name": "Forward-Only Git Integrity",
      "kind": "GIT",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "本次真实执行结果；不得从其他 gate 推断"
    },
    {
      "id": "X",
      "name": "Explicit Production NO-GO",
      "kind": "PRODUCTION_DECISION",
      "contract_status": "DESIGNED",
      "operational_status": "NOT_VERIFIED",
      "grants_activation": false,
      "evidence_requirement": "本次真实执行结果；不得从其他 gate 推断"
    }
  ],
  "protected_stores": {
    "daily_matches.json": "ee393b0dc6e575e6acd31886362edca0bbb75d30916502b1cc97a6d819f55431",
    "team_strength.json": "9673ca8680f9b5036cb5df6190b6c82f2fbf88df423c2bf0d2a3e93c24f62e73",
    "calibrated.json": "8798e528a8971551bc560f8f894b7b0504b003ae223a7d1af910b551edf1f450",
    "prediction_snapshots.json": "28d563ee977ae15748548fdd4728f5aca0e2665ead048d2ebea550742b7725a6",
    "odds_snapshots.json": "1343a67883475c16bfa61734e6f87ffcaa267822dcd83c33ef551bdbc67d3790",
    "settlements.json": "d1d4b0e2504f727d4364c46175e19d29e39066286422aa28298624459c1539f8",
    "evaluation_rows.json": "b114c1b1e0f0fbb6c297f1127849b7bed1ac886d0fd0631b8a7b441afb7398bd"
  },
  "immutable_repository": {
    "paths": [
      ".gitattributes",
      ".github/workflows/ci.yml",
      ".gitignore",
      "AGENTS.md",
      "LICENSE",
      "README.md",
      "app.py",
      "config.py",
      "data/.gitkeep",
      "docs/canonical-mapping-governance.md",
      "docs/canonical-snapshot-adapter.md",
      "docs/ingestion-identity-preflight.md",
      "docs/production-canonical-repair.md",
      "docs/runtime-activation-architecture.md",
      "docs/runtime-mapping-readiness.md",
      "docs/security/runtime-activation-approval-binding.md",
      "docs/security/runtime-activation-audit-recovery-risk.md",
      "docs/security/runtime-activation-integrated-security-review.md",
      "docs/security/runtime-activation-ledger-concurrency.md",
      "docs/security/runtime-activation-revocation-authority.md",
      "docs/security/runtime-activation-security-review.md",
      "docs/security/runtime-activation-snapshot-control.md",
      "docs/security/runtime-activation-supersession.md",
      "docs/security/runtime-activation-trusted-time.md",
      "docs/source-id-migration-planning.md",
      "docs/source-identity-provenance.md",
      "docs/staging-canonical-repair.md",
      "frontend/index.html",
      "frontend/package-lock.json",
      "frontend/package.json",
      "frontend/src/App.tsx",
      "frontend/src/api/client.test.ts",
      "frontend/src/api/client.ts",
      "frontend/src/api/index.ts",
      "frontend/src/api/matches.ts",
      "frontend/src/api/strategy.ts",
      "frontend/src/components/EmptyState/EmptyState.module.css",
      "frontend/src/components/EmptyState/EmptyState.tsx",
      "frontend/src/components/EmptyState/index.ts",
      "frontend/src/components/Footer/Footer.module.css",
      "frontend/src/components/Footer/Footer.tsx",
      "frontend/src/components/Footer/index.ts",
      "frontend/src/components/Icon/Icon.module.css",
      "frontend/src/components/Icon/Icon.tsx",
      "frontend/src/components/Icon/index.ts",
      "frontend/src/components/KellyCalculator/KellyCalculator.module.css",
      "frontend/src/components/KellyCalculator/KellyCalculator.tsx",
      "frontend/src/components/KellyCalculator/index.ts",
      "frontend/src/components/Layout/Layout.module.css",
      "frontend/src/components/Layout/Layout.tsx",
      "frontend/src/components/Layout/index.ts",
      "frontend/src/components/Navbar/Navbar.module.css",
      "frontend/src/components/Navbar/Navbar.tsx",
      "frontend/src/components/Navbar/index.ts",
      "frontend/src/components/OddsTimeline/OddsTimeline.module.css",
      "frontend/src/components/OddsTimeline/OddsTimeline.tsx",
      "frontend/src/components/OddsTimeline/index.ts",
      "frontend/src/components/ProbBar/ProbBar.module.css",
      "frontend/src/components/ProbBar/ProbBar.tsx",
      "frontend/src/components/ProbBar/index.ts",
      "frontend/src/components/RadarChart/RadarChart.module.css",
      "frontend/src/components/RadarChart/RadarChart.tsx",
      "frontend/src/components/RadarChart/index.ts",
      "frontend/src/components/Skeleton/Skeleton.module.css",
      "frontend/src/components/Skeleton/Skeleton.tsx",
      "frontend/src/components/Skeleton/index.ts",
      "frontend/src/components/SportFilter/SportFilter.module.css",
      "frontend/src/components/SportFilter/SportFilter.tsx",
      "frontend/src/components/SportFilter/index.ts",
      "frontend/src/components/StatusTag/StatusTag.module.css",
      "frontend/src/components/StatusTag/StatusTag.tsx",
      "frontend/src/components/StatusTag/index.ts",
      "frontend/src/components/SuspenseStars/SuspenseStars.module.css",
      "frontend/src/components/SuspenseStars/SuspenseStars.tsx",
      "frontend/src/components/SuspenseStars/index.ts",
      "frontend/src/components/index.ts",
      "frontend/src/features/DailyView/DailyView.module.css",
      "frontend/src/features/DailyView/DailyView.tsx",
      "frontend/src/features/DailyView/MatchRow.module.css",
      "frontend/src/features/DailyView/MatchRow.tsx",
      "frontend/src/features/DailyView/index.ts",
      "frontend/src/features/HistoryView/HistoryView.module.css",
      "frontend/src/features/HistoryView/HistoryView.tsx",
      "frontend/src/features/HistoryView/index.ts",
      "frontend/src/features/MatchDetail/MatchDetail.module.css",
      "frontend/src/features/MatchDetail/MatchDetail.tsx",
      "frontend/src/features/MatchDetail/index.ts",
      "frontend/src/features/StrategyView/StrategyView.module.css",
      "frontend/src/features/StrategyView/StrategyView.tsx",
      "frontend/src/features/StrategyView/index.ts",
      "frontend/src/features/index.ts",
      "frontend/src/hooks/index.ts",
      "frontend/src/hooks/useClock.ts",
      "frontend/src/hooks/useMatches.ts",
      "frontend/src/hooks/useRefresh.ts",
      "frontend/src/hooks/useStrategy.ts",
      "frontend/src/hooks/useTheme.ts",
      "frontend/src/main.tsx",
      "frontend/src/router.tsx",
      "frontend/src/styles/globals.css",
      "frontend/src/styles/shared.css",
      "frontend/src/styles/theme.css",
      "frontend/src/types/hooks.d.ts",
      "frontend/src/types/index.ts",
      "frontend/src/types/match.ts",
      "frontend/src/types/strategy.ts",
      "frontend/src/vite-env.d.ts",
      "frontend/tsconfig.json",
      "frontend/tsconfig.node.json",
      "frontend/vite.config.ts",
      "models/basketball_model.py",
      "models/poisson_model.py",
      "models/predictor.py",
      "models/strategy.py",
      "pytest.ini",
      "requirements-dev.txt",
      "requirements.txt",
      "start.bat",
      "start.sh",
      "static/css/style.css",
      "static/js/app.js",
      "templates/base.html",
      "templates/history.html",
      "templates/index.html",
      "templates/match.html",
      "templates/strategy.html",
      "tests/conftest.py",
      "tests/test_activation_revocation_authority.py",
      "tests/test_activation_supersession.py",
      "tests/test_basketball_margin_semantics.py",
      "tests/test_basketball_probability_semantics.py",
      "tests/test_calibration_evaluation.py",
      "tests/test_canonical_identity_guard.py",
      "tests/test_canonical_ingestion_guard.py",
      "tests/test_canonical_mapping_governance.py",
      "tests/test_canonical_repair_end_to_end.py",
      "tests/test_canonical_snapshot_adapter.py",
      "tests/test_classification_evaluation.py",
      "tests/test_concurrency.py",
      "tests/test_dixon_coles_counterfactual.py",
      "tests/test_dixon_coles_fitting.py",
      "tests/test_dixon_coles_walkforward.py",
      "tests/test_evaluation_rows.py",
      "tests/test_evaluation_scraper_integration.py",
      "tests/test_evidence_status.py",
      "tests/test_fetcher_compatibility.py",
      "tests/test_finalized_match_integrity.py",
      "tests/test_football_attack_defense.py",
      "tests/test_history_evidence.py",
      "tests/test_ledger_read_consistency.py",
      "tests/test_market_coverage.py",
      "tests/test_market_universe_integration.py",
      "tests/test_match_lifecycle.py",
      "tests/test_match_reconciliation.py",
      "tests/test_model_versioning.py",
      "tests/test_odds_scraper_integration.py",
      "tests/test_odds_snapshots.py",
      "tests/test_persistent_shadow_snapshot_control.py",
      "tests/test_prediction_scraper_integration.py",
      "tests/test_prediction_snapshots.py",
      "tests/test_production_canonical_repair.py",
      "tests/test_provider_identity_evidence.py",
      "tests/test_runtime_activation_approval_binding.py",
      "tests/test_runtime_activation_authorization.py",
      "tests/test_runtime_activation_design.py",
      "tests/test_runtime_activation_recovery_binding.py",
      "tests/test_runtime_activation_security_review.py",
      "tests/test_runtime_activation_trust_hardening.py",
      "tests/test_runtime_mapping_readiness.py",
      "tests/test_settlement_api.py",
      "tests/test_settlement_scraper_integration.py",
      "tests/test_settlements.py",
      "tests/test_shadow_activation_audit_store.py",
      "tests/test_shadow_activation_simulation.py",
      "tests/test_shadow_runtime_authority_corrections.py",
      "tests/test_shadow_runtime_reader.py",
      "tests/test_shadow_trust_chain_integration.py",
      "tests/test_snapshot_api.py",
      "tests/test_source_id_migration_manifest.py",
      "tests/test_source_identity_provenance.py",
      "tests/test_staging_canonical_repair.py",
      "tests/test_team_strength_determinism.py",
      "tests/test_temporal_evaluation.py",
      "tests/test_trusted_time_authority.py",
      "tools/canonical_snapshot_adapter.py",
      "tools/mapping_governance_shadow.py",
      "tools/production_canonical_repair.py",
      "tools/shadow_activation_supersession_validation.py",
      "tools/shadow_approval_binding_validation.py",
      "tools/shadow_ledger_concurrency_baseline.py",
      "tools/shadow_ledger_concurrency_validation.py",
      "tools/shadow_ledger_lock_baseline.py",
      "tools/shadow_reenablement_baseline.py",
      "tools/shadow_revocation_authority_validation.py",
      "tools/shadow_revocation_baseline.py",
      "tools/shadow_runtime_activation.py",
      "tools/shadow_snapshot_control_baseline.py",
      "tools/shadow_snapshot_control_validation.py",
      "tools/shadow_supersession_baseline.py",
      "tools/shadow_trust_chain_validation.py",
      "tools/shadow_trusted_time_validation.py",
      "tools/staging_canonical_repair.py",
      "utils/__init__.py",
      "utils/activation_revocation_authority.py",
      "utils/activation_supersession.py",
      "utils/atomic_json.py",
      "utils/authoritative_governance.py",
      "utils/calibration_evaluation.py",
      "utils/classification_evaluation.py",
      "utils/concurrency.py",
      "utils/daily_loader.py",
      "utils/dixon_coles_counterfactual.py",
      "utils/dixon_coles_fitting.py",
      "utils/dixon_coles_walkforward.py",
      "utils/evaluation_rows.py",
      "utils/evidence_status.py",
      "utils/fetcher_500.py",
      "utils/history_evidence.py",
      "utils/ingestion_identity.py",
      "utils/ledger_lock.py",
      "utils/mapping_governance.py",
      "utils/market_coverage.py",
      "utils/match_identity.py",
      "utils/match_lifecycle.py",
      "utils/observation_authority.py",
      "utils/odds_snapshots.py",
      "utils/persistent_shadow_snapshots.py",
      "utils/prediction_snapshots.py",
      "utils/provider_evidence.py",
      "utils/recovery_authority.py",
      "utils/runtime_activation_authorization.py",
      "utils/runtime_activation_design.py",
      "utils/runtime_mapping_eligibility.py",
      "utils/runtime_mapping_policy.py",
      "utils/scraper.py",
      "utils/settlements.py",
      "utils/shadow_activation_audit_store.py",
      "utils/shadow_activation_simulation.py",
      "utils/shadow_runtime_reader.py",
      "utils/shadow_snapshot_control.py",
      "utils/shadow_snapshot_control_journal.py",
      "utils/source_id_migration.py",
      "utils/source_identity.py",
      "utils/team_strength.py",
      "utils/temporal_evaluation.py",
      "utils/trusted_artifacts.py",
      "utils/trusted_time_authority.py"
    ],
    "normalized_sha256": "3a6ee30936a885dafcabdbba3ddfbbbd6021824c96a0440032098cfa72f7c773",
    "allowed_change_paths": [
      "docs/security/runtime-activation-production-trust-bootstrap.md",
      "docs/security/runtime-activation-production-authority-ownership.md",
      "docs/security/runtime-activation-production-deployment-contract.md",
      "docs/security/runtime-activation-production-acceptance-gates.md",
      "docs/security/runtime-activation-production-readiness-matrix.md",
      "docs/security/runtime-activation-production-blockers.md",
      "tests/test_production_trust_contracts.py"
    ]
  },
  "production_progress": {
    "DESIGNED": "YES",
    "OWNER_ASSIGNED": "NO",
    "APPROVED": "NO",
    "IMPLEMENTED": "NO",
    "VERIFIED_IN_TEST": "NO",
    "VERIFIED_IN_DEPLOYMENT": "NO",
    "PRODUCTION_AUTHORIZED": "NO"
  }
}
```

## 6. 执行顺序和停止线

```powershell
python -m pytest -q tests/test_production_trust_contracts.py
python -m pytest -q tests/test_shadow_trust_chain_integration.py
python -m pytest -q
git diff --check
git diff --cached --check
```

完整回归后只显式暂存七个允许文件；作者与提交者为 YGone 的指定 noreply 身份。
单行提交、空 body、禁用分期字眼、前向提交与普通 push；最终 fetch 验证 HEAD=origin/develop，main 不变，工作区干净。
最终 CI 需同一 head_sha、四个作业 success **以及整体 workflow completed/success**，不能仅凭个别 job 成功结束。
停止于文档、离线契约验证、本地回归、提交推送、最终 CI 和交付报告；不进入基础设施实施。
**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO。**
