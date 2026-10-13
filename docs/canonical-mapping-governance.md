# Canonical Mapping Governance

**Module**: `utils/mapping_governance.py`
**Shadow entry point**: `tools/mapping_governance_shadow.py`
**Tests**: `tests/test_canonical_mapping_governance.py` (PG-09 … PG-30, MC-01 … MC-27)
**Governance schema version**: `GOVERNANCE_SCHEMA_VERSION = "1.0.0"`
**Proposal schema version**: `PROPOSAL_SCHEMA_VERSION = "2.0.0"`
**Approval schema version**: `MAPPING_APPROVAL_SCHEMA_VERSION = "2.0.0"`
**Identity-key schema version**: `IDENTITY_KEY_SCHEMA_VERSION = "3.0.0"`
  (superseded `2.0.0`; legacy `1.0.0` = scope-only — both read-only)
**Production mapping write authorization**: `NOT_GRANTED`
**Runtime ledger enforcement**: `NOT_ENABLED`

---

## 1. Purpose and trust boundaries

Six distinct things must never be confused. Each is a separate trust boundary:

```text
  A. what the provider actually supplied        -> raw_provider_id
  B. what the existing parser derived           -> derived_source_id
  C. what the ingestion plan concluded          -> utils.ingestion_identity (runtime authority)
  D. what an operator proposed                 -> MappingProposal  (status PROPOSED)
  E. what a trusted owner explicitly approved   -> Ed25519 approval artifact
  F. what may eventually be eligible for
     runtime Canonical mapping integration      -> NOT REACHABLE IN THIS PHASE
```

Hard invariants:

> - A source observation is **not** an authorization to bind or modify a Canonical identity.
> - A proposal is **not** an approved mapping.
> - An approved mapping is **not** permission to rewrite historical Canonical data.
> - A recorded decision is attributed to the identity that **signed** it, never to whoever
>   happens to submit the transition.
> - A hash chain is **not** a signature, and a signature is **not** a SHA-256.

This phase builds and validates governance with **synthetic, isolated data only**.

### What is implemented vs. not

| Capability | Status |
| :--- | :--- |
| Provenance model, stability classification, conflict classification | **implemented** |
| Raw provider evidence inventory + opt-in, offline evidence extraction | **implemented** (`utils/provider_evidence.py`) |
| Proposal contract + explicit state machine | **implemented** |
| Scoped source identity key (distinct from scope metadata) | **implemented** |
| Provider ID uniqueness scope (supplied ≠ unique ≠ stable) | **implemented** |
| Ed25519 owner approval, bound to the identity key, with actor/operator attribution | **implemented** |
| Retained approval evidence + independent historical approval audit | **implemented** |
| Isolated append-only hash-chained ledger + structural verification | **implemented** |
| Read-only shadow validation entry point | **implemented** |
| Sport-scoped market source ID contract (design only, not wired into parsers) | **documented only** (`docs/source-id-migration-planning.md`) |
| Read-only migration impact manifest | **implemented** (`utils/source_id_migration.py`) |
| Runtime mapping enforcement / production mapping writes | **NOT implemented (not authorized)** |
| Historical Canonical migration / migration executor | **NOT implemented (not authorized)** |

---

## 2. MappingProposal schema

`PROPOSAL_SCHEMA_VERSION = "2.0.0"`. Required: proposal ID (content-derived), schema version, observation reference, source identity scope, **scoped source identity key**, candidate Canonical ID, existing owner, identity evidence type, observation fingerprints, ingestion-plan reference, reason, conflict category, expected baseline fingerprint, requested action, status, operator. Actions: `BIND_SOURCE_TO_EXISTING_CANONICAL`, `REGISTER_NEW_SOURCE_IDENTITY`, `REJECT_IDENTITY_CLAIM`, `FLAG_AMBIGUOUS_IDENTITY`, `NO_ACTION`.

> A proposal never executes its action. There is **no** action that rewrites an existing
> Canonical ID; `validate_proposal` rejects any action containing `REWRITE`, `RENAME`,
> `MIGRATE`, `DELETE` or `MERGE_CANONICAL`.

