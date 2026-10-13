# Runtime Mapping Readiness (readiness only — nothing is activated)

**Policy registry**: `utils/runtime_mapping_policy.py`
**Eligibility evaluator**: `utils/runtime_mapping_eligibility.py`
**Shadow entry points**: `tools/mapping_governance_shadow.py`
  (`shadow_runtime_eligibility_check`, `shadow_runtime_scenario_matrix`,
  `shadow_runtime_readiness_report`)
**Tests**: `tests/test_runtime_mapping_readiness.py` (RR-01 … RR-28)
**Policy version**: `RUNTIME_MAPPING_POLICY_VERSION = "1.0.0"`
**Evaluator version**: `RUNTIME_ELIGIBILITY_EVALUATOR_VERSION = "1.0.0"`

| Flag | Value |
| :--- | :--- |
| `RUNTIME_MAPPING_ACTIVATION` | `NOT_ENABLED` |
| `PRODUCTION_MAPPING_WRITE_AUTHORIZATION` | `NOT_GRANTED` |
| `RUNTIME_LEDGER_ENFORCEMENT` | `NOT_ENABLED` |
| Historical Canonical migration | `NOT_AUTHORIZED` |
| Production repair | `NOT_AUTHORIZED` |

> This document specifies the **future** runtime gate and validates it in isolation.
> It does not enable mapping, does not modify `refresh()`, does not migrate anything, and
> writes nothing.

---

## 1. Current runtime (unchanged)

The production identity path today is, and remains:

```text
source observation
      |
      v
utils.match_identity.same_event()
      |
      v
utils.ingestion_identity.plan_ingestion_identity() / validate_ingestion_identity()
      |
      v
Canonical ownership  (utils.scraper._refresh_impl -> daily_matches.json)
```

`utils.ingestion_identity.validate_ingestion_identity` is the **sole authoritative runtime
identity guard**. It runs after candidate construction and before *any* downstream
persistence write; it is order-independent, read-only, and fails the whole batch closed on
any identity ambiguity. Nothing in this readiness work replaces, wraps, weakens or is
consulted by it.

`utils/scraper.py` does not import any of the governance, evidence, migration or runtime
readiness modules, and `refresh()` behaviour is byte-for-byte unchanged.

### Why the guard stays authoritative

- It is the only place that decides Canonical ownership, and it does so from `same_event()`
  semantics (strong competition number first, exact home/away pair otherwise).
- It is deterministic and fail-closed, including on non-transitive identity components.
- A mapping layer is an *additional* input at best; it must never become an alternative
  authority. A mapping can only ever **re-point a source alias**, and only where the guard
  already agrees the events are the same.

---

## 2. Future runtime gate

A mapping may participate in runtime identity resolution only when **all** of the following
hold. Every condition is a declarative rule in `RUNTIME_ELIGIBILITY_RULES`; the evaluator
contains no ad-hoc branches.

### 2.1 Identity conditions

| Rule | Requirement |
| :--- | :--- |
| `IDENTITY_KEY_VERSION_CURRENT` | The identity key is on `IDENTITY_KEY_SCHEMA_VERSION = "3.0.0"`. |
| `IDENTITY_KEY_KIND_ALLOWED` | Kind ∈ `{COMPETITION_NUMBER, RAW_PROVIDER_ID}`. |
| `IDENTITY_EVIDENCE_SUFFICIENT` | The key carries sufficient discriminating evidence. |
| `UNVERIFIED_PROVIDER_ID_BLOCKED` | `UNVERIFIED_PROVIDER_ID` is rejected outright. |
| `BLOCKED_IDENTITY_TYPE_REJECTED` | `AMBIGUOUS_SHARED_SOURCE_ID`, `INSUFFICIENT` rejected. |
| `PROVIDER_ID_SCOPE_VERIFIED` | A provider-based key requires a **verified** uniqueness scope. |
| `ROW_POSITION_IDENTITY_BLOCKED` | `ROW_POSITION_UNSTABLE` rejected. |
| `RAW_IDENTIFIER_TYPE_DURABLE` | `ROW_INDEX` / `HTML_ELEMENT_ID` / `NONE` / `UNKNOWN` rejected. |
| `SOURCE_FINGERPRINT_DETERMINISTIC` | A 64-hex semantic observation fingerprint must exist. |
| `IDENTITY_NOT_AMBIGUOUS` | No pending ambiguity for this identity. |
| `NO_COMPETING_CANONICAL_OWNERS` | The source key must not map to several Canonical owners. |

### 2.2 Governance conditions

