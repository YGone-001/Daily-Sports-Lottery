# 生产信任来源所有权与职责登记

## 1. 范围、基线和证据状态

基线 `7c8e28f395cfb0edacd7e1e7055e254d14dbd43b`，分支 `develop`。
**本登记只建立设计契约，不建立生产权威，不默认为任何人分配职责。**
十八类严格沿用前次矩阵 A–R，根、检查点与角色为设计标识，不代表实际服务。
目前确认的生产 operational owner、安全批准者、实施负责人、可用性负责人均为 **NOT_ESTABLISHED**。
提交作者、GitHub 仓库所有者和编写此文档的智能体都不是生产责任接受证据。
本登记仅依据仓库与本次取得的证据，不声称已调查仓库外的组织基础设施或证明其不存在。
仓库内已存在合成 shadow 组件，不能将其 IMPLEMENTED 状态移植到生产实施列。

| 状态 | 必须具备的独立证据 |
| --- | --- |
| DESIGNED | 契约和威胁边界完整 |
| OWNER_ASSIGNED | 实际身份、接受职责记录、联系/升级途径 |
| APPROVED | 具名安全批准与适用版本/范围 |
| IMPLEMENTED | 选定平台上有实际实现 |
| VERIFIED_IN_TEST | 记录版本、输入、负向行为的测试证据 |
| VERIFIED_IN_DEPLOYMENT | 实际部署身份、根、持久状态和故障演练的独立证据 |
| PRODUCTION_AUTHORIZED | 全部前置证据后的单独生产决策 |

状态分别登记，**不存在自动晋级**。下面 JSON 是本 Markdown 的离线评审表，
只由 tests 读取，不被应用加载，不是新配置系统；NO 表示没有达到该生产状态。

## 2. 十八类权威总览

| ID | 权威 | 实际 operational owner | 生产根来源 | 状态 | 未来验收 |
| --- | --- | --- | --- | --- | --- |
| A | 治理账本 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-A |
| B | Canonical 所有权 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-B |
| C | 认证观测 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-C |
| D | owner key 管理 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-D |
| E | activation 签发 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-E |
| F | 可信时间 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-F |
| G | 时间持久高水位 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-G |
| H | supersession | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-H |
| I | 独立撤销与全局当前性 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-I |
| J | 持久快照发布 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-J |
| K | 受控 selector | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-K |
| L | control checkpoint | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-L |
| M | audit checkpoint | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-M |
| N | recovery | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-N |
| O | KMS、轮换和 key revocation | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-O |
| P | 崩溃耐久性 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-P |
| Q | 监控与事件响应 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-Q |
| R | 部署策略强制 | NOT_ESTABLISHED | NOT_ESTABLISHED | DESIGNED | PA-R |

每条记录完整保留所有必填字段；空角色列表表示该类别不独立签名，不能因此豁免其依赖的签名根。
checkpoint_role 是拟议签发职责，checkpoint_publisher 是实际实体；两者不得混淆。
production_evidence 为空，明确禁止将下面的代码定位当作真实 owner 或根来源的证据。

## 3. 所有权与角色的结构化评审登记

