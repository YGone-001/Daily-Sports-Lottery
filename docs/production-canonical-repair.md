# Auditable Production Canonical Repair Runbook

**Tool Path**: `tools/production_canonical_repair.py`
**Test Suite**: `tests/test_production_canonical_repair.py`
**Scope**: `PRODUCTION_CANONICAL_DEDUP`
**Authorization schema**: `2.0.0` (Ed25519 signed owner approval)
**Governance Status**: `PRODUCTION_REPAIR_AUTHORIZATION = GRANTED_ONLY_BY_SIGNED_OWNER_APPROVAL`

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

## 2. Trust boundary: preparation is not approval

The critical property of this tool is that **preparing** a repair and **approving** a
repair are different acts performed by different parties.

```
  operator                     owner (private key, offline)
     |                                   |
     | 1. prepare  --> prepared.json      |   (unsigned; authorized: false)
     |                                   |
     | 2. approve  <--------------------> |   (Ed25519 sign over canonical payload)
     |        \--> authorization.json     |   (authorized: true; approval block)
     |
     | 3. apply --trusted-public-key <owner public key>
```

1. `prepare` is read-only and derives the exact expected source hash, source record
   count and candidate hash for one collision. Its artifact carries
   `"authorized": false` and `"approval": null`. **It grants nothing.**
2. `approve` signs the artifact's canonical payload with an Ed25519 key the owner holds
   **outside the repository**. The signature is detached and covers the whole artifact
   minus its `approval` block.
3. `apply` accepts the artifact only if that signature verifies against a *separately
   provisioned* trusted public key **and** the artifact is explicitly marked
   `"authorized": true`.

A SHA-256 fingerprint is an integrity check, not an authentication primitive. The
artifact's `authorization_sha256` exists solely to tie a journal entry to the exact
artifact it recorded -- it never authorizes anything.

### Signed bindings

The canonical payload is the artifact minus `approval`, serialized with
`ensure_ascii=False, sort_keys=True, separators=(",", ":")` and UTF-8 encoded. A valid
signature therefore binds:

| Binding | Field |
| :--- | :--- |
| Repair scope | `scope` |
| Canonical target ID | `target_canonical_id` |
| Canonical source SHA-256 | `expected_source_sha256` |
| Expected candidate SHA-256 | `expected_candidate_sha256` |
| Expected source record count | `expected_source_total` |
| Canonical data directory | `data_dir` |
| Migration ID | `migration_id` |
| Approver identity | `operator` |
| Authorization purpose | `purpose` |
| Declaration time / justification | `approved_at`, `reason` |

### Key handling

- `keygen` provisions a fresh Ed25519 keypair. It **refuses any private-key path inside
  the repository or the application workspace.**
- The private key is never generated, read from, or persisted inside the repository.
- Only the public half is distributed; `apply` / `verify` / `recover` require it as
  `--trusted-public-key`.
- If the `cryptography` backend is unavailable, signature verification raises. The tool
  never silently degrades to "trusted".

### Schema migration

Schema `1.0.0` artifacts asserted `"authorized": true` with no signature at all. They are
**permanently barred**: `load_authorization` rejects them with an explicit message. Any
authorization must be re-prepared and re-approved under schema `2.0.0`.

---

## 3. Safety model

1. **Signed owner approval.** No write occurs without an Ed25519 approval whose bound
   fields (table above) all match the exact live dataset. Any drift fails closed before a
   single byte is written.
2. **Mandatory operator identity.** `apply` and `recover` require an explicit
   `--operator`, and it must equal the approver recorded in the signed artifact. A
   missing or mismatched operator is refused.
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
8. **No bypass switches.** There is deliberately no `--force`, `--skip-verification`,
   `--ignore-hash`, `--allow-unsigned` or `--allow-production-without-approval`.
9. **Test-only fault hooks.** The deterministic crash hooks used by the suite are gated
   behind an in-process flag that the production CLI cannot reach: there is no flag, no
   environment variable and no argument that arms them.

---

## 4. Read-only semantics (exact)

