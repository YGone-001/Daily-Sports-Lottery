# Runtime Activation Architecture (design only)

**Design contract**: `utils/runtime_activation_design.py`
**Design version**: `RUNTIME_ACTIVATION_DESIGN_VERSION = "1.0.0"`
**Design guard tests**: `tests/test_runtime_activation_design.py`
**Classification**: architecture design only

| Flag | Value |
| :--- | :--- |
| `ACTIVATION_IMPLEMENTATION_STATUS` | `NOT_IMPLEMENTED` |
| `RUNTIME_MAPPING_ACTIVATION` | `NOT_ENABLED` |
| `PRODUCTION_MAPPING_WRITE_AUTHORIZATION` | `NOT_GRANTED` |
| `ACTIVATION_ENABLE_API` | `None` |
| `RUNTIME_READER_API` | `None` |
| `EMERGENCY_DISABLE_API` | `None` |

> This document defines a **future** architecture. Nothing in it is implemented: no
> activation authorization can be issued, no runtime reader exists, and no runtime
> behaviour changes. `utils/scraper.py` is untouched and never consults any of these
> modules.

---

## 1. Runtime activation boundary

### 1.1 Current production runtime (unchanged)

```text
SOURCE_OBSERVATION
  -> IDENTITY_VALIDATION            (utils.match_identity.same_event)
  -> INGESTION_IDENTITY_PLAN        (utils.ingestion_identity)  <-- authoritative
  -> CANONICAL_OWNERSHIP            (utils.scraper._refresh_impl -> daily_matches.json)
```

`utils.ingestion_identity.validate_ingestion_identity` remains the sole authoritative
runtime identity guard. It is order-independent, read-only, and fails the whole batch closed.

### 1.2 Future controlled pipeline (design only)

```text
SOURCE_OBSERVATION
  -> IDENTITY_VALIDATION
  -> INGESTION_IDENTITY_PLAN        <-- still authoritative, never replaced
  -> MAPPING_PROPOSAL               (implemented: utils.mapping_governance)
  -> OWNER_APPROVAL                 (implemented: Ed25519 approval artifact)
  -> RUNTIME_ELIGIBILITY            (implemented: utils.runtime_mapping_eligibility)
  -> ACTIVATION_AUTHORIZATION       <-- NOT IMPLEMENTED (design only)
  -> RUNTIME_READER                 <-- NOT IMPLEMENTED (design only)
  -> CANONICAL_OWNERSHIP
```

The mapping layer is **additive**. It is a gate that may inform resolution; it never
replaces the ingestion identity planner, and it never changes Canonical ownership rules.

### 1.3 Layer responsibilities

| Layer | Question it answers | Implemented | Artifact | Authority |
| :--- | :--- | :--- | :--- | :--- |
| `ELIGIBILITY` | *Would this mapping be allowed?* | **yes** | `RuntimeMappingEligibilityDecision` | `utils.runtime_mapping_eligibility.evaluate_runtime_mapping_eligibility` |
| `ACTIVATION_AUTHORIZATION` | *May this mapping affect runtime?* | **no** | `RuntimeActivationAuthorization` | `NOT_IMPLEMENTED` |
| `RUNTIME_READER` | *How is an activated mapping consumed read-only?* | **no** | `RuntimeMappingReadView` | `NOT_IMPLEMENTED` |

**Eligibility and activation are separate layers and must stay separate.** An `ELIGIBLE`
decision is a *precondition*, never an authorization:

- the eligibility layer's declared prohibitions include
  `GRANT_ACTIVATION_AUTHORIZATION`, `ACTIVATE_RUNTIME_MAPPING`, `CREATE_BINDING`,
  `MUTATE_DATA`, `WRITE_FILES`, `CONSUME_LEDGER_FOR_WRITE`;
- an `ELIGIBLE` decision is **not** permanent authorization — any material governance or
  Canonical baseline change invalidates it and requires re-evaluation (a changed baseline
  yields a different `decision_id` and a `BASELINE_STALE` rejection);
- activation authorization can never be inferred from eligibility, from an owner approval,
  or from the passage of time.

---

## 2. Runtime reader contract (design only)

