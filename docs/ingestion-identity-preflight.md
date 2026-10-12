# Canonical Identity Ingestion Preflight

**Module**: `utils/ingestion_identity.py`
**Wired into**: `utils/scraper.py` (`_refresh_impl` step 5b/7b, and the top of `_merge`)
**Test suites**: `tests/test_canonical_ingestion_guard.py` (IG-01 … IG-28, IC-01 … IC-15, plus audit additions),
`tests/test_staging_canonical_repair.py` (IC-REG-12 … IC-REG-18)
**Governance status**: `PRODUCTION_REPAIR_AUTHORIZATION = NOT_GRANTED` (this module performs no repair)

---

## 1. Why this module exists

`utils.scraper` already refused to *persist* two Canonical records sharing one `id`
(step 0 of `_refresh_impl`), and `_merge` already refused several identity collisions.
What was missing was a **single, order-independent, reusable boundary** that decides
whether an incoming source batch may be admitted at all, that resolves every accepted
event to exactly one Canonical owner, and that provably runs *before every persistent
downstream write*.

Three defects were confirmed with executable reproducers before any implementation
change.

### 1.1 Order-dependent ownership decision

Strong competition numbers dominate event equality, and the exact-team fallback is used
only when a strong number is missing on one side. That comparison is **not transitive**:

| Record | Strong number | Teams | Matches `canon-001`? |
| :--- | :--- | :--- | :--- |
| `canon-001` (existing) | `001` | A / B | — |
| `x` | *(none)* | A / B | yes (team fallback) |
| `y` | `002` | A / B | no (strong-number conflict) |

`x ~ canon-001`, `y ~ canon-001` is **false**, yet `x ~ y` is **true** by team fallback.
`x` is claimed by two different events, so the batch is inherently ambiguous. The old
sequential merge accepted the batch when `x` was processed first (silently creating a
spurious new event `y`) and rejected it when `y` was processed first.

```text
=== WITHOUT preflight (pre-fix) ===
  order x,y: ACCEPTED ids=['canon-001', 'live-y'] added=1
  order y,x: REJECTED CanonicalIdentityCollisionError category=AMBIGUOUS_CANONICAL_OWNERSHIP
```

### 1.2 Incoming-only non-transitive component

The same non-transitivity also escaped detection when there was **no** pre-existing
Canonical owner, because an empty owner set was treated as proof of "one event". The
minimal reproducer is a triangle with no existing records at all:

| Row | Source ID | Strong number | Teams |
| :--- | :--- | :--- | :--- |
| `X` | `live-x` | *(none)* | A / B |
| `Y` | `market-y` | `001` | A / B |
| `Z` | `market-z` | `002` | A / B |

`same_event(X, Y) = True`, `same_event(X, Z) = True`, `same_event(Y, Z) = False`.
Measured on the pre-correction baseline, 4 of the 6 permutations were **accepted** and
persisted mutually inconsistent Canonical ID sets:

```text
[X,Y,Z] -> ACCEPTED ids=['live-x', 'market-z']   added=2 updated=1
[Z,X,Y] -> ACCEPTED ids=['market-y', 'market-z'] added=2 updated=0
[Y,X,Z] -> ACCEPTED ids=['market-y', 'market-z'] added=2 updated=0
[X,Z,Y] -> ACCEPTED ids=['live-x', 'market-y']   added=2 updated=1
[Y,Z,X] -> REJECTED AMBIGUOUS_CANONICAL_OWNERSHIP
[Z,Y,X] -> REJECTED AMBIGUOUS_CANONICAL_OWNERSHIP
```

### 1.3 Order-dependent first-seen identity selection

With two source rows for one brand-new event, the persisted Canonical ID was whichever
row happened to be processed first:

