# 生产根信任引导、密钥身份与权威来源契约

## 1. 结论与信任起点

**DESIGNED；生产 root 来源、实际 root custodian 和批准者均 NOT_ESTABLISHED。**
签名工件不能证明其自身公钥应受信；用户给出的公钥、fixture 公钥或请求附带的证书不自动成为生产根。
本次不生成真实根、不生成生产私钥、不选择或部署 KMS/HSM。`root:<role>` 只是 dummy 标识。
初始根信任来自独立批准的带外身份核验与受控部署渠道，而非被验签的业务工件。
该带外渠道本身的实际系统、责任人和恢复途径必须先有证据，不能用“渠道可信”循环自证。

## 2. 引导顺序 A–N

| 编号 | 步骤 | 必须取得的证据 | 负向验收 |
| --- | --- | --- | --- |
| RT-A | 初始注册 | 核验带外指纹与受托主体、来源、用途、环境/dataset；登记申请不赋予信任 | 未经批准公钥无法验证生产输入 |
| RT-B | 根授权批准 | 根托管角色和独立安全角色核对来源、职责接受与注册内容并共同批准 | 申请人自行签名或单人批准拒绝 |
| RT-C | 密钥身份与用途 | 记录算法、公钥指纹、角色、用途域、签发者/验证者身份和最小权限 | 合法签名跨角色拒绝 |
| RT-D | 范围绑定 | 固定 environment、dataset、schema/策略版本和允许的 authority ID | 跨环境或数据集根拒绝 |
| RT-E | 版本 | 登记根版本、最低可接受版本和独立恢复 epoch | 旧版本或相同版本不同内容拒绝 |
| RT-F | 分发 | 通过单独批准的部署配置渠道发布版本化根包与内容摘要，保留接收回执 | 工件内附带根、请求参数根均不接受 |
| RT-G | 配置完整性 | 部署准入核对代码/配置摘要、审批链、固定路径/权限和已安装最低版本 | 替换配置、缺失身份或摘要失配禁止启动 |
| RT-H | 正常轮换 | 旧/新根转换记录经独立批准；重叠窗口必须有政策，verifier 确认后淘汰旧签发 | 新旧根单独签名均不能取代独立批准 |
| RT-I | 紧急吊销 | 经独立应急根治理渠道冻结失陷角色，发布不可回退撤销版本并停用消费 | 无法确认最新根撤销即停用 |
| RT-J | 失陷恢复 | 封存受影响记录，确定失陷边界，替换凭据、重建当前事实并双独立批准新 epoch | 仅因旧签名曾有效不能继续授权 |
| RT-K | 历史验签 | 保留所有版本公钥、角色绑定、审批和吊销/失陷时间线；历史密码学有效单独标注 | 历史可审计不恢复当前授权 |
| RT-L | 防降级 | 最低根/策略版本与部署 epoch 存于受保护连续性存储并由独立 checkpoint 锚定 | 回滚磁盘、备份、根包均禁止消费 |
| RT-M | 独立审批 | 记录实际审批身份、不同职责、变更 request ID、范围、原因与审计回执 | 申请、执行和批准不能由同一主体完成 |
| RT-N | 启动行为 | 先固定环境、根、模式、存储身份、高水位与最新外部 checkpoint，再建立只读验证器 | 缺根、未知根、无效根、未知当前性一律启动失败；不自动选择或启用 |

**根包最低内容**：包 ID、环境、dataset、authority/role、签发和验证主体、算法/公钥指纹、用途、
版本/最低版本、启用/停止签发边界、前任根转换、失陷/吊销状态、独立批准引用、配置摘要和部署回执。
这是未来部署的设计，不改变当前任何工件 schema。根包签名必须能追溯到预先批准的带外信任；
部署管理员不能通过替换整个包和全部 verifier 公钥来绕过独立准入。

## 3. 生产根来源登记（全部尚未建立）

每个 signer 的 root_id 与所有权登记逐项关联。所有 production_source/key_id/approval_evidence
均保持 NOT_ESTABLISHED；synthetic=false 表示**未指定测试根为生产根**，不表示生产根已存在。

