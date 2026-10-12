# Canonical Identity Ingestion Preflight

**Module**: `utils/ingestion_identity.py`
**Wired into**: `utils/scraper.py` (`_refresh_impl` step 5b/7b, and the top of `_merge`)
**Test suite**: `tests/test_canonical_ingestion_guard.py` (IG-01 .. IG-28 plus audit additions)
**Governance status**: `PRODUCTION_REPAIR_AUTHORIZATION = NOT_GRANTED` (this module performs no repair)

---

## 1. Why this module exists

`utils.scraper` already refused to *persist* two Canonical records sharing one `id`
(step 0 of `_refresh_impl`), and `_merge` already refused several identity collisions.
What was missing was a **single, order-independent, reusable boundary** that decides
whether an incoming source batch may be admitted at all, and that provably runs
*before every persistent downstream write*.

The concrete defect this closed was an **ordering-dependent ownership decision**.
Strong competition numbers dominate event equality, and the exact-team fallback is only
used when a strong number is missing on one side. That comparison is therefore **not
transitive**:

| Record | Strong number | Teams | Matches `canon-001`? |
| :--- | :--- | :--- | :--- |
| `canon-001` (existing) | `001` | A / B | — |
| `x` | *(none)* | A / B | yes (team fallback) |
| `y` | `002` | A / B | no (strong-number conflict) |

`x ~ canon-001`, `y ~ canon-001` is **false**, yet `x ~ y` is **true** by team fallback.
`x` is claimed by two different events, so the batch is inherently ambiguous. The old
sequential merge happened to *accept* the batch when `x` was processed first (silently
creating a spurious new event `y`) and to *reject* it when `y` was processed first.

Measured before/after (same fixture, both row orders):

```text
=== WITHOUT preflight (pre-fix behaviour) ===
  order x,y: ACCEPTED ids=['canon-001', 'live-y'] added=1
  order y,x: REJECTED CanonicalIdentityCollisionError category=AMBIGUOUS_CANONICAL_OWNERSHIP

=== WITH preflight (current code) ===
  order x,y: REJECTED CanonicalIdentityCollisionError category=AMBIGUOUS_CANONICAL_OWNERSHIP
  order y,x: REJECTED CanonicalIdentityCollisionError category=AMBIGUOUS_CANONICAL_OWNERSHIP
```

---

## 2. Position in the pipeline

```text
Source Rows
    |
    v
Existing Parser and Source Adapters            (utils/fetcher_500.py)
    |
    v
Market Attachment / Candidate Construction     (_attach_odds, _append_unmatched_market_candidates)
    |
    v
Canonical Identity Ingestion Preflight         (validate_ingestion_identity)   <-- this module
    |
    +-- VALID / UNAMBIGUOUS
    |       |
    |       v
    |   Existing Market Admission               (_admit_market_candidates)
    |       |
    |       v
    |   Existing Canonical Merge                (_merge)
    |       |
    |       v
    |   Canonical ID Uniqueness Proof           (validate_canonical_uniqueness)  <-- this module
    |       |
    |       v
    |   Downstream Processing                   (odds history, Elo, snapshots, settlements,
    |                                            evaluation rows, daily_matches.json)
    |
    +-- AMBIGUOUS / CONFLICTING
            |
            v
       FAIL CLOSED
            |
            v
       No Persistent Writes
```

Every downstream write happens strictly after both checks:

| Downstream write | Called from |
| :--- | :--- |
| `_capture_odds_history()` | `_refresh_impl` step 8 |
| `_calibrate_from_finished()` | step 9 |
| `_capture_prediction_snapshots()` | step 10 |
| `_settle_finished_matches()` | step 11 |
| `_materialize_evaluation_rows()` | step 12 |
| `save_json(DAILY_FILE, ...)` | step 13 |

`_merge` calls `validate_ingestion_identity` at its own entry as well, so **direct
callers of `_merge` receive the same fail-closed protection**. The pipeline call is a
superset of the `_merge` call (`admitted ⊆ incoming`), and the preflight is monotone
over subsets, so a batch that passes the pipeline check can never be rejected by the
inner one.

---

## 3. Canonical ownership contract