One source identity claim cannot silently acquire multiple Canonical owners; one approved
source key cannot map to different owners without a separately governed supersession
decision.

### 2.1 Scoped source identity key (scope is not identity)

`SourceIdentityScope` (`adapter`, `sport`, `event_date`, `provider_namespace`,
`raw_identifier_type`) describes a **domain**. It does **not** identify one event. Two
genuinely distinct events — e.g. jczq `001` and `002` on the same sport/day — share an
identical scope tuple, so a binding key built only from scope metadata would collide and
falsely reject one of them.

`ScopedSourceIdentityKey` therefore adds **discriminating identity evidence**, versioned by
`IDENTITY_KEY_SCHEMA_VERSION = "3.0.0"`:

| Kind | `identity_value` | Sufficient for an active binding |
| :--- | :--- | :--- |
| `COMPETITION_NUMBER` | `sport\|date\|jczq\|normalized_number` | **yes** — the bare number is never used without sport and date scoping |
| `RAW_PROVIDER_ID` | `adapter\|namespace\|sport\|date\|raw_type\|uniqueness_scope\|raw_provider_id` | **yes** — only when the provider actually supplied it **and** its uniqueness scope is independently documented (`VERIFIED_SCOPED`) |
| `UNVERIFIED_PROVIDER_ID` | the same tuple, with `uniqueness_scope` ∈ {`UNVERIFIED`, `UNKNOWN`} | **no** — recorded and reviewable, but never an active binding |
| `AMBIGUOUS_SHARED_SOURCE_ID` | the disputed shared source string + member scopes | **no** — review only |
| `INSUFFICIENT` | the derived source ID as observed | **no** — row-position-derived, meaning-unconfirmable or synthetic evidence |

> The `provider_id_uniqueness_scope` is part of the key payload and therefore part of what an
> owner signature binds. A provider identifier that is merely *supplied* is never treated as
> globally unique: it lands in `UNVERIFIED_PROVIDER_ID` and cannot authorize a binding.

The `RAW_PROVIDER_ID` value carries `sport` and `date`, so the **same** provider number on a
different date or in a different sport no longer collapses into one binding identity.

Fail-closed rules:

- A **binding** action requires `identity_key.is_sufficient`; otherwise
  `ProposalValidationError` (ambiguous / unstable / position-derived / unverified-provider
  observations can still be *observed* and sent to `REVIEW_REQUIRED`, but never actively bound).
- A provider identifier that was never supplied is never inferred into the key.
- An HTML element `id` is never promoted to a stable provider identifier.
- Row-position identifiers are never treated as durable mapping authority.
- Two distinct events → two distinct keys; repeated observations of one identity → one key.
- Football and basketball remain separate identity scopes; the same number on different
  dates stays distinct; different numbers on the same sport/date stay distinct.
- The key never depends on observation order.

### 2.2 Schema compatibility across key versions

| Version | Meaning | Treatment |
| :--- | :--- | :--- |
| `3.0.0` (current) | provider identity value carries sport/date/uniqueness scope; `UNVERIFIED_PROVIDER_ID` exists | authoritative |
| `2.0.0` (superseded) | provider identity value lacked sport/date/uniqueness scope — the same raw number could collide across dates and sports | read-only; `classify_binding_key` → `LEGACY_VERSION`, refused wherever active authority is requested |
| `1.0.0` (legacy, absent marker) | scope-only metadata, no discriminating evidence | read-only; `LEGACY_SCOPE_ONLY` |

An approval signed under an older key contract is **historical evidence only**. It is never
silently migrated, reinterpreted, or reused to authorize a new-format key; a fresh owner
decision under the current contract is required.

---

## 3. State machine

`ALLOWED_TRANSITIONS` is the **single authoritative registry**. There is no generic force
transition (`GovernanceLedger.append` has no `force` parameter — asserted by PG-13).