```json
{
  "contract_id": "production-authority-ownership-v1",
  "states": [
    "DESIGNED",
    "OWNER_ASSIGNED",
    "APPROVED",
    "IMPLEMENTED",
    "VERIFIED_IN_TEST",
    "VERIFIED_IN_DEPLOYMENT",
    "PRODUCTION_AUTHORIZED"
  ],
  "authorities": [
    {
      "authority_id": "A",
      "name": "治理账本",
      "authoritative_dataset_and_scope": "完整治理历史、owner 批准、当前提案/绑定、签名 view 与 ledger checkpoint",
      "trust_boundary": "生产 治理账本 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "当前账本已提交前缀与独立 checkpoint 精确一致；签发前 currentness fence",
      "continuity_guarantee": "账本 count/tip、批准事实指纹和发布序号持久单调",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "ledger-checkpoint",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/mapping_governance.py",
          "symbol": "GovernanceLedger"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-A"
    },
    {
      "authority_id": "B",
      "name": "Canonical 所有权",
      "authoritative_dataset_and_scope": "dataset 身份、完整 Canonical/owner inventory、当前 baseline 清单",
      "trust_boundary": "生产 Canonical 所有权 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "canonical-publisher",
        "inventory-attestor"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "同一受控数据快照重算 baseline，完整性证明与发布版本一致",
      "continuity_guarantee": "baseline 版本、inventory 根和发布序号持久化，禁止回退",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "canonical-publisher",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/authoritative_governance.py",
          "symbol": "build_authoritative_governance_view"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-B"
    },
    {
      "authority_id": "C",
      "name": "认证观测",
      "authoritative_dataset_and_scope": "adapter/provider、原始采集证据与 scope v3 观测证明",
      "trust_boundary": "生产 认证观测 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "observation"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "采集/签发时间与可信 reference 核验；原始内容错误独立处置",
      "continuity_guarantee": "保存 evidence ID、原始证据摘要与 schema 版本；重启不重复接纳",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "observation",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/observation_authority.py",
          "symbol": "verify_observation_attestation"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-C"
    },
    {
      "authority_id": "D",
      "name": "owner key 管理",
      "authoritative_dataset_and_scope": "owner 身份注册、授权范围、历史批准原件",
      "trust_boundary": "生产 owner key 管理 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "owner-approval"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "批准签发者须在对应时期合法注册；当前授权另查吊销与治理状态",
      "continuity_guarantee": "版本化身份/密钥历史、批准原文和失陷时间线不可删除",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "owner-approval",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/mapping_governance.py",
          "symbol": "audit_historical_approvals"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-D"
    },
    {
      "authority_id": "E",
      "name": "activation 签发",
      "authoritative_dataset_and_scope": "当前认证批准到限定用途/范围/寿命的 activation",
      "trust_boundary": "生产 activation 签发 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "activation"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "只对当前 view 与批准指纹签发；每次消费重查撤销/期限",
      "continuity_guarantee": "唯一 activation ID、签发审计和完整授权 inventory 持久化",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "activation",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/runtime_activation_authorization.py",
          "symbol": "StrictActivationVerifier"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-E"
    },
    {
      "authority_id": "F",
      "name": "可信时间",
      "authoritative_dataset_and_scope": "签名时间与独立 freshness reference",
      "trust_boundary": "生产 可信时间 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "time"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "认证时间分发；独立 reference 不来自请求或 token",
      "continuity_guarantee": "时间/generation/evidence 高水位须在消费前耐久化",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "time",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/trusted_time_authority.py",
          "symbol": "TrustedTimeVerifier"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-F"
    },
    {
      "authority_id": "G",
      "name": "时间持久高水位",
      "authoritative_dataset_and_scope": "跨重启时间、reference、generation、根版本与 recovery epoch",
      "trust_boundary": "生产 时间持久高水位 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "已落盘高水位与独立最新 checkpoint 一致才允许建新会话",
      "continuity_guarantee": "不可随普通备份倒退；丢失、回滚或写入失败停用",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "time",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/trusted_time_authority.py",
          "symbol": "TrustedTimeVerifier"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-G"
    },
    {
      "authority_id": "H",
      "name": "supersession",
      "authoritative_dataset_and_scope": "完整授权、显式 A→B、CURRENT 及独立 lifecycle checkpoint",
      "trust_boundary": "生产 supersession → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "唯一 CURRENT；不得按时间/ID/插入顺序推断",
      "continuity_guarantee": "全量事件、授权 inventory、state sequence/tip 持久单调",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "lifecycle-checkpoint",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/activation_supersession.py",
          "symbol": "ActivationSupersessionVerifier"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-H"
    },
    {
      "authority_id": "I",
      "name": "独立撤销与全局当前性",
      "authoritative_dataset_and_scope": "撤销事件、身份专属目标、完整状态和独立 checkpoint",
      "trust_boundary": "生产 独立撤销与全局当前性 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "revocation",
        "revocation-current",
        "revocation-checkpoint"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "认证当前视图必须覆盖所有已提交撤销；故障拒绝消费",
      "continuity_guarantee": "事件和授权清单不缩减；sequence、tip、epoch 持久单调",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "revocation-checkpoint",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/activation_revocation_authority.py",
          "symbol": "IndependentRevocationVerifier"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-I"
    },
    {
      "authority_id": "J",
      "name": "持久快照发布",
      "authoritative_dataset_and_scope": "不可变 envelope、原始证据、发布命令/逻辑提交",
      "trust_boundary": "生产 持久快照发布 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "publisher"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "发布不授予选择权；无控制提交文件不可消费",
      "continuity_guarantee": "内容寻址、不覆盖历史；inventory 与控制 checkpoint 一致",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "publisher",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/persistent_shadow_snapshots.py",
          "symbol": "PersistentShadowSnapshotStore"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-J"
    },
    {
      "authority_id": "K",
      "name": "受控 selector",
      "authoritative_dataset_and_scope": "操作者身份、SELECT/ROLLBACK/RECOVER 与 CAS",
      "trust_boundary": "生产 受控 selector → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "selector"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "精确前态 CAS；当前全链验证及提交后 checkpoint",
      "continuity_guarantee": "request ID、selection sequence、generation 与前后态持久保留",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "selector",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/shadow_snapshot_control.py",
          "symbol": "PersistentShadowSnapshotController"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-K"
    },
    {
      "authority_id": "L",
      "name": "control checkpoint",
      "authoritative_dataset_and_scope": "journal/audit/pointer/inventory 联合当前证明",
      "trust_boundary": "生产 control checkpoint → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "control-checkpoint"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "独立直接读取各存储，不能仅签 writer 提交的摘要",
      "continuity_guarantee": "签发方持久 sequence/tip；新 challenge 不允许旧状态重签",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "control-checkpoint",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/shadow_snapshot_control.py",
          "symbol": "PersistentShadowSnapshotController"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-L"
    },
    {
      "authority_id": "M",
      "name": "audit checkpoint",
      "authoritative_dataset_and_scope": "完整审计前缀、store 身份、tip/count、独立当前证明",
      "trust_boundary": "生产 audit checkpoint → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "audit-checkpoint"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "独立审计读取同一已提交副本；拒绝适配器身份替换",
      "continuity_guarantee": "外部保留锚防截尾/整链重写；签发高水位跨重启保持",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "audit-checkpoint",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/shadow_activation_audit_store.py",
          "symbol": "ShadowActivationAuditStore"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-M"
    },
    {
      "authority_id": "N",
      "name": "recovery",
      "authoritative_dataset_and_scope": "停用态对账、恢复完成事件、指针、独立恢复证据",
      "trust_boundary": "生产 recovery → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "recovery"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "NONE 差异、当前 checkpoint 和全链均有效；恢复保持停用",
      "continuity_guarantee": "recovery epoch、store/tip/count/selection/generation 不可回退",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "recovery",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/recovery_authority.py",
          "symbol": "build_recovery_evidence"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-N"
    },
    {
      "authority_id": "O",
      "name": "KMS、轮换和 key revocation",
      "authoritative_dataset_and_scope": "根注册、角色签名授权、轮换/失陷治理",
      "trust_boundary": "生产 KMS、轮换和 key revocation → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [
        "root-approval"
      ],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "固定部署根、用途/环境/dataset 绑定；未知根拒绝",
      "continuity_guarantee": "根版本和撤销历史受保护；根变更双独立批准",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "root-approval",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/trusted_artifacts.py",
          "symbol": "verify_trusted_artifact"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-O"
    },
    {
      "authority_id": "P",
      "name": "崩溃耐久性",
      "authoritative_dataset_and_scope": "实际平台提交边界、备份、恢复、目录持久性",
      "trust_boundary": "生产 崩溃耐久性 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "UNCERTAIN 不推定成功；平台断电验收后方可声明耐久",
      "continuity_guarantee": "控制日志、审计、指针联合对账，历史字节原样保全",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "control-checkpoint",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/shadow_snapshot_control.py",
          "symbol": "PersistentShadowSnapshotController"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-P"
    },
    {
      "authority_id": "Q",
      "name": "监控与事件响应",
      "authoritative_dataset_and_scope": "告警、责任轮值、应急停用、证据保留",
      "trust_boundary": "生产 监控与事件响应 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "撤销/时间/checkpoint 不可达触发拒绝与告警",
      "continuity_guarantee": "独立告警投递、停用标记与事故证据跨重启保留",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "audit-checkpoint",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "utils/shadow_runtime_reader.py",
          "symbol": "ShadowRuntimeReader"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-Q"
    },
    {
      "authority_id": "R",
      "name": "部署策略强制",
      "authoritative_dataset_and_scope": "固定信任配置、环境、严格 Reader 工厂与发布准入",
      "trust_boundary": "生产 部署策略强制 → 固定配置的只读 verifier；请求调用方不属于权威",
      "operational_owner": "NOT_ESTABLISHED",
      "security_approver": "NOT_ESTABLISHED",
      "implementation_owner": "NOT_ESTABLISHED",
      "availability_owner": "NOT_ESTABLISHED",
      "source_of_truth_system": "NOT_ESTABLISHED",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_identity": "NOT_ESTABLISHED",
      "signing_key_role": [],
      "public_key_trust_anchor": "NOT_ESTABLISHED",
      "key_rotation_authority": "NOT_ESTABLISHED",
      "key_revocation_authority": "NOT_ESTABLISHED",
      "currentness_guarantee": "请求不可更换根/ledger/模式；启动验证且默认停用",
      "continuity_guarantee": "部署版本、最低根版本、策略版本和禁用状态持久防回退",
      "checkpoint_publisher": "NOT_ESTABLISHED",
      "checkpoint_role": "root-approval",
      "storage_location": "NOT_ESTABLISHED",
      "recovery_ownership": "NOT_ESTABLISHED",
      "audit_retention_policy": "POLICY_NOT_APPROVED",
      "status": "DESIGNED",
      "progress": {
        "DESIGNED": "YES",
        "OWNER_ASSIGNED": "NO",
        "APPROVED": "NO",
        "IMPLEMENTED": "NO",
        "VERIFIED_IN_TEST": "NO",
        "VERIFIED_IN_DEPLOYMENT": "NO",
        "PRODUCTION_AUTHORIZED": "NO"
      },
      "supporting_evidence": [
        {
          "path": "app.py",
          "symbol": "api_refresh"
        }
      ],
      "production_evidence": [],
      "acceptance_test": "PA-R"
    }
  ],
  "signer_roles": [
    {
      "role_id": "root-approval",
      "purpose": "ROOT_CONFIGURATION_APPROVAL",
      "authority_id": "O",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "deploy-admission",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:root-approval",
      "independent_from": [
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "canonical-publisher",
      "purpose": "CANONICAL_BASELINE_MANIFEST",
      "authority_id": "B",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "governance-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:canonical-publisher",
      "independent_from": [
        "root-approval",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "inventory-attestor",
      "purpose": "CANONICAL_OWNERSHIP_COMPLETENESS",
      "authority_id": "B",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "governance-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:inventory-attestor",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "owner-approval",
      "purpose": "CANONICAL_MAPPING_GOVERNANCE_APPROVE",
      "authority_id": "D",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "historical-approval-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:owner-approval",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "governance-view",
      "purpose": "GOVERNANCE_VIEW_ATTESTATION",
      "authority_id": "A",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "eligibility-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:governance-view",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "ledger-checkpoint",
      "purpose": "GOVERNANCE_LEDGER_CHECKPOINT",
      "authority_id": "A",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "governance-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:ledger-checkpoint",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "governance-revocation",
      "purpose": "ACTIVATION_REVOCATION_MANIFEST",
      "authority_id": "A",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "governance-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:governance-revocation",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "observation",
      "purpose": "SOURCE_OBSERVATION_ATTESTATION",
      "authority_id": "C",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "observation-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:observation",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "activation",
      "purpose": "RUNTIME_MAPPING_ACTIVATION_AUTHORIZE",
      "authority_id": "E",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "activation-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:activation",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "time",
      "purpose": "RUNTIME_ACTIVATION_TIME_ATTESTATION",
      "authority_id": "F",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "time-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:time",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "lifecycle",
      "purpose": "RUNTIME_ACTIVATION_SUPERSESSION",
      "authority_id": "H",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "lifecycle-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:lifecycle",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "lifecycle-current",
      "purpose": "RUNTIME_ACTIVATION_LIFECYCLE_STATE",
      "authority_id": "H",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "lifecycle-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:lifecycle-current",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "lifecycle-checkpoint",
      "purpose": "RUNTIME_ACTIVATION_LIFECYCLE_CHECKPOINT",
      "authority_id": "H",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "lifecycle-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:lifecycle-checkpoint",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "revocation",
      "purpose": "RUNTIME_ACTIVATION_REVOCATION",
      "authority_id": "I",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "revocation-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:revocation",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "revocation-current",
      "purpose": "RUNTIME_ACTIVATION_REVOCATION_STATE",
      "authority_id": "I",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "revocation-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:revocation-current",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "revocation-checkpoint",
      "purpose": "RUNTIME_ACTIVATION_REVOCATION_CHECKPOINT",
      "authority_id": "I",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "revocation-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:revocation-checkpoint",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "publisher",
      "purpose": "SHADOW_SNAPSHOT_PUBLICATION_REQUEST",
      "authority_id": "J",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "publication-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:publisher",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "selector",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "selector",
      "purpose": "SHADOW_SNAPSHOT_SELECTION_REQUEST",
      "authority_id": "K",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "control-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:selector",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "control-checkpoint",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "control-checkpoint",
      "purpose": "SHADOW_SNAPSHOT_CONTROL_CHECKPOINT",
      "authority_id": "L",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "control-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:control-checkpoint",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "audit-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "audit-checkpoint",
      "purpose": "SHADOW_AUDIT_CHAIN_CHECKPOINT",
      "authority_id": "M",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "audit-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:audit-checkpoint",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "recovery"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    },
    {
      "role_id": "recovery",
      "purpose": "SHADOW_RECOVERY_EVIDENCE",
      "authority_id": "N",
      "issuer_identity": "NOT_ESTABLISHED",
      "verifier_role": "recovery-verifier",
      "verifier_identity": "NOT_ESTABLISHED",
      "trust_root_id": "root:recovery",
      "independent_from": [
        "root-approval",
        "canonical-publisher",
        "inventory-attestor",
        "owner-approval",
        "governance-view",
        "ledger-checkpoint",
        "governance-revocation",
        "observation",
        "activation",
        "time",
        "lifecycle",
        "lifecycle-current",
        "lifecycle-checkpoint",
        "revocation",
        "revocation-current",
        "revocation-checkpoint",
        "publisher",
        "selector",
        "control-checkpoint",
        "audit-checkpoint"
      ],
      "separation": "DISTINCT_KEY_AND_SERVICE_PRINCIPAL; issuer cannot approve own root or verify own operational decision",
      "historical_verification": "versioned public key and approval provenance retained; validity is not current authorization",
      "status": "DESIGNED"
    }
  ],
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

## 4. 接受职责、分离和运营审批

OWNER_ASSIGNED 需记录实际主体身份、职责范围、环境/dataset、任期、替补、事故联络和签署接受记录。
安全批准者独立核验 owner 的权限；实施者不能独自批准自己的部署；issuer 与 verifier 使用不同服务身份。
每个签名角色独立密钥与服务权限，根变更至少由 root custodian 与独立安全审批角色共同批准，
两者均尚未建立。角色分离不得通过在同一不受限进程中创建多个对象来证明。
对同一类别中的多个 signer（例如 lifecycle state 与 lifecycle checkpoint），仍要求独立职责和根。
如未来要合并角色，必须先修改风险契约、提供独立评审并重新验收，不能照搬合成夹具的共享 checkpoint key。

恢复操作需 operational recovery owner 请求、独立 security approver 批准，控制 selector 执行签名 RECOVER，
独立 audit/checkpoint issuer 复核，随后另行批准显式再启用。恢复签名者不能自行批准恢复或回退根。
紧急停用允许经已认证的最小权限运维身份立即触发，审计不可达也须先停止消费；再启用无紧急豁免。
实际审批者、职责接受、保留时长与存储位置缺失时，实施状态为 **BLOCKED**。

关联：[根引导](runtime-activation-production-trust-bootstrap.md)、
[部署契约](runtime-activation-production-deployment-contract.md)、
[验收门禁](runtime-activation-production-acceptance-gates.md)。
**PRODUCTION_ACTIVATION_READY = NO；DECISION = NO-GO。**
