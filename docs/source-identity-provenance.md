# Source Identity Provenance

**Module**: `utils/source_identity.py`
**Tests**: `tests/test_source_identity_provenance.py` (PG-01 … PG-08 plus evidence-integrity cases)
**Schema version**: `PROVENANCE_SCHEMA_VERSION = "1.0.0"`
**Authorization status**: read-only evidence. No Canonical write path, no repair, no migration.

---

## 1. Purpose and trust boundaries

The provenance layer answers one question only: **what did the provider actually supply,
and what did the existing parser derive from it?** It never decides identity and never
binds anything.

```text
  provider payload
        |
        v
  existing parser / adapter              (utils/fetcher_500.py)
        |
        v
  SourceObservation                      <-- this module (read-only evidence)
        |
        +--> Provenance report (deterministic fingerprints)
        +--> Conflict report  (classification only, no decision)
        |
        v
  ingestion identity planner             (utils/ingestion_identity.py)  <-- runtime authority
```

Three identifiers are **never collapsed into one field**:

| Field | Meaning |
| :--- | :--- |
| `raw_provider_id` | The identifier the provider **actually supplied**. Absent stays absent. |
| `derived_source_id` | The source identifier the **existing parser derived** (e.g. `500l-{date}-{order}`). |
| `canonical_match_id` | A persisted Canonical ID. Only ever copied from an existing Canonical record; never generated here. |

> A source observation is **not** an authorization to bind or modify a Canonical identity.

`evidence_completeness` and `missing_evidence()` state explicitly what the evidence
**establishes** and what it **does not**.

---

## 2. SourceObservation schema

| Field | Type | Notes |
| :--- | :--- | :--- |
| `provenance_schema_version` | str | `"1.0.0"` |
| `observation_id` | str | Deterministic; derived from the semantic fingerprint |
| `adapter` | str | `live_football`, `live_basketball`, `market_xml`, `finished_feed`, `html_fallback` |
| `observation_category` | str | `LIVE_FOOTBALL`, `LIVE_BASKETBALL`, `MARKET_XML`, `FINISHED_FEED`, `HTML_FALLBACK`, `SYNTHETIC` |
| `source_namespace` | str | Lexical ID prefix (`500l`, `500lq`, `500j`, `500w`) or `MISSING` |
| `sport` | str | |
| `event_date` | str \| None | |
| `raw_provider_id` | str \| None | **Absent stays `None`** |
| `raw_provider_id_type` | str | `PROVIDER_MATCH_ID`, `COMPETITION_NUMBER`, `ROW_INDEX`, `NONE`, `UNKNOWN` |
| `derived_source_id` | str \| None | |
| `canonical_match_id` | str \| None | |
| `competition_number_raw` | str \| None | Original value preserved |
| `competition_number_normalized` | str \| None | Normalization result, stored **alongside** the original |
| `home` / `away` | str \| None | |
| `kickoff_time` / `kickoff_time_known` | str \| bool | |
| `identifier_stability` | str | See §3 |
| `stability_scope` | SourceIdentityScope | See §4 |
| `extractor_identity` | str | Parser function + version, e.g. `utils.fetcher_500._parse_live@1` |
| `raw_evidence_sha256` | str | Fingerprint of the raw field values actually read |
| `raw_values` | tuple | Original values, `MISSING` when absent |
| `evidence_completeness` | tuple | Per-dimension boolean |
| `notes` | tuple | Explicit statements of what is *not* established |
| `identity_stability_reason` | str | Why this stability class was chosen |

`semantic_fingerprint()` covers only **what the observation asserts**; `raw_evidence_sha256`
covers the raw bytes/fields. The two are deliberately different quantities (§7).

---

## 3. Identifier stability classification

| Class | Meaning |
| :--- | :--- |
| `PROVIDER_SUPPLIED_UNVERIFIED` | The provider did supply it, but the repository holds **no** independently documented guarantee of global uniqueness or cross-refresh stability. |
| `COMPETITION_NUMBER_SCOPED` | Identity is scoped to `(sport, date, normalized_number)`. |
| `DERIVED_FROM_PROVIDER_FIELDS` | Derived from provider fields, but **which** field is embedded cannot be determined — so its stability cannot be asserted either. |
| `ROW_POSITION_UNSTABLE` | Derived from the **row position** in the response. Never durable identity. |
| `SYNTHETIC_LOCAL` | Locally synthesized; corresponds to no provider value. |
| `UNKNOWN` | Not classifiable. |

> Provider-supplied is **not** equated with globally stable unless that guarantee is
> independently documented. The default for a provider-supplied identifier is therefore
> `PROVIDER_SUPPLIED_UNVERIFIED`.

---

## 4. Scope dimensions

`SourceIdentityScope` records `adapter`, `sport`, `event_date`, `provider_namespace`,
`raw_identifier_type`, and `as_key()` produces the sortable scope tuple.

> The bare numeric competition number is **never** used as a globally unique event key.
> A valid competition identity keeps `(sport, date, normalized_competition_number)`, so
> football and basketball never share an unsafe global numeric namespace.

---

## 5. Complete source identity inventory

Declared in `SOURCE_ID_PATTERNS` and verified against the **real** parsers by
`test_pg_07_source_id_inventory_matches_actual_parsers`.