| From | Allowed to |
| :--- | :--- |
| `OBSERVED` | `PROPOSED`, `REJECTED` |
| `PROPOSED` | `REVIEW_REQUIRED`, `APPROVED`, `REJECTED` |
| `REVIEW_REQUIRED` | `APPROVED`, `REJECTED` |
| `APPROVED` | `REVOKED`, `SUPERSEDED` |
| `REJECTED` | `SUPERSEDED` |
| `REVOKED` | `SUPERSEDED` |
| `SUPERSEDED` | *(terminal)* |

Guarantees:

1. `OBSERVED` does not mean `PROPOSED` — a proposal must be recorded explicitly.
2. `PROPOSED` does not mean `APPROVED`.
3. `REVIEW_REQUIRED` does not authorize mutation.
4. `REJECTED` cannot become `APPROVED` by editing the same stored object.
5. `REVOKED` mappings are never active (`ACTIVE_STATES = {APPROVED}`).
6. `SUPERSEDED` retains historical evidence; the ledger is append-only.
7. Transitions are validated against the registry.
8. Every transition is attributable (`actor`, `actor_role`).
9. Full transition history remains inspectable (`history_of`).
10. State changes never erase evidence — entries are never deleted.

> Conflict governance rules never target `APPROVED`; the module asserts this at import.

---

## 4. Owner approval trust boundary

Established primitives only: **RFC 8032 Ed25519** via `cryptography` (PyCA). No bespoke
cryptography. Private key must live **outside** the repository.

Domain separation:

| Purpose | Value |
| :--- | :--- |
| Mapping approval | `CANONICAL_MAPPING_GOVERNANCE_APPROVE` |
| Production Canonical repair | `PRODUCTION_CANONICAL_DEDUP_APPLY` |

> A production repair authorization artifact can **never** be replayed as a mapping
> approval, and vice versa (verified in both directions by PG-20 and PG-27).

The signed payload is the artifact minus its `approval` block, canonically serialized, so a
signature binds **every** declared field:

`proposal_id`, `proposal_content_fingerprint`, `source_observation_fingerprint`,
`source_identity_scope`, **`scoped_source_identity_key`**, `candidate_canonical_id`,
`requested_action`, `dataset_identity`, `expected_baseline_fingerprint`, `operator`,
`approver`, `approved_at`, `reason`, `purpose`.

### 4.1 Actor attribution

A signed decision is attributed to the identity that signed it. For every `APPROVED`
transition the ledger enforces:

```text
ledger.actor              == signed_approval.approver
signed_approval.operator  == proposal.operator
signed_approval.approver  != signed_approval.operator
```

Verified cryptographically against the complete approved payload, **before** any ledger
append. Refused (fail closed):

- missing / unsigned / empty-signature approval attempts;
- an approval whose `purpose` is not the mapping purpose;
- an approval under a **legacy approval contract** (schema other than `2.0.0` — never
  silently migrated or reinterpreted);
- any tampered field (signature verification fails);
- unavailable, malformed or non-Ed25519 trusted public keys;
- an approval produced by an untrusted signing key;
- binding mismatches (proposal, content fingerprint, observation fingerprint, scope,
  **scoped source identity key**, candidate Canonical ID, action, dataset identity);
- `approver == operator`, blank approver or blank operator;
- **wrong ledger actor** (`ActorBindingError`: `owner-B` cannot record a decision signed
  by `owner-A`);
