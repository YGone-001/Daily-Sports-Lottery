# Source Identity Provenance

**Module**: `utils/source_identity.py`
**Opt-in evidence extraction**: `utils/provider_evidence.py`
**Tests**: `tests/test_source_identity_provenance.py` (PG-01 … PG-08 plus evidence-integrity cases),
`tests/test_provider_identity_evidence.py` (PE-01 … PE-18)
**Schema version**: `PROVENANCE_SCHEMA_VERSION = "2.0.0"` (legacy `1.0.0` is read-only)
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
| `provenance_schema_version` | str | `"2.0.0"` |
| `observation_id` | str | Deterministic; derived from the semantic fingerprint |
| `adapter` | str | `live_football`, `live_basketball`, `market_xml`, `finished_feed`, `html_fallback` |
| `observation_category` | str | `LIVE_FOOTBALL`, `LIVE_BASKETBALL`, `MARKET_XML`, `FINISHED_FEED`, `HTML_FALLBACK`, `SYNTHETIC` |
| `source_namespace` | str | Lexical ID prefix (`500l`, `500lq`, `500j`, `500w`) or `MISSING` |
| `sport` | str | |
| `event_date` | str \| None | |
| `raw_provider_id` | str \| None | **Absent stays `None`** |
| `raw_provider_id_type` | str | `PROVIDER_MATCH_ID`, `COMPETITION_NUMBER`, `HTML_ELEMENT_ID`, `ROW_INDEX`, `NONE`, `UNKNOWN` |
| `raw_provider_id_uniqueness_scope` | str | `VERIFIED_SCOPED`, `UNVERIFIED`, `UNKNOWN`, `NOT_APPLICABLE` (see §3.1) |
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
| `evidence_completeness` | tuple | Per-dimension boolean (includes `provider_id_uniqueness_verified`) |
| `notes` | tuple | Explicit statements of what is *not* established |
| `identity_stability_reason` | str | Why this stability class was chosen |

`semantic_fingerprint()` covers only **what the observation asserts**; `raw_evidence_sha256`
covers the raw bytes/fields. The two are deliberately different quantities (§7). Because the
uniqueness scope is part of what an observation asserts, the *same* raw provider value
recorded as `VERIFIED_SCOPED` and as `UNKNOWN` produces **different** semantic fingerprints.

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

### 3.1 Provider ID uniqueness scope

Three guarantees are **not** equivalent and are never collapsed:

| Guarantee | Meaning | Independent evidence required |
| :--- | :--- | :--- |
| *provider-supplied* | The source really did emit this value. | none — it is directly observable |
| *provider-unique* | Unique within an **explicitly declared** scope. | yes — a documented reference |
| *provider-stable* | Same meaning across refreshes. | yes — a documented reference |

`ProviderIdUniquenessScope` records which one holds:

| Value | Meaning |
| :--- | :--- |
| `VERIFIED_SCOPED` | An independently documented guarantee covers `(adapter, namespace, sport, raw_type)`. |
| `UNVERIFIED` | The provider supplied it, but no documented uniqueness/stability guarantee exists. |
| `UNKNOWN` | The uniqueness scope cannot be determined. Behaves like `UNVERIFIED` for binding purposes. |
| `NOT_APPLICABLE` | The identity key carries no provider identifier (e.g. a competition-number identity). |

`VERIFIED_PROVIDER_ID_SCOPES` is **deliberately empty**: this repository holds no independently
documented uniqueness guarantee for any provider identifier. Consequently every provider
identifier observed today resolves to `UNKNOWN`, and:

- the observation is still **recorded**;
- the resulting identity key is `UNVERIFIED_PROVIDER_ID`, which is **never sufficient**;
- a binding action built on it is refused (`ProposalValidationError` / `LedgerIntegrityError`
  boundary), so it can only be observed, reviewed and used for conflict detection.