```text
[A,B] -> Canonical ID 'live-001'
[B,A] -> Canonical ID 'market-001'
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
Canonical Identity Ingestion Preflight         (plan_ingestion_identity / validate_ingestion_identity)
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
    |   Canonical ID Uniqueness Proof           (validate_canonical_uniqueness)
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

Running the preflight on the **whole candidate batch** (before market admission) is
load-bearing: a bridge row that market admission would later drop can still be the
evidence that makes a batch ambiguous.

Every downstream write happens strictly after both checks:

| Downstream write | Called from |
| :--- | :--- |
| `_capture_odds_history()` | `_refresh_impl` step 8 |
| `_calibrate_from_finished()` | step 9 |
| `_capture_prediction_snapshots()` | step 10 |
| `_settle_finished_matches()` | step 11 |
| `_materialize_evaluation_rows()` | step 12 |
| `save_json(DAILY_FILE, ...)` | step 13 |

`_merge` calls `plan_ingestion_identity` at its own entry as well, so **direct callers of
`_merge` receive the same fail-closed protection and the same deterministic ID
selection**. The pipeline call is a superset of the `_merge` call
(`admitted ⊆ incoming`), and the preflight is monotone over subsets, so a batch that
passes the pipeline check can never be rejected by the inner one.

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

## 4. Incoming-only ambiguity closure (identity graph)

Ownership analysis is a **connected-component analysis over the incoming
`same_event` graph**, not a chain of pairwise matches. Because `same_event` is not
transitive, a chain of pairwise matches is *not* proof that all its members are one
event.

Algorithm (order independent; `(sport, date)` bucketing is an equivalence-preserving
optimisation, since `same_event` already requires both to be equal):

1. Group the incoming records by normalised source ID; every pair inside a group must be
   `same_event`, otherwise `SAME_SOURCE_ALIAS_DIFFERENT_EVENT`.
2. Reject an incoming source ID that is already owned by an incompatible Canonical
   record (`SAME_SOURCE_ALIAS_DIFFERENT_EVENT`).
3. Build connected components over `same_event` edges (union-find).
4. **Every component must be a clique.** If any two members of one component are not
   `same_event`, the component contains mutually incompatible strong-number identities
   bridged by a missing-number record → `AMBIGUOUS_CANONICAL_OWNERSHIP`
   (`evidence_type="non_transitive_identity"`, `reason="non_clique_component"`).
5. **Every member of a component must resolve to the same set of existing Canonical
   owners** (possibly empty). A member that matches an owner while a sibling does not is
   the same defect seen from the existing-collection side → `AMBIGUOUS_CANONICAL_OWNERSHIP`
   (`reason="component_owner_inconsistency"`).
6. A component resolving to more than one owner → `AMBIGUOUS_CANONICAL_OWNERSHIP`
   (`evidence_type="multi_owner"`).

Step 4 is what closes the incoming-only triangle (§1.2); step 5 is what closes the
existing-owner variant (§1.1). Steps 4 and 5 together are provably complete for this
identity model: if every member agrees pairwise and every member agrees about its owner,
the component is one unambiguously owned event.

Cost is O(b²) per `(sport, date)` bucket for the graph and O(b·m_b) for the owner scan,
where `b` is the batch size within a bucket and `m_b` the Canonical records in it —
bounded and negligible for daily sports-source batch sizes.

---

## 5. Deterministic first-seen ownership

Identity equivalence and identifier selection are separated. Identity is established by
§4 **first**; only then is a Canonical ID chosen, and the choice depends solely on record
content:

1. **Source IDs present** — pick the member ID with the best declared source-namespace
   tier; ties break on the lexicographically smallest ID string.

   | Tier | Namespace | Adapter |
   | :--- | :--- | :--- |
   | 0 | `500l`, `500lq` | live score feed (football / basketball) |
   | 1 | `500j` | market XML (`jczq` / `jclq`) |
   | 2 | `500w` | finished feed |
   | 3 | anything else | undeclared namespace, lexicographic tie-break only |

   This table only makes the **pre-existing de-facto behaviour** explicit: live rows
   already entered the batch ahead of market and finished rows, so live IDs already won
   in practice. It is never used as identity evidence, and never as source popularity.

2. **No source ID present** — keep the existing synthetic format
   `{sport}-{date}-{ordinal}`, where `ordinal = len(existing) + rank` and `rank` is the
   record's position among the batch's ID-less new events sorted by content key. The
   format is unchanged; only the ordinal's determinism is new.

3. A resolved new Canonical ID that collides with an existing ID or with another planned
   new ID → `INCOMING_SHARED_ID` (fail closed, never a silent rename).

Subsequent refreshes always keep the persisted Canonical ID: an existing owner's raw ID
string is carried through unchanged, even when an incoming row has a better-tier ID.

---

## 6. Conflict categories

Preserved categories (externally observable behaviour unchanged):

| Category | Meaning |
| :--- | :--- |
| `PREEXISTING_DUPLICATE` | Two Canonical records share one `id`. |
| `SAME_SOURCE_ALIAS_DIFFERENT_EVENT` | One source ID claims incompatible events. |
| `AMBIGUOUS_CANONICAL_OWNERSHIP` | Non-clique component, inconsistent owner resolution, or multiple owners. |
| `INCOMING_SHARED_ID` | A new event would reuse an already-owned Canonical ID. |
| `MALFORMED_INCOMING_RECORD` | An incoming record is not a dictionary. |
| `MALFORMED_INCOMING_ID` | An incoming ID is not a non-empty string. |
| `INADEQUATE_EVENT_IDENTITY` | No ID and no usable identity fields. |

Added categories, used only by the post-merge uniqueness proof:

| Category | Meaning |
| :--- | :--- |
| `MALFORMED_CANONICAL_MATCH` | A merged record is not a dictionary. |
| `MALFORMED_CANONICAL_ID` | A merged ID is not a non-empty string. |
| `CANONICAL_UNIQUENESS_VIOLATION` | The merge produced a duplicate ID. |
| `CANONICAL_ID_LOST` | An existing Canonical ID disappeared during the merge. |
| `CANONICAL_COUNT_MISMATCH` | Record count is not internally consistent. |

New ambiguity variants are documented through `reason`
(`non_clique_component`, `component_owner_inconsistency`) and `evidence_type`
(`non_transitive_identity`, `multi_owner`).

---

## 7. Determinism guarantees

1. **Alias consistency** — all-pairs inside each alias group (iterated in sorted order).
2. **Component analysis** — union-find over the incoming graph, bucketed by `(sport, date)`.
3. **Clique + owner consistency** — per component, all-pairs and per-member owner sets.
4. **Component ordering** — components are processed in an order derived from record
   content, never from row order, so the reported conflict is stable too.
5. **ID selection** — content-derived (tier, then lexicographic), never row order.
6. **Input immutability** — the preflight never mutates `existing` or `incoming`.
7. **No source precedence as identity proof** — precedence applies only after identity is
   established.

Consequences: reordering independent incoming events never changes Canonical ownership;
the same conflict class is detected for every permutation; repeated identical refreshes
stay idempotent; malformed or contradictory evidence cannot pass through.

---

## 8. Structured diagnostics

`CanonicalIdentityCollisionError` remains the primary abstraction. `category` and
`match_id` stay directly inspectable; the remaining fields are additive:

| Field | Description |
| :--- | :--- |
| `category` | Conflict class (see §6). |
| `match_id` | The Canonical ID or source alias involved. |
| `source_namespace` | Lexical prefix of the ID (`500lq-...` -> `500lq`); no provenance is inferred beyond what the identifier states. |
| `incoming_index` | Index of the offending record in this batch. |
| `competing_canonical_ids` | The Canonical IDs that compete for ownership. |
| `evidence_type` | `source_alias_reuse`, `existing_alias_ownership`, `non_transitive_identity`, `multi_owner`, `canonical_id_uniqueness`, … |
| `reason` | Stable machine-readable reason token. |

Diagnostics are deterministic and suitable for log inspection and offline regression
tests. No database, review queue, or persistence schema is introduced. A clean batch
emits no new warnings.

---

## 9. Post-merge uniqueness proof

`validate_canonical_uniqueness(merged, previous_ids=..., expected_total=...)` runs after
the merge and before the first downstream write. It asserts:

1. every Canonical match is a dictionary;
2. every Canonical ID is a non-empty string;
3. every Canonical ID is unique;
4. every pre-existing Canonical ID survived the merge (no rewrite, no loss);
5. the record count equals `len(existing) + added`.

It is an **additional** defence layer. It does **not** replace the startup guard that
rejects pre-existing duplicate Canonical IDs, and it never auto-repairs historical
duplicates.

---

## 10. Source identity stability audit

| Adapter | Identifier | Basis | Stability |
| :--- | :--- | :--- | :--- |
| `_parse_live` (football) | `500l-{date}-{order or fid}` | `order` = 竞彩 competition number; `fid` = provider row id (`a1495377`) | Provider-derived. Scoped to `(sport, date)`. |
| `_parse_lq` (basketball) | `500lq-{date}-{no or mid}` | `no` = 竞彩 number; `mid` = provider match id | Provider-derived. Scoped to `(sport, date)`. |
| `fetch_jczq_xml` | `500j-{date}-{matchnum or mid}` | `matchnum` = 竞彩 number; `mid` = provider match id | Provider-derived. The `500j` namespace prefix is shared by football and basketball, so a cross-sport number collision is possible and is rejected (behaviourally identical before and after this correction). |
| `_parse_wanchang` (finished) | `500w-{date}-{row_index}` | **Row position** in the parsed response | **Unstable.** Never accepted as sole ownership proof; an incompatible reuse is rejected, a genuine same-event replay reconciles through strong-number or exact-team evidence. |

No new persisted ID algorithm was introduced, and no existing match was renamed
retrospectively.

---

## 11. Fail-closed semantics

A rejected batch produces **zero** changes to all seven persistent JSON stores:
`daily_matches.json`, `team_strength.json`, `calibrated.json`,
`prediction_snapshots.json`, `odds_snapshots.json`, `settlements.json`,
`evaluation_rows.json`.

The regression suite verifies this by seeding all seven stores, recording SHA-256 of
every file, injecting a conflicting batch, asserting the identity exception, recomputing
all seven hashes, asserting byte-for-byte equality, and then proving the refresh lock was
released by running a subsequent valid refresh to completion.

A source transport failure with no identity conflict keeps the application's existing
error-handling behaviour; an identity collision is never misclassified as an ordinary
outage.

---

## 12. Preserved contracts

- **Market admission** — unpriced new events are still rejected; valid priced new events
  are still admitted; already-tracked events still survive temporary odds loss and keep
  receiving status and final-score updates. Identity validation never turns an unpriced
  match into a tracked market event, and never rejects a legitimate market-backed event
  whose source ID differs while existing identity evidence safely reconciles it.
- **Finalized integrity** — `valid_full_time_score`, `is_finalized_match` and
  `resolve_match_update` are untouched.
- **Model mathematics** — model versions and all football / basketball / Temporal
  Evaluation formulas are unchanged.
- **Protected-path guard** — `tools/staging_canonical_repair.py` containment checks and
  legacy protected-root declarations are unchanged. See §13.

---

## 13. Windows local regression corrections

Three regression tests in `tests/test_staging_canonical_repair.py` were environment
dependent. On a host whose checkout sits **at a declared legacy production root** —
`LEGACY_PROD_REPO_ROOT == SOURCE_REPO_ROOT` — the path guard reaches its
declared-protected-root branch before its source-checkout branch, so the *same* refusal
was reported with a different (equally valid) message, and one test asserted the two
canonicalized roots were distinct.

The security decision was never wrong: every target was refused. Only *which* valid
reason was reached first differed. Corrections:

| Test | Correction |
| :--- | :--- |
| `IC-REG-12` | Case 1 asserts a protected-location refusal reason-agnostically (`assert_refused_as_protected`). |
| `IC-REG-12b` | Refusal asserted reason-agnostically; the **dynamic** checkout protection is then isolated (test-scoped removal of the declared legacy roots) and required to reject specifically as *source repository checkout*. |
| `IC-REG-12c` | Value-distinctness from `SOURCE_REPO_ROOT` replaced by a machine-independent invariant: the legacy roots are static **string literals** in the module source (verified by `ast`), so they are never re-derived from the dynamic checkout. |
| `IC-REG-17` (new) | Overlap scenario built from a controlled temporary fixture root that is simultaneously a declared legacy root and the source checkout; each protection is then required to be independently sufficient. |
| `IC-REG-18` (new) | Real NTFS junction behaviour: junction into a protected root refused; junction escaping the staging root refused. Fixture roots only; nothing outside `tmp_path` is touched. |

No containment check was weakened, no Windows-specific assertion is skipped on Windows,
and no developer-specific path was added.

---

## 14. Non-goals

Out of scope for this boundary, by explicit governance:

- repairing historical Canonical collisions, rewriting Canonical identifiers, or
  migrating historical snapshots / odds history / settlements / evaluation samples;
- running production repair `apply` / `recover`, or touching signed repair tooling;
- a persistent identity-mapping store, a persistent review queue, or a human-review UI;
- new database dependencies or public JSON schema changes;
- changing `same_event` semantics, adding fuzzy matching or an alias dictionary.

A safe upstream correction to provider-ID provenance (for example deriving a stable
finished-feed identifier, or a sport-scoped `500j` namespace) requires changing
historical source contracts and remains **DEFERRED**.