- **wrong signed operator** (`ActorBindingError`: the approval cannot be attributed to an
  operator other than the proposal's operator);
- a **stale baseline**: if the Canonical baseline fingerprint changed after proposal
  creation, the approval cannot be reused without revalidation (`StaleBaselineError`).

> `actor_role="owner"` alone proves nothing about *which* owner decided.

> Mapping approval **never** invokes production repair and never rewrites Canonical files.

---

## 5. Isolated governance ledger

`GovernanceLedger` is an append-only, hash-chained JSONL sidecar.

| Requirement | Implementation |
| :--- | :--- |
| Explicit storage configuration | Path is mandatory; there is **no default** |
| Isolated workspace only | Any path inside the repository (including `data/`) is refused (`LedgerPathError`) |
| Never written by `refresh()` | `utils/scraper.py` contains no reference to the governance layer (PG-30) |
| Append-only history | Entries are never deleted or rewritten |
| Deterministic serialization | `canonical_json` per line |
| Previous-entry hash binding | `previous_entry_hash` / `entry_hash`; genesis = 64 zeros |
| Atomic writes | same-directory temp file + `fsync` + `os.replace` |
| Concurrency safety | dedicated cross-process + cross-thread lock (independent of the refresh lock); acquisition retries against an explicit deadline (`LEDGER_LOCK_TIMEOUT_SECONDS`) rather than relying on the platform primitive's fixed retry window |
| Reject malformed transitions | `validate_transition` + first-transition/declared-status consistency |
| Reject conflicting active bindings | one source key cannot be actively bound to two different Canonical IDs |
| Detect broken hash chains | `verify()` recomputes every hash, sequence and identity |
| Detect replayed transition identities | `replay_key` uniqueness + stale-tip binding |
| Retained approval evidence | every `APPROVED` entry carries the full signed artifact **and** the immutable proposal evidence inside the hashed entry |
| Legacy binding-key compatibility | `classify_binding_key` distinguishes `CURRENT` / `LEGACY_SCOPE_ONLY` / `LEGACY_VERSION` / `UNKNOWN`; legacy keys are never reinterpreted and never authorize an active binding (`LegacyBindingKeyError`) |
| Preserve rejected / superseded evidence | append-only |
| Read-only verification | `verify()` never writes |

Identity model (three derived hashes per entry, all recomputed by `verify()`):

| Hash | Covers | Purpose |
| :--- | :--- | :--- |
| `decision_fingerprint` | decision fields **excluding** `occurred_at` | exact duplicate submission → idempotent no-op |
| `transition_id` | decision fields + `from_state` | recorded transition identity |
| `replay_key` | proposal, target state, actor, role, approval, baseline | same decision re-submitted with different content → `LedgerReplayError` |

> **Trust-anchor honesty**: the hash chain proves *internal consistency* against accidental
> or detectable tampering. It is **not** an external trust anchor and must not be described
> as tamper-proof. Truncating the tail yields a shorter but self-consistent chain; that is
> precisely why an independently trusted signature or checkpoint would be required. This is
> asserted explicitly in PG-14 / MC-14 and reported by `verify()["trust_anchor"]`.

### 5.1 Structural verification vs. authenticated approval audit

Two deliberately separate contracts (never conflated):

| Category | What it establishes | Requires |
| :--- | :--- | :--- |
| `STRUCTURAL_LEDGER_INTEGRITY` (`ledger.verify()`) | sequence integrity, previous-entry hashes, entry hashes, replay checks, ledger structure, binding-key contract histogram | nothing |
| `AUTHENTICATED_MAPPING_APPROVAL_AUDIT` (`audit_historical_approvals`) | the actual Ed25519 signature verifies; the signed payload binds the proposal, the scoped source identity key, the observation evidence, the Canonical target, the mapping purpose, the operator and approver identities, the recorded transition and the original approved baseline; the recorded actor matches the signed approver | an **explicitly trusted public key** + retained evidence |
| `CURRENT_MAPPING_ELIGIBILITY` (`mapping_eligibility`) | which historical approvals are still executable against the **current** baseline | a current baseline fingerprint |

Fail-closed rules for the authenticated audit:

- Missing trusted public key → `SignatureVerificationError`; an embedded public key is
  **never** accepted as the trust anchor.
- An `APPROVED` entry without retained `approval_artifact` **or** `proposal_evidence` →
  `MissingApprovalEvidenceError` (new records can never be incomplete; `verify()` also
  exposes `incomplete_approved_entries()` structurally).
- The retained artifact must reproduce the recorded `approval_fingerprint`, and the
  retained proposal evidence must reproduce the recorded proposal identity and content
  fingerprint; otherwise fail closed.
- Failures preserve their **original** exception type with the entry sequence attached.

### 5.2 Historical authenticity vs. current eligibility

`audit_historical_approvals` verifies each decision against the **recorded** baseline, so a
historical approval remains valid *evidence of its original decision* even after the current
Canonical baseline changes. `mapping_eligibility` separately reports which of those are
still executable now. A valid old signature **never** automatically authorizes action
against a new baseline.

### 5.3 Schema compatibility boundary

The binding-key contract moved from scope-only metadata (`IDENTITY_KEY_SCHEMA_VERSION`
absent = legacy `1.0.0`) to discriminating identity evidence (`2.0.0`), and then to
scope-carrying provider identity evidence (`3.0.0`), while the approval contract gained
`scoped_source_identity_key` (`MAPPING_APPROVAL_SCHEMA_VERSION` `1.0.0` → `2.0.0`).
Consequences:

- Legacy artifacts are **not** silently migrated, reinterpreted, or reused for a new
  identity-key meaning.
- A `2.0.0` key means something different from a `3.0.0` key (its provider value lacked
  sport/date/uniqueness scope), so it is refused as `LEGACY_VERSION` rather than re-read.
- Legacy keys are preserved for read-only inspection (`legacy_active_bindings()`,
  `binding_key_contracts()`), and are refused whenever active authority is requested
  (`LegacyBindingKeyError`).
- Legacy approvals are refused by schema version.
- No migration code exists; any migration would be an isolated fixture test, never a
  production operation. The ledger is not production authority and is not adopted by
  `refresh()`.

---

## 6. Conflict and replay governance

| Conflict | Rule target | Behaviour |
| :--- | :--- | :--- |
| `CROSS_SPORT_SHARED_NAMESPACE` | `REVIEW_REQUIRED` / `FLAG_AMBIGUOUS_IDENTITY` | Distinct scoped identities; the shared legacy `500j` string is ambiguous and never silently authorizes two owners |
| `ROW_POSITION_REUSE` | `REVIEW_REQUIRED` / `FLAG_AMBIGUOUS_IDENTITY` | Never auto-overwrite an existing mapping |
| `STRONG_NUMBER_CONFLICT` | `REJECTED` / `REJECT_IDENTITY_CLAIM` | Rejected outright |
| `CANONICAL_OWNER_CONFLICT` | `REVIEW_REQUIRED` / `FLAG_AMBIGUOUS_IDENTITY` | Never auto-select the latest claim |
| Stale approval | — | `StaleBaselineError`; revalidation required |
| Duplicate submission | — | Exact duplicate → idempotent no-op; same decision with different payload → `LedgerReplayError` |

---

## 7. Shadow validation

`tools/mapping_governance_shadow.py` — **read-only** entry point.

Allowed: inspect a synthetic observation batch; extract provider evidence from a raw source
fixture (**offline**, `shadow_evidence_inspection`); create proposal artifacts; compare with an
isolated Canonical fixture; validate ownership against the existing ingestion planner
(**read-only**); verify a governance ledger; build a **read-only** migration impact manifest
(`shadow_migration_analysis`); produce a deterministic conflict report.

Forbidden (and not implemented): consulting the ledger as production authority; rewriting
`daily_matches.json`; rewriting historical snapshots; changing source adapters' persisted ID
formats; blocking the live refresh from ledger decisions; auto-approving proposals;
auto-migrating historical mappings; executing any migration.

CLI:

```bash
python -m tools.mapping_governance_shadow --demo --workspace /tmp/gov_shadow
python -m tools.mapping_governance_shadow \
    --observations rows.json --canonical-fixture canonical.json \
    --workspace /tmp/gov_shadow --dataset-identity my-fixture
python -m tools.mapping_governance_shadow \
    --evidence-fixture market.xml --adapter market_xml \
    --canonical-fixture canonical.json --workspace /tmp/gov_shadow \
    --migration-manifest --proposed-mapping proposed.json \
    --dependent-stores seven_stores.json
```

> The current ingestion preflight remains the authoritative runtime identity guard.
> Shadow governance is **not** an additional production write path. The migration impact
> manifest is an analysis artifact: it carries `executable: false`, exposes no execution API,
> and grants no authorization.

---

## 8. Authorization status

| Flag | Value |
| :--- | :--- |
| `PRODUCTION_MAPPING_WRITE_AUTHORIZATION` | `NOT_GRANTED` |
| `RUNTIME_LEDGER_ENFORCEMENT` | `NOT_ENABLED` |
| `ledger_is_production_authority` | `false` |
| `runtime_identity_authority` | `utils.ingestion_identity.validate_ingestion_identity` |

> No generated governance artifact may be mistaken for an already-authorized production
> migration.

---

## 9. Deferred source-ID migration designs (documentation only — not executed)

### 9.1 Sport-scoped market identity

**Problem**: football and basketball share the `500j-{date}-{matchnum}` string.

**Future-safe design**: derive new market identities with the sport in the namespace
(e.g. `500jzq-…` / `500jlq-…`, or `500j-{sport}-{date}-{number}`), and treat the legacy
`500j-…` string as an explicitly ambiguous alias.

The versioned contract, the three evaluated candidates and the selected preferred format are
specified in `docs/source-id-migration-planning.md` §1. Nothing there is wired into a
production parser: `utils/source_id_migration.py` only *composes* and *classifies* future
identifiers, and `utils/fetcher_500.py` is unchanged.

**Compatibility**: existing `500j` records stay byte-identical. The legacy form is mapped
only through a governance decision, never by a silent rewrite. Until such a decision exists,
the ingestion planner continues to fail closed on a cross-sport collision.

### 9.2 Stable finished-feed identity

**Requirement**: a stable finished-feed identity needs a provider identifier that is stable
across refreshes (a provider match id, or a competition number present on the finished feed).

**Current limitation**: the finished feed exposes **neither** — `_parse_wanchang` produces
no provider id and no competition number, so the only available value is the row ordinal.
The parser does not even retain the provider row identifier it reads from the `<tr>`.

**Conclusion**: **no stable identifier currently exists in the source data.** No surrogate
provider ID is invented, and row position is never promoted to a stable ID. The only safe
path is to preserve an identifier the provider actually supplies, if and when the finished
feed exposes one.

### 9.3 Historical mapping reconciliation (if ever authorized)

Referential integrity across `daily_matches.json`, `prediction_snapshots.json`,
`odds_snapshots.json`, `settlements.json` and `evaluation_rows.json` would require, in order:

1. **Dry run** — compute the full impact manifest; write nothing.
   (`utils/source_id_migration.build_migration_impact_manifest`, read-only.)
2. **Full impact manifest** — every affected Canonical ID, every dependent snapshot /
   settlement / evaluation row, and the exact before/after identity mapping.
3. **Owner approval** — an Ed25519 approval bound to the manifest fingerprint, the dataset
   identity and the baseline fingerprint, using a **mapping-specific** purpose (never the
   production repair purpose).
4. **Backups** — a verified, restorable copy of every affected store.
5. **Immutable evidence** — the pre-migration stores retained as evidence; nothing destroyed.
6. **Rollback / recovery** — a tested reverse path with an explicit recovery procedure.
7. **Postcondition verification** — every Canonical ID unique, every historical reference
   resolvable, all seven stores accounted for, and zero unintended changes.

This is design documentation only. No migration code and no production repair execution are
part of this phase. The manifest schema, the six impact classifications and the historical
reference analysis are specified in `docs/source-id-migration-planning.md` §2.

---

## 10. Non-goals

- No modification of real `data/*.json`; no production repair `apply` / `recover`.
- No historical Canonical migration or ID rewrite; no `500j` / `500w` format change.
- No change to `same_event`, strong-number precedence or exact-team fallback.
- No fuzzy matching, no invented provider identifiers, no promotion of row positions.
- No model, Elo, settlement or Temporal Evaluation mathematics change.
- No production approval bypass, no automatic binding approval, no reuse of a production
  repair signature as a mapping approval.
- No persistent production review queue, no web administration UI, no new database
  dependency, no `main` modification, no Git history rewrite.