| Rule | Requirement |
| :--- | :--- |
| `PROPOSAL_PRESENT` | A structurally valid proposal exists. |
| `PROPOSAL_FINGERPRINT_MATCHES` | The recorded proposal fingerprint matches. |
| `PROPOSAL_STATE_ACTIVE` | The recorded state is `APPROVED`. |
| `MAPPING_NOT_REVOKED` / `MAPPING_NOT_SUPERSEDED` | Not revoked, not superseded. |
| `APPROVAL_PRESENT` | An owner approval artifact exists. |
| `TRUST_ANCHOR_PRESENT` | An **explicitly supplied** trusted public key exists. |
| `APPROVAL_VERSION_ALLOWED` | Approval contract `2.0.0` only; legacy contracts are refused. |
| `APPROVAL_PURPOSE_ALLOWED` | Purpose is `CANONICAL_MAPPING_GOVERNANCE_APPROVE`. |
| `APPROVAL_APPROVER_MATCHES` / `APPROVAL_OPERATOR_MATCHES` | Signed approver/operator match the runtime expectation. |
| `APPROVAL_SIGNATURE_VERIFIES` | The Ed25519 signature verifies against the canonical payload (all bindings). |
| `BASELINE_MATCHES` | The approved baseline equals the current baseline. |
| `DATASET_IDENTITY_MATCHES` | The approved dataset identity matches. |
| `APPROVAL_NOT_REPLAYED` | The approval fingerprint has not been used for a different source identity. |

### 2.3 Runtime safety conditions

| Rule | Requirement |
| :--- | :--- |
| `ACTION_ALLOWED` | Action is `BIND_SOURCE_TO_EXISTING_CANONICAL` (no rewrite action exists). |
| `SOURCE_SCOPE_ALLOWED` | `(adapter, namespace)` is a declared source path. |
| `CANONICAL_OWNER_EXISTS_OR_NEW_EVENT` | The target Canonical ID exists, or new-event mode is explicit. |
| `NO_CANONICAL_OWNERSHIP_CONFLICT` | No conflicting Canonical ownership for this source identity. |
| `NO_CANONICAL_ID_CHANGE` | The binding must not require a Canonical ID change. |
| `NO_HISTORICAL_REWRITE` | No historical rewrite requirement. |
| `NO_SNAPSHOT_IMPACT` / `NO_SETTLEMENT_IMPACT` / `NO_EVALUATION_IMPACT` | No dependent-store impact. |
| `NO_MIGRATION_REQUIRED` | No migration requirement. |
| `NO_PENDING_AMBIGUITY` | No pending ambiguity anywhere in the context. |

Result: `ELIGIBLE` iff **no** rejection code was produced; otherwise `NOT_ELIGIBLE` with the
complete, sorted list of rejection codes. There is no third outcome and no override.

### 2.4 Decision object

`RuntimeMappingEligibilityDecision` is immutable, hashable, deterministically serializable,
and exposes **no** mutation or execution method. It answers *"would this mapping be allowed?"*
— never *"this mapping has been applied"*. `evaluated_at` is metadata and is deliberately
excluded from `decision_id`, so the same inputs yield the same decision identity regardless
of the clock (re-evaluation is idempotent).

---

## 3. Approval boundary

- A signed decision is attributed to the identity that **signed** it. The evaluator compares
  the signed `approver`/`operator` against the runtime expectation; the ledger additionally
  enforces `ledger.actor == signed approver` and `signed operator == proposal.operator`.
- An approval is bound to the proposal id, the proposal content fingerprint, the observation
  fingerprint, the source identity scope, the **scoped source identity key**, the candidate
  Canonical ID, the requested action, the dataset identity, the expected baseline, the
  operator, the approver, the timestamp and the purpose.
- The trust anchor must be supplied explicitly. An embedded public key is **never** accepted
  as the trust anchor; if no key is supplied the decision is `NOT_ELIGIBLE`
  (`TRUST_ANCHOR_MISSING`).
- Legacy approval contracts and superseded identity-key contracts are refused, never
  reinterpreted and never silently migrated.
- An approval is **not** permission to rewrite history, and **not** production repair
  authorization.

---

## 4. Rollback model

Activation is designed to be *off by default and trivially revertible*, because nothing in
this phase creates a durable dependency:

| Layer | State | How it would be disabled |
| :--- | :--- | :--- |
| Runtime mapping activation | `NOT_ENABLED` (a declared constant) | Keep the constant at `NOT_ENABLED`; the evaluator is only reachable from an explicit isolated call. |
| Policy registry | declarative, versioned | Revert the registry version; the evaluator fails closed on any incoherent registry at import time. |
| Eligibility evaluator | pure, read-only | Not imported by `scraper.py`; removing the import path removes the capability entirely. |
| Governance ledger | isolated sidecar, outside the repository | Never consulted by `refresh()`; deleting the workspace removes all governance state. |
| Owner approvals | external Ed25519 artifacts | Revoke/supersede through a new governed decision; a revoked mapping is never active. |
| Historical data | untouched | There is nothing to roll back: no Canonical ID, snapshot, settlement or evaluation row is modified by any code path added here. |

Because the evaluator performs no writes, the practical rollback for any future activation is
"stop consulting it" — the data is already unchanged.

---

## 5. Migration separation

Runtime mapping and historical migration are **independent** and must stay so:

- Runtime mapping decides only whether a *source alias* may participate in identity
  resolution. It never rewrites a Canonical ID.
- Historical migration (should it ever be authorized) would rewrite Canonical IDs and every
  dependent reference, which is a strictly larger operation requiring its own manifest,
  owner approval, backups, rollback and postcondition verification
  (`docs/source-id-migration-planning.md` §2).