| Dimension | Design |
| :--- | :--- |
| **Input contract** | `(current SourceObservation, runtime snapshot id, current Canonical baseline fingerprint)`. The reader never accepts a bare source string and never accepts caller-asserted governance facts. |
| **Trust requirements** | It consumes only: current-contract scoped identity keys, verified Ed25519 owner approvals, a non-expired and non-revoked activation authorization, and a re-checkable eligibility decision. Any missing or non-recheckable item → refuse service (fail closed). It never degrades to source-ID string matching. |
| **Snapshot model** | The reader works from an **immutable runtime snapshot**: `(authorization set, baseline fingerprint, policy version, evaluator version, dataset identity)`. A published snapshot is read-only; changes publish a new snapshot, never mutate the old one. |
| **Cache invalidation** | The cache key is the snapshot fingerprint. Every trigger (new authorization, revocation/expiry, Canonical baseline change, policy or evaluator version change, emergency disable) invalidates the cache and forces a reload. Time-based guessing of validity is forbidden. |
| **Baseline compatibility** | The authorization's bound baseline fingerprint must equal the current runtime baseline fingerprint. Any difference means **incompatible**: the authorization must not be consumed, and both eligibility evaluation and authorization must be repeated. |
| **Failure behaviour** | Every failure (missing snapshot, unverifiable signature, expired/revoked authorization, incompatible baseline, unresolved ambiguity) **refuses service and reports honestly**. It never falls back to unauthorized heuristics and never silently reuses the last successful snapshot. |

**Reader prohibitions** (design-level): `MUTATE_DATA`, `REPAIR_IDENTITIES`,
`CREATE_MAPPINGS`, `WRITE_FILES`, `CONSUME_UNVERIFIED_CONTEXT`,
`FALL_BACK_TO_SOURCE_ID_MATCHING`, `SILENTLY_REUSE_STALE_SNAPSHOT`,
`BLOCK_OR_ALTER_REFRESH`.

---

## 3. Activation authorization model (design only)

### 3.1 Who may authorize

An **authorizer** holding an owner-controlled Ed25519 private key **outside the
repository**. The authorizer may be the same human as the mapping owner, but the artifact
must be **separately issued**: a mapping approval can never be replayed as an activation
authorization, and vice versa.

### 3.2 What artifact represents authorization

`RuntimeActivationAuthorization` — schema `1.0.0`, algorithm `ed25519`, purpose
`RUNTIME_MAPPING_ACTIVATION_AUTHORIZE` (domain-separated from the mapping approval purpose
`CANONICAL_MAPPING_GOVERNANCE_APPROVE` and from the production repair purpose
`PRODUCTION_CANONICAL_DEDUP_APPLY`).

Required bound fields:

```text
proposal_id, proposal_content_fingerprint, scoped_source_identity_key,
candidate_canonical_id, dataset_identity, expected_baseline_fingerprint,
approval_fingerprint,
activation_authorization_schema_version, purpose, activation_id,
eligibility_decision_id, eligibility_evaluator_version, policy_version,
runtime_snapshot_id, authorized_scope, operator, authorizer, authorized_at,
expires_at, revocation_reference, reason
```

The first block is **isomorphic to the owner approval** so the same evidence is bound;
the second block is **activation-specific**: it binds *which* eligibility decision is being
acted upon, *what scope* is granted, *which runtime snapshot* it belongs to, and *when it
expires*.

### 3.3 How authorization differs from approval

| Aspect | Owner approval | Activation authorization |
| :--- | :--- | :--- |
| Question | *Is this mapping **correct**?* | *May this mapping **affect runtime**?* |
| Artifact | Ed25519 mapping approval | `RuntimeActivationAuthorization` |
| Purpose | `CANONICAL_MAPPING_GOVERNANCE_APPROVE` | `RUNTIME_MAPPING_ACTIVATION_AUTHORIZE` |
| Schema | `2.0.0` | `1.0.0` |
| Issuer | Owner (external private key) | Authorizer (must issue separately) |
| Lifetime | No intrinsic expiry; invalidated by baseline change | **Explicit expiry**; expires on schedule |
| Revocation | Via revoke/supersede decision | Revocation reference **and** expiry |
| Effect | None (states the mapping is correct) | Permits read-only consumption within a bounded scope |
| Sufficient alone | No — never a basis for activation | No — still requires eligibility + baseline compatibility |