```json
{
  "contract_id": "production-trust-bootstrap-v1",
  "roots": [
    {
      "root_id": "root:root-approval",
      "role_id": "root-approval",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:canonical-publisher",
      "role_id": "canonical-publisher",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:inventory-attestor",
      "role_id": "inventory-attestor",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:owner-approval",
      "role_id": "owner-approval",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:governance-view",
      "role_id": "governance-view",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:ledger-checkpoint",
      "role_id": "ledger-checkpoint",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:governance-revocation",
      "role_id": "governance-revocation",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:observation",
      "role_id": "observation",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:activation",
      "role_id": "activation",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:time",
      "role_id": "time",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:lifecycle",
      "role_id": "lifecycle",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:lifecycle-current",
      "role_id": "lifecycle-current",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:lifecycle-checkpoint",
      "role_id": "lifecycle-checkpoint",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:revocation",
      "role_id": "revocation",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:revocation-current",
      "role_id": "revocation-current",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:revocation-checkpoint",
      "role_id": "revocation-checkpoint",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:publisher",
      "role_id": "publisher",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:selector",
      "role_id": "selector",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:control-checkpoint",
      "role_id": "control-checkpoint",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:audit-checkpoint",
      "role_id": "audit-checkpoint",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    },
    {
      "root_id": "root:recovery",
      "role_id": "recovery",
      "production_source": "NOT_ESTABLISHED",
      "operational_owner": "NOT_ESTABLISHED",
      "approval_evidence": "NOT_ESTABLISHED",
      "key_id": "NOT_ESTABLISHED",
      "distribution_location": "NOT_ESTABLISHED",
      "environment": "NOT_ESTABLISHED",
      "dataset_identity": "NOT_ESTABLISHED",
      "version": "NOT_ESTABLISHED",
      "minimum_accepted_version": "NOT_ESTABLISHED",
      "status": "NOT_ESTABLISHED",
      "synthetic": false
    }
  ],
  "bootstrap_steps": [
    {
      "id": "RT-A",
      "name": "初始注册",
      "evidence": "核验带外指纹与受托主体、来源、用途、环境/dataset；登记申请不赋予信任",
      "negative_test": "未经批准公钥无法验证生产输入"
    },
    {
      "id": "RT-B",
      "name": "根授权批准",
      "evidence": "根托管角色和独立安全角色核对来源、职责接受与注册内容并共同批准",
      "negative_test": "申请人自行签名或单人批准拒绝"
    },
    {
      "id": "RT-C",
      "name": "密钥身份与用途",
      "evidence": "记录算法、公钥指纹、角色、用途域、签发者/验证者身份和最小权限",
      "negative_test": "合法签名跨角色拒绝"
    },
    {
      "id": "RT-D",
      "name": "范围绑定",
      "evidence": "固定 environment、dataset、schema/策略版本和允许的 authority ID",
      "negative_test": "跨环境或数据集根拒绝"
    },
    {
      "id": "RT-E",
      "name": "版本",
      "evidence": "登记根版本、最低可接受版本和独立恢复 epoch",
      "negative_test": "旧版本或相同版本不同内容拒绝"
    },
    {
      "id": "RT-F",
      "name": "分发",
      "evidence": "通过单独批准的部署配置渠道发布版本化根包与内容摘要，保留接收回执",
      "negative_test": "工件内附带根、请求参数根均不接受"
    },
    {
      "id": "RT-G",
      "name": "配置完整性",
      "evidence": "部署准入核对代码/配置摘要、审批链、固定路径/权限和已安装最低版本",
      "negative_test": "替换配置、缺失身份或摘要失配禁止启动"
    },
    {
      "id": "RT-H",
      "name": "正常轮换",
      "evidence": "旧/新根转换记录经独立批准；重叠窗口必须有政策，verifier 确认后淘汰旧签发",
      "negative_test": "新旧根单独签名均不能取代独立批准"
    },
    {
      "id": "RT-I",
      "name": "紧急吊销",
      "evidence": "经独立应急根治理渠道冻结失陷角色，发布不可回退撤销版本并停用消费",
      "negative_test": "无法确认最新根撤销即停用"
    },
    {
      "id": "RT-J",
      "name": "失陷恢复",
      "evidence": "封存受影响记录，确定失陷边界，替换凭据、重建当前事实并双独立批准新 epoch",
      "negative_test": "仅因旧签名曾有效不能继续授权"
    },
    {
      "id": "RT-K",
      "name": "历史验签",
      "evidence": "保留所有版本公钥、角色绑定、审批和吊销/失陷时间线；历史密码学有效单独标注",
      "negative_test": "历史可审计不恢复当前授权"
    },
    {
      "id": "RT-L",
      "name": "防降级",
      "evidence": "最低根/策略版本与部署 epoch 存于受保护连续性存储并由独立 checkpoint 锚定",
      "negative_test": "回滚磁盘、备份、根包均禁止消费"
    },
    {
      "id": "RT-M",
      "name": "独立审批",
      "evidence": "记录实际审批身份、不同职责、变更 request ID、范围、原因与审计回执",
      "negative_test": "申请、执行和批准不能由同一主体完成"
    },
    {
      "id": "RT-N",
      "name": "启动行为",
      "evidence": "先固定环境、根、模式、存储身份、高水位与最新外部 checkpoint，再建立只读验证器",
      "negative_test": "缺根、未知根、无效根、未知当前性一律启动失败；不自动选择或启用"
    }
  ],
  "root_change_approval": {
    "required_roles": [
      "root-custodian",
      "independent-security-approver"
    ],
    "actual_approvers": "NOT_ESTABLISHED",
    "self_approval_allowed": false
  },
  "unknown_root_action": "FAIL_CLOSED",
  "artifact_self_authentication": false,
  "caller_selected_root_allowed": false,
  "historical_signature_grants_current_authority": false,
  "synthetic_root_allowed": false,
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

## 4. Canonical、历史批准与当前治理来源

| 事实 | 发布及完整性契约 | 当前代码与生产缺口 |
| --- | --- | --- |
| dataset 与 baseline | B 的 canonical-publisher 从同一受控只读数据快照计算指纹，绑定环境/dataset/schema/版本 | `build_authoritative_governance_view` 目前接受 caller 的 baseline 和签名 manifest；实际 publisher 未建立 |
| 完整 owner inventory | B 的 inventory-attestor 独立核对完整数据范围、计数、成员摘要、分页边界与 owner 唯一性，并签完整性声明 | 现有 canonical_ids 仅验证签名、基线和非空；非空或一个有效列表不证明生产完整性 |
| 治理历史 | A 的固定来源只读发布完整账本，ledger identity/path 由受控配置固定 | `GovernanceLedger.verified_snapshot` 在合作锁下给出同一已提交前缀；不能证明 caller 选择的路径是生产权威 |
| 历史 owner 批准 | D 注册的历史 owner key 与批准原文、proposal 指纹、APPROVED transition、dataset/baseline 共同验签 | `audit_historical_approvals` 与 `_derive_approved_records` 确实执行认证；单独字符串不是证据 |
| 当前提案与 supersession | A 由经验证历史推导当前状态；合法 SUPERSEDED/撤销追加让旧提案非当前，保留原批准 | `ACTIVE_STATES`、view facts 与批准高水位已有隔离控制；生产全局当前源未建立 |
| 当前撤销事实 | A 的治理撤销与 I 的独立撤销保守合并；任何 caller 空集合不能清除认证撤销 | 当前 view 的签名不证明撤销源全局最新，仍需独立当前 checkpoint |
| 当前治理 view | A 的 governance-view 角色只签同一 checkpoint-bound 历史派生事实；读取前重查当前发布序号 | builder 二次观察仅作拒绝 fence，不能把多次读取字段混为一份快照 |
| 独立 ledger checkpoint | ledger-checkpoint 独立身份/密钥/读权限，直接观察固定账本和持久高水位 | 精确 identity/count/tip 已在代码验证；生产独立签发服务与根来源未建立 |

实际 publisher、completeness attestor、historical owner registry 与 current-state publisher 均
**NOT_ESTABLISHED**。完整来源无法确立时不得猜测 ownership、接受 caller 列表、忽略空值或生成新 Canonical ID。
**当前所有权无法确立 → 拒绝消费并阻断部署**。本设计保持 `same_event()`、v3 source identity、
`validate_ingestion_identity()` 的原身份与 fail-closed 语义，禁止修改 `scraper.refresh()`。

## 5. 观测来源边界

C 的 observation issuer 必须绑定实际 adapter/service principal、上游 provider 身份与采集入口、
采集 timestamp、dataset、sport、event date、provider uniqueness scope、identity schema version、
observation ID/语义指纹以及原始响应的不可变证据引用。原始 HTTP 内容、请求元数据和解析器版本需受控保留；
保留政策与存储尚未批准。生产入口和 adapter 到 provider 的绑定也必须认证，不能只信 provider 字符串。
`verify_observation_attestation` 已核验 dataset、scope、adapter、采集时间和语义指纹；
目前工件没有完整上游原始证据保留服务，新增生产 provenance 字段需单独实现/评审，不能宣称代码已经支持。

签名只证明**受信采集者声称采集了该观测**，不能证明 provider 内容事实正确。
缺字段、scope 不足、provider 编号复用、冲突、异常内容均隔离并拒绝影响映射；不能用签名升级弱身份。
每次消费以可信时间检查 freshness，去重/重放状态跨重启保存。
采集服务失陷时停用该 issuer，独立吊销其 key、封存原始证据、标记受影响窗口，重新认证观测并重评所有下游授权；
不得通过新签名使旧错误数据自动恢复可信。误报/事实错误的裁决责任和安全批准者仍未建立。

## 6. 签名职责与现有 wire contract 对账

所有权文档 signer_roles 是未来逻辑职责表，**不新增 wire 常量**。必须复用现有用途域/算法，
独立 inventory 完整性与 root 包则是未来待审批契约。
`trusted_artifacts.verify_trusted_artifact` 只在明确配置的公钥下验证类型/签名/时间；
`StrictActivationVerifier`、lifecycle、revocation、controller 固定部分根身份，不能替代根注册与分发。
`ShadowReadContext` 的 observation/recovery 等根仍可随调用提供，未来必须由固定部署封装消除替换入口。
合成场景 checkpoint key 同时验证 ledger/Canonical/治理撤销/lifecycle/audit，这个测试简化**不得移植生产**。
用途字符串的实际值以源码为准；下面登记用于复核，不将逻辑标签冒充现有 wire schema。

## 7. 密钥生命周期与失陷控制

注册后按角色最小权限授权签名，签发服务不能导出生产私钥；具体 KMS/HSM 和平台待授权选择。
密钥不得存于仓库或应用工作区。正常轮换需独立批准、受控双版本分发、verifier 接收确认和可审计淘汰，
重叠时长为 POLICY_NOT_APPROVED。根撤销与业务 activation 撤销是不同权威，二者均要独立当前性。
若 issuer 失陷，即使历史签名数学上有效，当前消费也必须暂停并按独立事故裁决确定污染窗口。
保留历史 public key 和原始工件可用于审计，不等于继续授权；失陷时间无法界定时采取保守停用。
根失陷不能仅依靠该根签发“我已恢复”的消息；必须通过预先建立的独立应急批准渠道重新引导。
轮换、撤销、恢复和 root downgrade 的证据归入 PA-O 与 RT-A–N 负向验收。

**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO。**

## 8. 根引导的生产证据状态

| 状态 | 当前结论及晋级证据 |
| --- | --- |
| DESIGNED | YES；仅有本引导契约 |
| OWNER_ASSIGNED | NO；无实际 root custodian/独立批准者的接受记录 |
| APPROVED | NO；无真实根来源、配置分发或应急渠道批准 |
| IMPLEMENTED | NO（生产）；现有验签组件不等于生产根治理 |
| VERIFIED_IN_TEST | NO（生产控制）；离线契约校验不等于根生命周期测试 |
| VERIFIED_IN_DEPLOYMENT | NO；未部署或独立验证真实根分发与防回退 |
| PRODUCTION_AUTHORIZED | NO；仍需单独生产决定 |