- Any mapping that *requires* a historical rewrite is rejected by the runtime gate
  (`NO_HISTORICAL_REWRITE`, `NO_CANONICAL_ID_CHANGE`, `NO_MIGRATION_REQUIRED`).
- `snapshot_id` is derived from `match_id`, so rewriting a Canonical ID would make snapshot
  IDs unverifiable. That is why the runtime gate refuses mappings that would require an ID
  change — and why keeping Canonical IDs stable is a precondition, not an implementation
  detail.

---

## 6. Failure modes

| Failure mode | Detected by | Rejection code(s) |
| :--- | :--- | :--- |
| Stale approval (baseline moved) | `BASELINE_MATCHES` + cryptographic verification | `BASELINE_STALE` |
| Ambiguous identity | `IDENTITY_NOT_AMBIGUOUS`, `NO_PENDING_AMBIGUITY`, `BLOCKED_IDENTITY_TYPE_REJECTED` | `IDENTITY_AMBIGUOUS`, `PENDING_AMBIGUITY`, `IDENTITY_TYPE_BLOCKED` |
| Provider collision (same number, other day/sport) | `PROVIDER_ID_SCOPE_VERIFIED`, `IDENTITY_KEY_VERSION_CURRENT` | `PROVIDER_ID_SCOPE_NOT_VERIFIED`, `IDENTITY_KEY_VERSION_NOT_CURRENT` |
| Provider identifier with unknown uniqueness | `UNVERIFIED_PROVIDER_ID_BLOCKED` | `UNVERIFIED_PROVIDER_ID` |
| Row-position identity | `ROW_POSITION_IDENTITY_BLOCKED`, `RAW_IDENTIFIER_TYPE_DURABLE` | `ROW_POSITION_UNSTABLE`, `RAW_IDENTIFIER_TYPE_NOT_DURABLE` |
| Historical reference conflict | `NO_HISTORICAL_REWRITE`, `NO_SNAPSHOT_IMPACT`, `NO_SETTLEMENT_IMPACT`, `NO_EVALUATION_IMPACT` | `HISTORICAL_REFERENCE_CONFLICT`, `SNAPSHOT_IMPACT`, `SETTLEMENT_IMPACT`, `EVALUATION_IMPACT` |
| Revoked approval | `MAPPING_NOT_REVOKED`, `PROPOSAL_STATE_ACTIVE` | `MAPPING_REVOKED`, `MAPPING_STATE_NOT_ACTIVE` |
| Superseded mapping | `MAPPING_NOT_SUPERSEDED` | `MAPPING_SUPERSEDED` |
| Replayed approval | `APPROVAL_NOT_REPLAYED` | `APPROVAL_REPLAYED` |
| Canonical owner conflict | `NO_COMPETING_CANONICAL_OWNERS`, `NO_CANONICAL_OWNERSHIP_CONFLICT` | `CANONICAL_OWNER_CONFLICT`, `CANONICAL_OWNERSHIP_CONFLICT` |
| Missing / unsigned / tampered approval | `APPROVAL_PRESENT`, `APPROVAL_SIGNATURE_VERIFIES` | `APPROVAL_MISSING`, `APPROVAL_SIGNATURE_INVALID` |
| Wrong approver / operator | `APPROVAL_APPROVER_MATCHES`, `APPROVAL_OPERATOR_MATCHES` | `APPROVAL_WRONG_APPROVER`, `APPROVAL_WRONG_OPERATOR` |
| Legacy identity-key / approval contract | `IDENTITY_KEY_VERSION_CURRENT`, `APPROVAL_VERSION_ALLOWED` | `IDENTITY_KEY_VERSION_NOT_CURRENT`, `APPROVAL_LEGACY_VERSION` |

Every failure mode is fail-closed: the decision is `NOT_ELIGIBLE` and no state changes.

---

## 7. Non-goals

- **No activation.** `RUNTIME_MAPPING_ACTIVATION` stays `NOT_ENABLED`; the evaluator is
  reachable only through explicit, isolated calls.
- **No migration.** No Canonical ID, source ID, snapshot, settlement or evaluation row is
  rewritten; no migration executor exists.
- **No production writes.** The evaluator and the readiness manifest write nothing: no
  `data/*.json`, no ledger, no manifest file. `executable` is `False` and any attempt to
  treat the manifest as an activation plan is rejected.
- **No change to `refresh()`**, `same_event()`, the ingestion identity planner, market
  admission, final-score protection, model mathematics or Elo calibration.
- **No automatic approval**, no automatic rebinding, no fuzzy matching, no fallback trust,
  no provider identifier trusted without a verified scope.
- **No production mapping database, no admin UI, no `main` modification, no Git history
  rewrite.**

### CLI (read-only)

```bash
python -m tools.mapping_governance_shadow --demo --workspace /tmp/gov_shadow \
    --no-ledger --runtime-eligibility --runtime-scenarios
```

Without owner keys the valid-approval scenario is reported as `SKIPPED` rather than trusting
an embedded public key; every other scenario still evaluates and must be `NOT_ELIGIBLE`.
