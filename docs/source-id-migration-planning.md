# Source-ID Migration Planning (read-only design)

**Market source-ID contract**: `utils/source_id_migration.py`
**Contract version**: `MARKET_SOURCE_ID_CONTRACT_VERSION = "1.0.0"`
**Manifest schema version**: `MIGRATION_MANIFEST_SCHEMA_VERSION = "1.0.0"`
**Tests**: `tests/test_source_id_migration_manifest.py` (MI-01 … MI-16)
**Production mapping write authorization**: `NOT_GRANTED`
**Runtime ledger enforcement**: `NOT_ENABLED`
**Historical Canonical migration**: `NOT_AUTHORIZED`

> Everything in this document is **design and read-only analysis**. No production parser
> emits the new format, no source ID is rewritten, no Canonical ID is rewritten, and no
> migration operation exists anywhere in the repository.

---

## 1. Sport-scoped market source-ID contract

### 1.1 Problem

`utils/fetcher_500.fetch_jczq_xml` builds `500j-{date}-{matchnum_or_mid}` for **both**
football (`jczq`) and basketball (`jclq`). The two competition-number sequences are
independent per day, so one string can denote two different events in two different sports:

```text
football  market row -> 500j-2030-05-01-001  (jczq 001)
basketball market row -> 500j-2030-05-01-001  (jclq 001)
```

The existing ingestion preflight fails closed on that batch
(`SAME_SOURCE_ALIAS_DIFFERENT_EVENT`), so the ambiguity is *contained*, not resolved.

### 1.2 Candidate formats

| Option | Format | Example (football / basketball) | Assessment |
| :--- | :--- | :--- | :--- |
| **A (preferred)** | `{prefix}{sport_token}-{date}-{number}` | `500jzq-2030-05-01-001` / `500jlq-2030-05-01-001` | Sport is disambiguated at the **namespace** level. A new prefix does **not** match the existing `startswith("500j-")` market-fallback compatibility branch in `competition_number_for_match`, so no legacy semantics are triggered accidentally. Lexical namespace splitting (`id.split("-", 1)[0]`) keeps working unchanged. Cost: two new namespace prefixes must be registered in `SOURCE_NAMESPACE_TIERS`. |
| B | `{prefix}-{sport}-{date}-{number}` | `500j-football-2030-05-01-001` / `500j-basketball-2030-05-01-001` | Parsable (a 4-digit year vs an alphabetic sport token is unambiguous), but the string **still** matches `startswith("500j-")`, so the legacy market-fallback branch would fire for new IDs. Requires tightening that branch at the same time — a larger, riskier change. |
| C | `{prefix}v2-{sport}-{date}-{number}` | `500jv2-football-2030-05-01-001` / `500jv2-basketball-2030-05-01-001` | Adds an explicit contract-version token in the namespace; avoids the `500j-` collision, but introduces a third namespace and duplicates a concept already carried by `IDENTITY_KEY_SCHEMA_VERSION`. No additional benefit over A. |

**Selected**: **Option A**. It is the only candidate that fixes the collision at the point
where the collision actually exists (the shared namespace) while leaving every existing
prefix-based code path intact and untriggered.

`SPORT_NAMESPACE_TOKENS = {"football": "zq", "basketball": "lq"}`, so the composed IDs are
`500jzq-…` / `500jlq-…`.

### 1.3 Required design considerations

