# Auditable Production Canonical Repair Runbook

**Tool Path**: `tools/production_canonical_repair.py`
**Test Suite**: `tests/test_production_canonical_repair.py`
**Scope**: `PRODUCTION_CANONICAL_DEDUP`
**Governance Status**: `PRODUCTION_REPAIR_AUTHORIZATION = GRANTED_VIA_SIGNED_ARTIFACT`

---

## 1. Why this tool exists

`utils.scraper` enforces a fail-closed invariant before every write: the Canonical
tracking set in `data/daily_matches.json` may never contain two records sharing one
`id` (`scraper.py`, step 0 of `_refresh_impl`).

A legacy duplicate (`500l-2026-10-09-1362422`, one abbreviated live-score alias plus
one finished-feed record) was persisted **before** that guard existed. Because the two
records disagree on the strong competition number (`4004` vs. empty),
`utils.match_identity.same_event` falls back to exact club-name matching and classifies
them as *different* events that nevertheless share one id.

The result was a permanent outage: the guard rejected the dataset at load time, so
`POST /api/refresh` returned HTTP 500 and the 1800-second background auto-sync failed on
every cycle. Nothing was corrupting further -- the system simply refused to run.

The two pre-existing tools deliberately cannot help here:

| Tool | Production behaviour |
| :--- | :--- |
| `tools/staging_canonical_repair.py` | `PRODUCTION_REPAIR_AUTHORIZATION = NOT_GRANTED`; hard-rejects the production repo and data directory |
| `tools/canonical_snapshot_adapter.py` | Rejects `Daily-Sports-Lottery/data`; rejects `LIVE_PRODUCTION_RUNTIME_SNAPSHOT` |

This module closes that operational gap. It is **not** a general-purpose editor: it
performs exactly one operation -- the `N -> N - 1` de-duplication of a single Canonical
identity -- and refuses everything else.

---

## 2. Safety model

1. **Owner authorization artifact.** No write occurs without a signed authorization
   whose `data_dir`, `expected_source_sha256`, `target_canonical_id` and
   `expected_candidate_sha256` all bind to the exact live dataset. Any drift fails
   closed before a single byte is written.
2. **Explicit confirmation.** `apply` additionally requires `--confirm-apply`, and the
   `--operator` must match the operator recorded in the artifact.
3. **Refusal of preserved evidence.** Evidence-epoch archives
   (`_quiescent_evidence_backup*`) and filesystem/drive roots are rejected.
4. **Workspace containment.** The journal/archive/output workspace must be neither
   inside the data directory nor a parent of it.
5. **Shared cross-process lock.** The transaction holds the very same
   `<data_dir>/.refresh.lock` byte-range lock used by `utils.concurrency`, so a provider
   refresh and a repair can never interleave. A concurrent refresh raises
   `RefreshBusyError` and is skipped, and vice versa.
6. **Dynamic, drift-tolerant invariants.** Nothing is hardcoded: dataset cardinality is
   discovered at runtime, so the tool keeps working as the live dataset grows.
7. **Independently derived postconditions.** The expected projected file bytes are
   re-derived from the archived raw source rather than trusted from the active file.

---

## 3. Seven-phase transaction state machine

```text
[1. PREPARED]
      |  lock acquired; authorization bound; pre-store hashes + N recorded
      v
[2. EVIDENCE_VERIFIED]
      |  exactly one collision group == target; exactly 2 records;
      |  a terminal finished record with a strictly valid score exists;
      |  forensic archive written and verified
      v
[3. AUTHORIZED_FOR_PRODUCTION_REPAIR]
      |  operator, authorization fingerprint, approval time and reason recorded
      v
[4. COMMIT_INTENT_DURABLE]
      |  candidate synthesized and matched against the authorized hash;
      |  projected bytes independently derived; source/projected/candidate hashes journaled
      v
[5. PROJECTION_COMMITTED]
      |  stale-source check, then atomic os.replace
      v
[6. POSTCONDITIONS_VERIFIED]
      |  dynamic N-1 invariants + six immutable stores + derived-hash re-derivation
      v
[7. COMPLETION_RECORDED]
      |  result written to <workspace>/output/production-apply-result.json
```

Fault boundaries are resolvable by `recover`:

- **Before `COMMIT_INTENT_DURABLE`** -> `RECOVERED_PRE_COMMIT_CLEAN` (no writes).
- **At `COMMIT_INTENT_DURABLE`, file still baseline** -> `RECOVERED_ROLLED_BACK`
  (restored byte-identically from the forensic archive).
- **At/after `PROJECTION_COMMITTED`, file already projected** ->
  `RECOVERED_FORWARD_COMPLETED`.