### 3.4 Expiration model

- Expiry is **mandatory**; an authorization without a valid `expires_at` is invalid.
- A maximum lifetime bound is set by owner policy; the issuing party may not widen it.
- Expiry is re-checked on **every** runtime read against the runtime's controlled clock —
  never against a self-declared "now" inside the artifact.
- Expiry ≠ revocation: an expired authorization is preserved in the audit trail but is
  **never** consumed again.
- Renewal means **issuing a new authorization** with a new `activation_id`; an existing
  artifact is never extended in place.

### 3.5 Rollback of an authorization

- Revocation is expressed as a new, auditable record referencing the `activation_id`.
- Revocation takes effect at the next snapshot publication — it does not wait for expiry.
- A new authorization for the same mapping supersedes the old one; the old artifact is
  retained in the audit trail and is no longer active.
- An activation authorization **never** implies permanent authorization: expiry and
  baseline compatibility are hard constraints.

---

## 4. Audit model (design only)

No events are produced in this phase. The catalogue below is the design contract.

| Event | Recorded when | Key required fields |
| :--- | :--- | :--- |
| `ELIGIBILITY_EVALUATED` | After every runtime eligibility decision (including `NOT_ELIGIBLE`). | `decision_id`, `proposal_id`, `eligibility_result`, `rejection_codes`, `evaluator_version`, `policy_version`, `evidence_provenance` |
| `ACTIVATION_REQUESTED` | Someone requests activation of an `ELIGIBLE` decision. | `activation_id`, `decision_id`, `requested_scope`, `operator` |
| `ACTIVATION_AUTHORIZED` | The authorizer issues an activation authorization. | `activation_id`, `decision_id`, `authorization_fingerprint`, `authorized_scope`, `expires_at`, `authorizer` |
| `ACTIVATION_ENABLED` | An authorization is loaded into a runtime snapshot. | `activation_id`, `runtime_snapshot_id`, `activated_at` |
| `ACTIVATION_DISABLED` | An authorization is disabled (including emergency disable). | `activation_id`, `runtime_snapshot_id`, `reason` |
| `ROLLBACK_EXECUTED` | The runtime rolls back to a previous snapshot. | `from_runtime_snapshot_id`, `to_runtime_snapshot_id`, `trigger`, `deterministic_recovery_fingerprint` |
| `ACTIVATION_AUTHORIZATION_EXPIRED` | An authorization is found expired at read time. | `activation_id`, `expires_at` |
| `RUNTIME_READ_REJECTED` | The reader refuses service. | `activation_id`, `rejection_reason` |

Common fields on every event: `event_id`, `event_type`, `schema_version`, `occurred_at`,
`actor`, `actor_role`, `dataset_identity`, `baseline_fingerprint`.

**Audit store model**: an isolated, append-only JSONL store **outside the repository**,
with previous-entry hash binding. Entries are never deleted and never rewritten. The hash
chain proves **internal consistency only** — it is *not* an external trust anchor, and
immutability ultimately depends on retaining the original signed artifacts plus an
independently trusted signature or checkpoint.

---

## 5. Rollback design (design only)

| Dimension | Design |
| :--- | :--- |
| **Emergency disable** | Set the activation state constant back to `NOT_ENABLED` and publish a new snapshot with an empty authorization set. Single-step, immediate, auditable, and independent of any external service. **Disabling requires no authorization; enabling always does** — a deliberate asymmetry. |
| **Previous runtime snapshot** | Every runtime snapshot retains its full fingerprint and content. Rollback means **pointing at the previous verified snapshot** — never rebuilding, never editing a snapshot in place. |
| **Audit preservation** | Rollback only **appends** audit events (`ACTIVATION_DISABLED`, `ROLLBACK_EXECUTED`). No existing entry is deleted or rewritten. |
| **Deterministic recovery** | Given `(target snapshot fingerprint, dataset identity, baseline fingerprint)` the rollback result must be uniquely determined. After rollback the deterministic recovery fingerprint is recomputed and compared; a mismatch means the rollback failed and the system fails closed. |
| **Historical data** | Rollback **never** rewrites Canonical IDs, snapshots, settlements, evaluation rows or source aliases. Runtime activation only changes *whether a mapping is consumed during resolution*; it never changes persisted history. |
| **Fail-closed default** | If the rollback path itself fails, the system stays **not activated**. Partial application is forbidden. |