A provider identifier is never inferred to be globally unique from its shape, length or
numeric-ness. Adding a `VERIFIED_SCOPED` declaration requires a reviewable
`documentation_reference` in the registry entry.

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

### 5.1 Raw evidence inventory (`PROVIDER_EVIDENCE_INVENTORY`)

`SOURCE_ID_PATTERNS` describes the **derived** ID construction paths. The raw-evidence
inventory is a separate, explicitly declared structure recording, per parser: the exact
parser, the input field or attribute, the observed meaning, evidence availability, stability
limitations, the current data loss, and the safe preservation mechanism.

| Adapter | Parser | Raw identifiers actually present | Currently discarded |
| :--- | :--- | :--- | :--- |
| `live_football` | `_parse_live` | `<tr id="a…">` element id; `order` attribute (竞彩 number) | one of the two (whichever did not become the ID suffix) |
| `live_basketball` | `_parse_lq` | `matchList[_LQ_ID]` (provider match id); `matchList[_LQ_NO]` (竞彩 number) | one of the two |
| `market_xml` | `fetch_jczq_xml` | XML `<m id>` (provider match id); `<m matchnum>` (竞彩 number) | the XML `id` (used only as an aggregation key) |
| `finished_feed` | `_parse_wanchang` | row ordinal only | the `<tr>` `id` attribute is never read, and no competition number exists |
| `html_fallback` | `_parse_jczq_html` | row ordinal only | `data-mid` (if the rendered page carries it) is never read |

> No entry in this inventory asserts that a provider identifier is globally unique or
> stable across refreshes. The inventory records availability, not authority.

### 5.2 Opt-in evidence extraction (`utils/provider_evidence.py`)

`extract_source_evidence(fixture, adapter=…, sport=…)` returns a typed
`SourceEvidenceExtraction` that unpacks to `(normalized_rows, provenance_observations)`, plus a
`SourceProvenanceSidecar`. It runs on an **already obtained** source fixture and:

- captures raw provider values **before** normalization discards them;
- captures only values actually present in the supplied fixture (never synthesizes one);
- keeps the provider field, the HTML element id and the competition number separate;
- stores the exact raw value and the normalized interpretation separately;
- records the parser/extractor identity and a deterministic observation fingerprint;
- never mutates the fixture or the parser's input;
- never changes `utils/fetcher_500`'s return values, and is never called by `scraper.refresh()`;
- persists nothing — no `daily_matches.json` fields, no network, no ledger writes,
  no automatic mapping proposals.

Classification rule used by the extractor (deliberately conservative):

| Raw source | Recorded `raw_provider_id_type` | Uniqueness scope |
| :--- | :--- | :--- |
| HTML `id` attribute | `HTML_ELEMENT_ID` | `UNKNOWN` |
| explicitly named provider field (`<m id>`, `matchList[_LQ_ID]`, `data-mid`) | `PROVIDER_MATCH_ID` | `UNKNOWN` |
| competition number field (`order`, `matchnum`, `_LQ_NO`) | `COMPETITION_NUMBER` | `NOT_APPLICABLE` (scoped to `sport\|date\|number`) |

Because every provider identifier is `UNKNOWN`, the extracted observations can never
authorize an active binding; they are evidence for review and conflict detection only.

The sidecar is **not** part of the runtime match schema: it is a separate object that is never
written into `daily_matches.json`.

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
- No provider identifier is ever promoted to authoritative mapping evidence without an
  independently documented uniqueness scope (see §3.1).
- The provenance layer is **not** the runtime identity authority; that remains
  `utils.ingestion_identity.validate_ingestion_identity`.
- The opt-in extraction path is **not** wired into the live refresh: it changes no parser
  contract, no fetcher return value and no persisted runtime schema.
- The migration designs that follow from these risks are documented in
  `docs/canonical-mapping-governance.md` §9 and `docs/source-id-migration-planning.md`, and are
  **not executed**.