- **Any unrecognised file hash** -> fails closed with `RecoveryStateError`; no write.

---

## 4. Enforced postconditions

| # | Invariant |
| :--- | :--- |
| 1 | `total_matches == N - 1` and `unique_ids_count == N - 1` |
| 2 | Zero duplicate Canonical IDs remain |
| 3 | The target id occurs exactly once, at the first duplicate's position |
| 4 | The target record hashes to the authorized candidate |
| 5 | Active file hash equals the projection independently derived from the raw archive |
| 6 | All unrelated records are preserved byte-for-byte and keep their relative order |
| 7 | `meta.total` updated; `meta.last_refresh` and `meta.sources` preserved |
| 8 | Unknown top-level keys preserved exactly |
| 9 | All six non-Canonical stores byte-identical (checked before and after) |
| 10 | Journal chain valid and terminal at `COMPLETION_RECORDED` |

---

## 5. Field synthesis policy ("Owner Decision A", generalized)

The candidate is synthesized deterministically, then **must** match the authorized hash:

1. Rank records: a terminal `finished` record with a strictly valid full-time score
   wins; ties break toward the most complete (longest) club names, preferring the
   standard full name over an abbreviated alias.
2. Start from the highest-ranked record.
3. Fill only **empty** fields from the siblings -- populated fields are never
   overwritten.

For the historical collision this yields the previously approved record exactly
(SHA-256 `93a1a6105b0b5db6fe0ce2b7a5d38d5af4a73d3aa980c62c718c7c6464c1785f`): standard
club names and the terminal 2-2 result from the finished feed, plus the competition
number `4004` and the closing odds from the live-score alias.

---

## 6. CLI reference

```bash
# Read-only: what collisions exist?
python -m tools.production_canonical_repair inspect --data-dir <data_dir>

# Read-only: build the signed authorization artifact (the owner decision)
python -m tools.production_canonical_repair authorize \
  --data-dir <data_dir> --target-id <canonical_id> \
  --operator <name> --reason "<justification>" \
  --out <authorization.json>

# Read-only: preview the projection, diff and projected cardinality
python -m tools.production_canonical_repair dry-run \
  --data-dir <data_dir> --authorization <authorization.json> [--workspace <dir>]

# Execute the guarded repair
python -m tools.production_canonical_repair apply \
  --data-dir <data_dir> --authorization <authorization.json> \
  --workspace <dir> --operator <name> --confirm-apply

# Read-only postcondition audit
python -m tools.production_canonical_repair verify \
  --data-dir <data_dir> [--authorization <authorization.json>]

# Resolve an interrupted repair
python -m tools.production_canonical_repair recover \
  --data-dir <data_dir> --workspace <dir>
```

Exit codes: `0` success, `101` safety violation, `102` precondition failure,
`103` lock busy, `104` immutable-store mutation, `106` recovery failure,
`110` authorization failure.

Default workspace: `<parent_of_data_dir>/repair_runs/<migration_id>` (git-ignored).

---

## 7. Recovery procedure for the recorded incident

```bash
python -m tools.production_canonical_repair inspect --data-dir data
python -m tools.production_canonical_repair authorize \
  --data-dir data --target-id 500l-2026-10-09-1362422 \
  --operator <name> --reason "legacy canonical duplicate blocking refresh" \
  --out repair_runs/authorization.json
python -m tools.production_canonical_repair dry-run \
  --data-dir data --authorization repair_runs/authorization.json
python -m tools.production_canonical_repair apply \
  --data-dir data --authorization repair_runs/authorization.json \
  --operator <name> --confirm-apply
python -m tools.production_canonical_repair verify \
  --data-dir data --authorization repair_runs/authorization.json
```

The running application does not need to be restarted: the Canonical file is re-read on
every refresh.

---

## 8. Testing

```bash
python -m pytest -v tests/test_production_canonical_repair.py
```

The suite is fully offline and portable: synthetic fixtures are generated in a temporary
directory, so it never touches `data/`. It covers safety refusals, authorization
binding, read-only guarantees, the happy path with every postcondition, idempotency,
lock contention, immutable-store protection, fault injection and all recovery branches.

---

## 9. Relationship to the staging tools

The staging tools remain the correct instrument for rehearsing a repair against an
isolated replica. This production tool is the only sanctioned way to apply one to the
live dataset, and it is the one that carries an explicit owner authorization. All three
share one evidence language: `hash_record`, `serialize_record`, `sha256_file` and
`valid_full_time_score` are imported from `tools/staging_canonical_repair.py`, so a
record hashes identically everywhere.