| # | Consideration | Resolution |
| :--- | :--- | :--- |
| 1 | Sport disambiguation | Carried by the namespace prefix, so sport is determined even if date/number segments are malformed. |
| 2 | Event-date scoping | The date segment keeps `YYYY-MM-DD`, so the competition identity scope stays `(sport, date, normalized_number)`. |
| 3 | Competition-number normalization | Reuses the existing `normalize_competition_number` rules; the contract does not change normalization semantics. |
| 4 | Provider ID vs competition number | The new format carries **only** the competition number. Provider match ids stay in the provenance sidecar and are never written into a source ID. |
| 5 | Unknown identifiers | Missing numbers are never invented: the existing derived-ID rules apply, and such an ID is observation/review-only. |
| 6 | Legacy `500j` compatibility | Existing `500j-…` strings stay **byte-identical** and are classified `LEGACY_SHARED`; they are mapped only by an explicit governance decision. |
| 7 | Historical Canonical ownership | Existing Canonical IDs are never rewritten. A source-alias change alone is **not** grounds for a Canonical ID change. |
| 8 | Mapping approval requirements | Any legacy → new alias binding requires an Ed25519 owner approval bound to the discriminating identity key (contract `3.0.0`). Never auto-approved. |
| 9 | Snapshot references | `snapshot_id` is derived from `match_id` (`sha256(match_id \| …)`), so rewriting a Canonical ID would make snapshot IDs unverifiable. This is the strongest argument for leaving Canonical IDs unchanged. |
| 10 | Migration rollback feasibility | Alias mapping is reversible (the legacy string is retained and the new string is recomputable). Canonical-ID rewriting is **not** reversible for the snapshot/settlement/evaluation chain. Rollback feasibility therefore hinges entirely on *not* rewriting Canonical IDs — which is exactly what this phase does. |

### 1.4 Pure helpers (never wired into a parser)

```python
compose_sport_scoped_market_source_id(sport="football", event_date="2030-05-01", number="001")
# -> "500jzq-2030-05-01-001"

parse_sport_scoped_market_source_id("500jlq-2030-05-01-001")
# -> {"sport": "basketball", "event_date": "2030-05-01", "number": "001"}

classify_market_source_id("500j-2030-05-01-001")   # -> "LEGACY_SHARED"
classify_market_source_id("500jzq-2030-05-01-001") # -> "SPORT_SCOPED"
```

Unsupported sports, a missing date, or a missing number all raise — nothing is guessed.

> `utils/fetcher_500.py` is **not modified** and does not import this module.

---

## 2. Read-only migration impact manifest

### 2.1 Purpose

`build_migration_impact_manifest(...)` answers, for a synthetic fixture:
*if these source aliases were ever re-pointed, exactly what would be touched, and what is
still missing?* It writes nothing and executes nothing.

### 2.2 Inputs

| Input | Meaning |
| :--- | :--- |
| `fixture_identity` | Name of the synthetic fixture. |
| `canonical_fixture` | Synthetic Canonical match rows (defines the Canonical ID set). |
| `observations` | `SourceObservation` batch (from the provenance layer or the opt-in extractor). |
| `proposed_mappings` | `ProposedSourceMapping` list: legacy alias → proposed future source ID, plus its identity evidence. **There is no "new Canonical ID" field** — no such operation exists. |
| `dependent_stores` | Optional synthetic seven-store fixture. |
| `dataset_identity` | Fixture/dataset identity binding. |

### 2.3 Output fields

`kind = READ_ONLY_MIGRATION_IMPACT_ANALYSIS`, `executable = false`, `execution_api = null`,
`manifest_schema_version`, `market_source_id_contract_version`,
`preferred_market_source_id_option`, `identity_key_schema_version`, `fixture_identity`,
`dataset_identity`, `baseline_fingerprint` (fixture baseline, order-independent),
`canonical_id_count` / `canonical_ids`, `source_observation_fingerprints`,
`proposed_mapping_count`, `entries[]`, `conflicting_mappings`, `missing_references`,
`unknown_provider_evidence`, `legacy_cross_sport_collisions`, `source_identity_conflicts`,
`store_reference_inventory`, `chain_inconsistencies`, `derived_identity_warnings`,
`classification_histogram`, `authorization_status`, `forbidden_operations`,
`non_executable_marker`.

Each `entries[]` element carries: `existing_canonical_id`,
`proposed_future_source_identity`, `proposed_source_id_kind`, `legacy_source_id_kind`,
`mapping_proposal_reference`, `binding_key_contract_version`, `identity_kind`,
`identity_value`, `provider_id_uniqueness_scope`, `observation_fingerprints`,
`classifications`, `flags`, `affected_evidence_references` (store → JSON pointers),
`missing_references`, `legacy_compatibility_classification`, `estimated_rewrite_scope`.

`estimated_rewrite_scope.canonical_id_rewrites` is **always 0** in this phase.