| Command | Reads data dir | Writes data dir | Writes elsewhere |
| :--- | :--- | :--- | :--- |
| `inspect` | yes | **no** | nothing |
| `verify-authorization` | no | **no** | nothing |
| `verify` | yes | **no** | nothing |
| `dry-run` | yes | **no** | one preview artifact in `<workspace>/output/` |
| `prepare` | yes | **no** | the artifact at `--out` |
| `approve` | no | **no** | the signed artifact at `--out` |
| `keygen` | no | **no** | the keypair at the given paths |
| `apply` | yes | **yes** (journal, archive, Canonical file) | result artifact in `<workspace>/output/` |
| `recover` | yes | **yes** (journal; Canonical file only on rollback) | nothing |

`dry-run` is **not** globally zero-write: it writes an external preview artifact and says
so in its result (`"read_only_data_dir": true`, `"writes_external_artifact": true`).
The live data directory is never modified by `inspect`, `dry-run` or `verify`.

---

## 5. Seven-phase transaction state machine

```text
[1. PREPARED]
      |  lock acquired; signed authorization bound; pre-store hashes + N recorded
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
  `RECOVERED_FORWARD_COMPLETED`, but only after the full postcondition contract passes.
- **Any unrecognised file hash** -> fails closed with `RecoveryStateError`; no write.

---

## 6. Recovery integrity

`recover` can rewrite the Canonical file, so it is treated as a mutation:

1. **Immutable-store drift is checked first.** A mutated non-Canonical store voids the
   whole transaction (`ImmutableStoreMutationError`).
2. **The transaction must be authenticated.** Recovery requires the same signed
   authorization and explicit `--operator`; the journal must carry the matching
   migration ID and the matching `authorization_sha256`. Unrelated or forged journal
   state is refused.
3. **Evidence is verified before mutation.** The forensic archive raw bytes must hash to
   the pre-state fingerprint recorded at `PREPARED`.
4. **Fingerprints are independently derived.** The expected projected file hash is
   recomputed from the archived raw source. If the journal's recorded
   `projected_file_sha256` disagrees, recovery refuses (`forged or stale journal state`).
5. **Fail-closed UNKNOWN is preserved.** If the active file matches neither the
   independently derived source nor the independently derived projection, recovery
   raises and writes nothing.
6. **No false completion.** The forward path builds the terminal journal entries in
   memory and runs the full postcondition contract over that planned chain *before* the
   `COMPLETION_RECORDED` entry is allowed to become durable.
7. **Evidence is never discarded.** Recovery never deletes the forensic archive.

---

## 7. Verification contract

Two deliberately distinct contracts exist, and a successful basic inspection is **never**
reported as a successful repair verification.

### A. Dataset integrity inspection (`inspect`, `inspect_dataset`)

Canonical schema validity, match count, duplicate-ID detection, basic record consistency.
Result is labelled `"verification": "DATASET_INTEGRITY_INSPECTION"` and carries
`"verified": false, "repair_verified": false`.

### B. Full repair postcondition verification (`verify`)

Requires a signed authorization and the trusted public key, and verifies:

| # | Element |
| :--- | :--- |
| 1 | Verified authorization binding (signature, directory, ids, purpose) |
| 2 | Archived source fingerprint equals the authorized source hash |
| 3 | Projected-file fingerprint independently derived from the archive |
| 4 | Candidate record fingerprint re-derived from archived bytes |
| 5 | `N -> N-1` cardinality against the authorized source count |
| 6 | Unrelated record preservation (byte-identical, original order) |
| 7 | Six immutable-store hashes unchanged |
| 8 | Journal chain valid and terminal at `COMPLETION_RECORDED` |
| 9 | Forensic manifest, evidence records and migration identity |

Missing or unverifiable evidence raises `IncompleteVerificationError`; the CLI reports
exit code `107` and the JSON result carries `"complete": false`. Strict verification is
never silently downgraded.

---

## 8. Enforced postconditions

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

## 9. Field synthesis policy ("Owner Decision A", generalized)

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

## 10. CLI reference

```bash
# Read-only: what collisions exist?  (dataset integrity inspection)
python -m tools.production_canonical_repair inspect --data-dir <data_dir>

# Provision an owner Ed25519 keypair OUTSIDE the repository (once per owner)
python -m tools.production_canonical_repair keygen \
  --private-key-out ~/keys/canonical-owner.pem \
  --public-key-out  ~/keys/canonical-owner.pub.pem