| Concern | Rule |
| :--- | :--- |
| Existing Canonical ID | Once persisted, an ID is never rewritten. Incoming source IDs identify or reconcile events; they never replace a Canonical ID. |
| Event equality | `utils.match_identity.same_event` verbatim: sport, then date, then strong competition number when both sides have one, else exact `(sport, date, home, away)`. |
| Strong-number conflict | Never falls back to team names. |
| Kickoff time | Not part of event identity. |
| Source alias | A source ID claimed by two incompatible events is rejected. |
| Owner cardinality | Each accepted incoming event maps to exactly **one** Canonical owner, or becomes one genuinely new event with an unused ID. |
| Ambiguity | Any unresolved ambiguity rejects the **whole** batch. No first-match shortcut, no arbitrary list-order owner selection, no partial commit, no row deletion to bypass the failure. |

No fuzzy team matching, no club-alias dictionary, no heuristic event merging, no source
popularity as identity evidence, and no new event-equality rule were introduced.

---

## 4. Conflict categories

Preserved categories (externally observable behaviour unchanged):

| Category | Meaning |
| :--- | :--- |
| `PREEXISTING_DUPLICATE` | Two Canonical records share one `id`. |
| `SAME_SOURCE_ALIAS_DIFFERENT_EVENT` | One source ID claims incompatible events. |
| `AMBIGUOUS_CANONICAL_OWNERSHIP` | One incoming event matches multiple owners, or two equal incoming events resolve to different owners. |
| `INCOMING_SHARED_ID` | A new event would reuse an already-owned Canonical ID. |
| `MALFORMED_INCOMING_RECORD` | An incoming record is not a dictionary. |
| `MALFORMED_INCOMING_ID` | An incoming ID is not a non-empty string. |
| `INADEQUATE_EVENT_IDENTITY` | No ID and no usable identity fields. |

Added categories, used only by the new post-merge uniqueness proof (never emitted where
an existing category used to be emitted):

| Category | Meaning |
| :--- | :--- |
| `MALFORMED_CANONICAL_MATCH` | A merged record is not a dictionary. |
| `MALFORMED_CANONICAL_ID` | A merged ID is not a non-empty string. |
| `CANONICAL_UNIQUENESS_VIOLATION` | The merge produced a duplicate ID. |
| `CANONICAL_ID_LOST` | An existing Canonical ID disappeared during the merge. |
| `CANONICAL_COUNT_MISMATCH` | Record count is not internally consistent. |

---

## 5. Determinism guarantees

The preflight never chooses an owner from list order. All checks are set/group based and
therefore permutation invariant:

1. **Alias consistency** — incoming records are grouped by normalized source ID
   (iterated in sorted order) and checked **all-pairs**, not against a single
   reference member. All-pairs is required precisely because `same_event` is not
   transitive.
2. **Existing-alias compatibility** — a claimed alias must be `same_event` with its
   existing owner.
3. **Owner cardinality** — per-record match set against the existing Canonical set.
4. **Non-transitive consistency** — for every pair of equal incoming records, the two
   existing-owner sets must be identical. Differing sets (including *empty vs.
   non-empty*) mean the batch is ambiguous and is rejected.

Consequences:

- Reordering independent incoming events never changes Canonical ownership.
- The same conflict class is detected for every permutation.
- Repeated identical refreshes stay idempotent.
- Malformed or contradictory evidence cannot pass through.
- The preflight does not modify its inputs (asserted by `test_preflight_does_not_mutate_its_inputs`).
- Source priority is never used as a substitute for identity proof.

---

## 6. Structured diagnostics

`CanonicalIdentityCollisionError` remains the primary abstraction. `category` and
`match_id` stay directly inspectable; the remaining fields are additive:

| Field | Description |
| :--- | :--- |
| `category` | Conflict class (see §4). |
| `match_id` | The Canonical ID or source alias involved. |
| `source_namespace` | Lexical prefix of the ID (`500lq-...` -> `500lq`); no provenance is inferred beyond what the identifier states. |
| `incoming_index` | Index of the offending record in this batch. |
| `competing_canonical_ids` | The Canonical IDs that compete for ownership. |
| `evidence_type` | `source_alias_reuse`, `existing_alias_ownership`, `multi_owner`, `non_transitive_identity`, `canonical_id_uniqueness`, ... |
| `reason` | Stable machine-readable reason token. |

Diagnostics are deterministic and suitable for log inspection and offline regression
tests. No database, review queue, or persistence schema is introduced. A clean batch
emits no new warnings.

---