| # | Adapter | Namespace | Pattern | Embedded raw value | Raw type | Provider ID preserved | Stability |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `live_football` (`_parse_live`) | `500l` | `500l-{date}-{order_or_fid}` | `attrs['order']` (竞彩 number) **or** `<tr id>` provider row id (`fid`) | `COMPETITION_NUMBER` or `PROVIDER_MATCH_ID` | **no** | `COMPETITION_NUMBER_SCOPED` when the suffix normalizes to the competition number, otherwise `DERIVED_FROM_PROVIDER_FIELDS` |
| 2 | `live_basketball` (`_parse_lq`) | `500lq` | `500lq-{date}-{number_or_mid}` | `matchList[_LQ_NO]` (竞彩 number) **or** `matchList[_LQ_ID]` (provider match id) | `COMPETITION_NUMBER` or `PROVIDER_MATCH_ID` | **no** | as above |
| 3 | `market_xml` (`fetch_jczq_xml`) | `500j` | `500j-{date}-{matchnum_or_mid}` | XML `<m matchnum>` (竞彩 number) **or** XML `<m id>` (provider match id) | `COMPETITION_NUMBER` or `PROVIDER_MATCH_ID` | **no** | as above |
| 4 | `finished_feed` (`_parse_wanchang`) | `500w` | `500w-{date}-{row_index}` | Response row ordinal `len(matches)` | `ROW_INDEX` | **no** | `ROW_POSITION_UNSTABLE` |
| 5 | `html_fallback` (`_parse_jczq_html`) | `500j` | `500j-{today}-{row_index}` | Rendered-HTML row ordinal `len(matches)` | `ROW_INDEX` | **no** | `ROW_POSITION_UNSTABLE` |

> The audit found **two** position-derived construction paths, not one: `500w-…-{row_index}`
> and the browser-fallback `500j-…-{row_index}`. The latter shares its namespace prefix with
> the competition-number-based market XML path while meaning something entirely different.

**Systemic finding**: in **all five** paths the provider's own row/match identifier is
**not preserved** in the normalized match row. A derived source ID may therefore embed a
provider identifier the system can no longer audit or re-derive. `observe_source_row`
records this as `raw_provider_id = None` plus an explicit note — it never synthesizes a
provider value.

---

## 6. Confirmed provenance risks

### 6.1 Cross-sport `500j` namespace collision

Football (`jczq`) and basketball (`jclq`) share the `500j` prefix. The two competition-number
sequences are independent per day, so one string can denote two different events in two
different sports. Reproduced with synthetic data (PG-06):

```text
football  market row -> id = 500j-2030-05-01-001  (jczq 001)
basketball market row -> id = 500j-2030-05-01-001  (jclq 001)
=> CROSS_SPORT_SHARED_NAMESPACE (severity AMBIGUOUS)
=> utils.ingestion_identity.plan_ingestion_identity rejects the batch:
   SAME_SOURCE_ALIAS_DIFFERENT_EVENT
```

The two remain **distinct scoped identities**; the shared legacy string never silently
authorizes two different Canonical owners. Behaviour is identical before and after the
ingestion-guard work — this phase documents and classifies it, and **does not rewrite
persisted IDs**.

### 6.2 Finished-feed row-position reuse

`500w-{date}-{row_index}` depends on response ordering. Two refreshes with different row
orders assign the same ID to different matches. Reproduced with synthetic data (PG-08):

```text
pass A rows: [甲队/乙队, 丙队/丁队] -> 500w-…-0 = 甲队/乙队
pass B rows: [丙队/丁队, 甲队/乙队] -> 500w-…-0 = 丙队/丁队   (same ID, different match)
=> ROW_POSITION_REUSE (severity AMBIGUOUS)
=> the ingestion planner refuses to overwrite the unrelated Canonical record
```

Row position is never treated as durable identity, and the planner refuses to use it as
sole ownership proof.

### 6.3 Other detected classes

`detect_all_conflicts` also classifies `STRONG_NUMBER_CONFLICT` (severity `REJECT`) and
`CANONICAL_OWNER_CONFLICT` (severity `AMBIGUOUS`, never auto-resolved to the latest claim).

---

## 7. Evidence integrity

| Requirement | Implementation |
| :--- | :--- |
| Pure / read-only functions | `observe_source_row` and every detector read only; asserted by `test_observations_do_not_mutate_source_rows` |
| Explicit schema version | `PROVENANCE_SCHEMA_VERSION` on every record and report |
| Stable JSON serialization | `canonical_json` (sorted keys, UTF-8, no insignificant whitespace) |
| Deterministic SHA-256 | `fingerprint` / `sha256_text` |
| Order independence | `batch_semantic_fingerprint` and `batch_raw_evidence_fingerprint` hash **sorted** per-observation digests |
| Raw vs normalized distinction | `raw_evidence_sha256` + `raw_values` vs `semantic_fingerprint()` |
| No fabricated provider fields | Absent provider values stay `None` / `MISSING` |
| No network access | The module imports no HTTP client; asserted by `test_observation_layer_performs_no_network_access` |
| Missing evidence stays missing | `evidence_completeness` + `missing_evidence()` |

> A normalized value never replaces the original source evidence: both are stored.

---

## 8. Non-goals

- No Canonical ID rewriting, no historical migration, no `500j` / `500w` ID changes.
- No fuzzy team matching, no invented provider identifiers, no promotion of row positions
  to stable IDs, no assumption that a source ID is globally unique.
- The provenance layer is **not** the runtime identity authority; that remains
  `utils.ingestion_identity.validate_ingestion_identity`.
- The migration designs that follow from these risks are documented in
  `docs/canonical-mapping-governance.md` §9 and are **not executed**.