### 2.4 The six impact classifications

| Classification | Meaning |
| :--- | :--- |
| `UNCHANGED_CANONICAL_ID` | The Canonical ID is untouched (the baseline classification for every entry). |
| `SOURCE_ALIAS_CHANGE_ONLY` | Only the source alias would change; no dependent evidence references the alias. |
| `HISTORICAL_REFERENCE_IMPACT` | Dependent stores reference the alias, so their pointers would need attention. |
| `AMBIGUOUS_MAPPING` | One legacy alias is proposed to several targets, or several aliases to one target. |
| `UNSUPPORTED_EVIDENCE` | The proposed mapping is not backed by sufficient identity evidence (e.g. an unverified provider ID). |
| `BLOCKED_BY_MISSING_AUTHORIZATION` | No authorization exists — always present in this phase. |

`assert_not_executable(manifest)` fails closed if the manifest ever declares itself
executable, exposes an execution API, contains a forbidden key (`apply`, `execute`,
`executor`, `migration_plan`, `operations`, `rewrite`, `mutations`), or carries a callable.

### 2.5 Historical reference analysis (synthetic, read-only)

Seven stores are enumerated in a fixed order, with the reference kind recorded per store:

| Store | Reference kind | Where |
| :--- | :--- | :--- |
| `daily_matches.json` | `CANONICAL_DEFINITION` | `/matches/{i}/id` |
| `team_strength.json` | `NO_MATCH_ID_REFERENCE` | indexed by team name — no match-id references |
| `calibrated.json` | `MATCH_ID_LIST` | `/ids/{i}` |
| `prediction_snapshots.json` | `MATCH_ID_FIELD` | `/snapshots/{sid}/match_id` |
| `odds_snapshots.json` | `MATCH_ID_FIELD` | `/snapshots/{oid}/match_id` |
| `settlements.json` | `MATCH_ID_FIELD` | `/settlements/{tid}/match_id` |
| `evaluation_rows.json` | `MATCH_ID_FIELD` | `/rows/{eid}/match_id` |

The analysis reports: references to existing Canonical IDs, unresolved references
(ids not present in the fixture), collisions with existing Canonical IDs, one-alias-to-many-
targets conflicts, chain inconsistencies (`settlement.match_id` vs `snapshot.match_id`,
`evaluation_row.match_id` vs its snapshot/settlement), and `derived_identity_warnings` for
identifiers that are themselves derived from `match_id` (`snapshot_id`, `evaluation_id`).

> **A changed source alias does not automatically justify a changed Canonical ID.**
> Existing Canonical IDs are kept unchanged wherever possible; the manifest's whole point is
> to show that the dependent chain (snapshots → settlements → evaluation rows) is keyed by
> `match_id` and therefore should not be disturbed.

No rewritten historical dataset is generated or executed in this phase.

---

## 3. Relationship to approval and authorization

| Artifact | What it is | What it is **not** |
| :--- | :--- | :--- |
| Source observation | Read-only evidence about what the provider supplied. | Not an authorization to bind or modify a Canonical identity. |
| Governance proposal | An operator's proposed handling. | Not an approved mapping. |
| Owner approval (Ed25519) | A signed owner decision bound to the identity key and dataset. | Not permission to rewrite historical Canonical data; not a production repair authorization. |
| Migration impact manifest | A read-only analysis artifact. | Not owner approval, not a migration plan, not authorization. |

An impact manifest is not owner approval. A governance proposal is not owner approval. An old
mapping approval is not production repair authorization. No automatic signature migration
exists, and an approval signed under a superseded identity-key contract remains historical
evidence only.

---

## 4. Non-goals

- No modification of real `data/*.json`; no production repair `apply` / `recover`.
- No source-ID rewrite, no Canonical-ID rewrite, no historical migration, no
  `500j` / `500w` format change in the live parsers.
- No change to `same_event`, ingestion identity planning, market admission, final-score
  protection, model mathematics or Elo calibration.
- No fuzzy matching, no invented provider identifiers, no promotion of row positions.
- No production migration executor, no force/bypass flag, no reuse of a production repair
  signature, no automatic approval, no `main` modification, no Git history rewrite.