## 7. Post-merge uniqueness proof

`validate_canonical_uniqueness(merged, previous_ids=..., expected_total=...)` runs after
the merge and before the first downstream write. It asserts:

1. every Canonical match is a dictionary;
2. every Canonical ID is a non-empty string;
3. every Canonical ID is unique;
4. every pre-existing Canonical ID survived the merge (no rewrite, no loss);
5. the record count equals `len(existing) + added`.

It is an **additional** defence layer. It does **not** replace the startup guard that
rejects pre-existing duplicate Canonical IDs, and it never auto-repairs historical
duplicates. `PREEXISTING_DUPLICATE` still fails closed at load time.

---

## 8. Source identity stability audit

| Adapter | Identifier | Basis | Stability |
| :--- | :--- | :--- | :--- |
| `_parse_live` (football) | `500l-{date}-{order or fid}` | `order` = 竞彩 competition number; `fid` = provider row id (`a1495377`) | Provider-derived. Scoped to `(sport, date)`. |
| `_parse_lq` (basketball) | `500lq-{date}-{no or mid}` | `no` = 竞彩 number; `mid` = provider match id | Provider-derived. Scoped to `(sport, date)`. |
| `fetch_jczq_xml` | `500j-{date}-{matchnum or mid}` | `matchnum` = 竞彩 number; `mid` = provider match id | Provider-derived. Scoped to `(sport, date)` — the namespace prefix is shared by football and basketball, so a cross-sport number collision is possible and is rejected by the preflight. |
| `_parse_wanchang` (finished) | `500w-{date}-{row_index}` | **Row position** in the parsed response | **Unstable.** Not an identity claim. |

The finished-feed identifier is derived from position and can denote a different event
on the next refresh. Because finished rows carry no odds, market admission only lets
them through when they already reconcile with a tracked event, and the preflight never
treats a `500w-` alias as sufficient proof: an incompatible reuse is rejected, while a
genuine same-event replay reconciles through the existing strong-number or exact-team
evidence and keeps the original Canonical ID. Where no stable ownership can be
established, the batch fails closed.

No new persisted ID algorithm was introduced, and no existing match was renamed
retrospectively.

---

## 9. Fail-closed semantics

A rejected batch produces **zero** changes to all seven persistent JSON stores:
`daily_matches.json`, `team_strength.json`, `calibrated.json`,
`prediction_snapshots.json`, `odds_snapshots.json`, `settlements.json`,
`evaluation_rows.json`.

The regression suite verifies this by seeding all seven stores, recording SHA-256 of
every file, injecting a conflicting batch, asserting the identity exception, recomputing
all seven hashes, asserting byte-for-byte equality, and then proving the refresh lock
was released by running a subsequent valid refresh to completion.

A source transport failure with no identity conflict keeps the application's existing
error-handling behaviour (the source is recorded as `error: ...` and the refresh
continues); an identity collision is never misclassified as an ordinary outage.

---

## 10. Preserved contracts

- **Market admission** — unpriced new events are still rejected; valid priced new events
  are still admitted; already-tracked events still survive temporary odds loss and keep
  receiving status and final-score updates. Identity validation never turns an unpriced
  match into a tracked market event, and never rejects a legitimate market-backed event
  whose source ID differs while existing identity evidence safely reconciles it.
- **Finalized integrity** — `valid_full_time_score`, `is_finalized_match` and
  `resolve_match_update` are untouched. Valid final scores propagate; identical replays
  are idempotent; conflicting final results cannot overwrite an authoritative score;
  malformed scores still cannot enter Elo calibration, settlement or evaluation
  materialization.
- **Model mathematics** — model versions and all football / basketball / Temporal
  Evaluation formulas are unchanged.

---

## 11. Non-goals

Out of scope for this boundary, by explicit governance:

- repairing historical Canonical collisions, rewriting Canonical identifiers, or
  migrating historical snapshots / odds history / settlements / evaluation samples;
- running production repair `apply` / `recover`, or touching signed repair tooling;
- a persistent identity-mapping store, a persistent review queue, or a human-review UI;
- new database dependencies or public JSON schema changes;
- changing `same_event` semantics, adding fuzzy matching or an alias dictionary.

A safe upstream correction to provider-ID provenance (for example deriving a stable
finished-feed identifier) requires changing historical source contracts and is
therefore **DEFERRED** rather than attempted here.
