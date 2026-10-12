# Canonical Mapping Governance

**Module**: `utils/mapping_governance.py`
**Shadow entry point**: `tools/mapping_governance_shadow.py`
**Tests**: `tests/test_canonical_mapping_governance.py` (PG-09 … PG-30)
**Schema version**: `GOVERNANCE_SCHEMA_VERSION = "1.0.0"`
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

This phase builds and validates governance with **synthetic, isolated data only**.

### What is implemented vs. not

| Capability | Status |
| :--- | :--- |
| Provenance model, stability classification, conflict classification | **implemented** |
| Proposal contract + explicit state machine | **implemented** |
| Ed25519 owner approval with domain separation and full binding | **implemented** |
| Isolated append-only hash-chained ledger + read-only verification | **implemented** |
| Read-only shadow validation entry point | **implemented** |
| Runtime mapping enforcement / production mapping writes | **NOT implemented (not authorized)** |
| Historical Canonical migration | **NOT implemented (not authorized)** |

---

## 2. MappingProposal schema

| Field | Notes |
| :--- | :--- |
| `proposal_schema_version` | `"1.0.0"` |
| `proposal_id` | Deterministic, derived from **content** (order independent) |
| `source_observation_ref` | Observation ID |
| `source_identity_scope` | Scope dict; for conflicts this is an explicit `AMBIGUOUS_SHARED_SOURCE_ID` scope listing member scopes |
| `candidate_canonical_id` | Target Canonical ID (required for binding actions) |
| `existing_canonical_owner` | Existing owner, if present |
| `identity_evidence_type` | `strong_competition_number`, `exact_team_pair`, `source_alias`, `none` |
| `observation_fingerprints` | Semantic fingerprints of the evidence |
| `ingestion_plan_reference` | Read-only reference to the planner's conclusion |
| `reason` | Justification |
| `conflict_category` | Conflict class, if any |
| `expected_baseline_fingerprint` | Canonical baseline the proposal was built against |
| `requested_action` | See below |
| `status` | Declared initial status (`APPROVED` is rejected at construction) |
| `operator` | Preparing identity |

Allowed actions: `BIND_SOURCE_TO_EXISTING_CANONICAL`, `REGISTER_NEW_SOURCE_IDENTITY`,
`REJECT_IDENTITY_CLAIM`, `FLAG_AMBIGUOUS_IDENTITY`, `NO_ACTION`.

> A proposal never executes its action. There is **no** action that rewrites an existing
> Canonical ID; `validate_proposal` rejects any action containing `REWRITE`, `RENAME`,
> `MIGRATE`, `DELETE` or `MERGE_CANONICAL`.

One source identity claim cannot silently acquire multiple Canonical owners; one approved
source key cannot map to different owners without a separately governed supersession
decision.

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
`source_identity_scope`, `candidate_canonical_id`, `requested_action`, `dataset_identity`,
`expected_baseline_fingerprint`, `operator`, `approver`, `approved_at`, `reason`, `purpose`.

Refused (fail closed):

- missing / unsigned / empty-signature approval attempts;
- an approval whose `purpose` is not the mapping purpose;
- any tampered field (signature verification fails);
- unavailable, malformed or non-Ed25519 trusted public keys;
- an approval produced by an untrusted signing key;
- binding mismatches (proposal, content fingerprint, observation fingerprint, scope,
  candidate Canonical ID, action, dataset identity);
- `approver == operator` (the owner decision must be distinguishable from the operator);
- a **stale baseline**: if the Canonical baseline fingerprint changed after proposal
  creation, the approval cannot be reused without revalidation (`StaleBaselineError`).

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
| Concurrency safety | dedicated cross-process + cross-thread lock (independent of the refresh lock) |
| Reject malformed transitions | `validate_transition` + first-transition/declared-status consistency |
| Reject conflicting active bindings | one source key cannot be actively bound to two different Canonical IDs |
| Detect broken hash chains | `verify()` recomputes every hash, sequence and identity |
| Detect replayed transition identities | `replay_key` uniqueness + stale-tip binding |
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
> asserted explicitly in PG-14 and reported by `verify()["trust_anchor"]`.

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

Allowed: inspect a synthetic observation batch; create proposal artifacts; compare with an
isolated Canonical fixture; validate ownership against the existing ingestion planner
(**read-only**); verify a governance ledger; produce a deterministic conflict report.

Forbidden (and not implemented): consulting the ledger as production authority; rewriting
`daily_matches.json`; rewriting historical snapshots; changing source adapters' persisted ID
formats; blocking the live refresh from ledger decisions; auto-approving proposals;
auto-migrating historical mappings.

CLI:

```bash
python -m tools.mapping_governance_shadow --demo --workspace /tmp/gov_shadow
python -m tools.mapping_governance_shadow \
    --observations rows.json --canonical-fixture canonical.json \
    --workspace /tmp/gov_shadow --dataset-identity my-fixture
```

> The current ingestion preflight remains the authoritative runtime identity guard.
> Shadow governance is **not** an additional production write path.

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
(e.g. `500jzq-…` / `500jclq-…`, or `500j-{sport}-{date}-{number}`), and treat the legacy
`500j-…` string as an explicitly ambiguous alias.

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
part of this phase.

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