# Read-only: build the UNSIGNED preparation artifact (grants nothing)
python -m tools.production_canonical_repair prepare \
  --data-dir <data_dir> --target-id <canonical_id> \
  --operator <name> --reason "<justification>" \
  --out <prepared.json>

# Owner approval: sign with the external private key
python -m tools.production_canonical_repair approve \
  --prepared <prepared.json> --private-key ~/keys/canonical-owner.pem \
  --out <authorization.json>

# Read-only: confirm the approval verifies against the pinned public key
python -m tools.production_canonical_repair verify-authorization \
  --authorization <authorization.json> \
  --trusted-public-key ~/keys/canonical-owner.pub.pem

# Read-only: preview the projection, diff and projected cardinality
python -m tools.production_canonical_repair dry-run \
  --data-dir <data_dir> --authorization <authorization.json> \
  --trusted-public-key ~/keys/canonical-owner.pub.pem [--workspace <dir>]

# Execute the guarded repair
python -m tools.production_canonical_repair apply \
  --data-dir <data_dir> --authorization <authorization.json> \
  --workspace <dir> --operator <name> \
  --trusted-public-key ~/keys/canonical-owner.pub.pem --confirm-apply

# Read-only FULL repair postcondition audit
python -m tools.production_canonical_repair verify \
  --data-dir <data_dir> --authorization <authorization.json> \
  --trusted-public-key ~/keys/canonical-owner.pub.pem \
  --operator <name> [--workspace <dir>]

# Resolve an interrupted repair (authenticated mutation)
python -m tools.production_canonical_repair recover \
  --data-dir <data_dir> --workspace <dir> \
  --authorization <authorization.json> \
  --trusted-public-key ~/keys/canonical-owner.pub.pem --operator <name>
```

Exit codes: `0` success, `1` verification failed / unexpected error, `101` safety
violation, `102` precondition failure, `103` lock busy, `104` immutable-store mutation,
`106` recovery failure, `107` incomplete verification, `110` authorization failure,
`112` signature verification failure.

Default workspace: `<parent_of_data_dir>/repair_runs/<migration_id>` (git-ignored).

---

## 11. Recovery procedure for the recorded incident

```bash
python -m tools.production_canonical_repair inspect --data-dir data
python -m tools.production_canonical_repair prepare \
  --data-dir data --target-id 500l-2026-10-09-1362422 \
  --operator <name> --reason "legacy canonical duplicate blocking refresh" \
  --out repair_runs/prepared.json
# --- hand prepared.json to the owner; they sign it offline ---
python -m tools.production_canonical_repair approve \
  --prepared repair_runs/prepared.json --private-key <owner-key.pem> \
  --out repair_runs/authorization.json
python -m tools.production_canonical_repair dry-run \
  --data-dir data --authorization repair_runs/authorization.json \
  --trusted-public-key <owner-public.pem>
python -m tools.production_canonical_repair apply \
  --data-dir data --authorization repair_runs/authorization.json \
  --operator <name> --trusted-public-key <owner-public.pem> --confirm-apply
python -m tools.production_canonical_repair verify \
  --data-dir data --authorization repair_runs/authorization.json \
  --operator <name> --trusted-public-key <owner-public.pem>
```

The running application does not need to be restarted: the Canonical file is re-read on
every refresh.

---

## 12. Testing

```bash
python -m pytest -q tests/test_production_canonical_repair.py
python -m pytest -q tests/
```

The suite is fully offline and portable: synthetic fixtures are generated in a temporary
directory, so it never touches `data/`. It covers the authorization trust boundary
(unsigned, forged, tampered, untrusted-key, legacy-schema and missing-key artifacts),
mandatory operator identity, CLI bypass rejection, read-only guarantees, the happy path
with every postcondition, idempotency, lock contention, immutable-store protection,
inspection-vs-verification separation, and all recovery branches.

`cryptography` is required for the signature tests (it is declared in
`requirements.txt`).

---

## 13. Relationship to the staging tools

The staging tools remain the correct instrument for rehearsing a repair against an
isolated replica. This production tool is the only sanctioned way to apply one to the
live dataset, and it is the one that carries an explicit signed owner approval. All three
share one evidence language: `hash_record`, `serialize_record`, `sha256_file` and
`valid_full_time_score` are imported from `tools/staging_canonical_repair.py`, so a
record hashes identically everywhere.