Rollback prohibitions: `REWRITE_CANONICAL_IDS`, `REWRITE_HISTORICAL_STORES`,
`DELETE_AUDIT_EVENTS`, `REBUILD_SNAPSHOTS`, `PARTIAL_APPLICATION`, `SILENT_ROLLBACK`.

---

## 6. Testing design (requirements only)

These tests are **future requirements**. All are declared with `implemented = False`; none
is implemented in this phase, because activation does not exist.

| ID | Requirement | Expected outcome |
| :--- | :--- | :--- |
| `AT-01` | Activation requires authorization: an `ELIGIBLE` decision alone never enables a mapping. | Enabling without an activation authorization is rejected (fail closed). |
| `AT-02` | Expired authorization rejected: an `expires_at` earlier than the runtime clock is never consumed. | Service refused; `ACTIVATION_AUTHORIZATION_EXPIRED` recorded. |
| `AT-03` | Rollback deterministic: identical `(target snapshot, dataset, baseline)` yields a unique result. | Recovery fingerprint reproducible; mismatch → rollback failure. |
| `AT-04` | Unauthorized runtime read rejected: missing/revoked authorization or incompatible baseline. | Service refused; `RUNTIME_READ_REJECTED` recorded; never degraded matching. |
| `AT-05` | Audit trail immutable: existing entries cannot be deleted or rewritten. | Any deletion/rewrite is detected (hash-chain break) and fails closed. |
| `AT-06` | Revocation takes effect immediately at the next snapshot. | Post-revocation reads are rejected. |
| `AT-07` | Baseline change invalidates authorization; re-evaluation and re-authorization required. | Baseline-incompatible authorization rejected. |
| `AT-08` | Emergency disable is immediate and requires no authorization. | All reads rejected afterwards; `ACTIVATION_DISABLED` recorded. |
| `AT-09` | The reader never mutates data; read-only must be structurally provable. | The read path contains no write primitive. |
| `AT-10` | Purpose domain separation: mapping approvals, production repair authorizations and activation authorizations are mutually non-replayable. | Replaying any one as another is rejected. |
| `AT-11` | Refresh isolation: activation state never blocks or alters `refresh()`. | `refresh()` output and persistence are identical with activation on or off. |
| `AT-12` | Seven-store integrity: activation, disable and rollback never touch the seven persistent stores. | Byte-identical. |

Design guard tests that **do** run today live in `tests/test_runtime_activation_design.py`.
They verify the design contract itself (coherence, layer separation, artifact separation,
absence of activation APIs, reader prohibitions, audit catalogue, rollback model, future
test requirements) and that no runtime behaviour changed. They do not implement activation.

---

## 7. Non-goals

- No activation authorization issuance, loading or consumption.
- No runtime reader implementation.
- No emergency-disable or rollback execution path.
- No change to `scraper.refresh()`; no runtime ledger consumption.
- No binding creation, no Canonical ID rewrite, no historical store rewrite.
- No source-identity migration, no change to parser ID formats.
- No runtime mapping database, no admin UI, no new production write path.
- No change to prediction models, settlement logic or Elo mathematics.

---

## 8. Status and next steps

| Item | Status |
| :--- | :--- |
| Activation boundary documented | **done** |
| Eligibility vs activation separated | **done** (declared + enforced by import-time assertions and design guard tests) |
| Authorization model defined | **done** |
| Rollback model defined | **done** |
| Audit model defined | **done** |
| Runtime behaviour changed | **no** |
| Existing tests changed | **no** |

Any future activation work must first satisfy the `AT-*` requirements above, and must keep
`RUNTIME_MAPPING_ACTIVATION = NOT_ENABLED` until an owner-issued activation authorization
model, expiry, revocation, audit store and deterministic rollback all exist and are tested.
