# Guarded Historical Canonical Projection Staging Migration Tool

**Phase**: `P1-B-INC-I` — Isolated Staging Migration Implementation and Dry-Run Only  
**Target Canonical ID**: `500l-2026-10-09-1362422`  
**Tool Path**: `tools/staging_canonical_repair.py`  
**Test Suite**: `tests/test_staging_canonical_repair.py`  
**Governance Status**: `PROJECTION_STATUS = PROPOSED_NOT_APPROVED` | `PRODUCTION_REPAIR_AUTHORIZATION = NOT_GRANTED`

---

## 1. Overview and Operational Purpose

The `staging_canonical_repair.py` tool provides an auditable, deterministic, fail-closed reconciliation mechanism for resolving the historical Canonical duplicate record affecting match `500l-2026-10-09-1362422` (巴拉纳竞技 vs 米内罗竞技, 巴甲第29轮).

### Core Architectural Guarantees
1. **Isolated Staging Only**: The tool strictly operates against explicitly designated isolated staging directories. It contains hardcoded, fail-closed safety checks that reject any attempt to target production directories, repository roots, or evidence epoch backups.
2. **Strict Store Preservation**: All six non-target persistent stores (`team_strength.json`, `calibrated.json`, `prediction_snapshots.json`, `odds_snapshots.json`, `settlements.json`, `evaluation_rows.json`) must remain 100% byte-identical before and after migration.
3. **Forensic Archival**: Before replacing the active projection, the original raw bytes of `daily_matches.json` and isolated representations of Record A and Record B are durably archived with cryptographic manifests.
4. **Seven-Phase Transaction State Machine**: Coordinates external forensic preservation, an append-only JSONL disposition journal, and atomic single-file runtime replacement (`os.replace`).
5. **Idempotency & Crash Recovery**: Re-running migration against an already reconciled dataset is a non-destructive no-op. Process interruptions are deterministically resolved via `staging-recover`.

---

## 2. Protected Path Boundaries

The tool resolves every target path to a canonical form (`os.path.realpath` +
`os.path.normcase`, so symlinks, NTFS junctions and Windows case-folding are all
resolved first) and immediately raises `ProtectedPathViolationError` if it
resolves into any of:

| Protected root | Purpose |
| :--- | :--- |
| `LEGACY_PROD_REPO_ROOT` (`program\Daily-Sports-Lottery`) | Historical production checkout. A *declaration*: matched by path prefix whether or not the directory exists on this host. |
| `program\_quiescent_evidence_backup_20261010_224710` | Preserved evidence epoch 0 (immutable history). |
| `_quiescent_evidence_backup_epoch1_20261011_014059` | Preserved evidence epoch 1 (immutable history). |
| `SOURCE_REPO_ROOT` (dynamic) | The live Git checkout executing this code, resolved from `__file__`; also covers `SOURCE_DATA_DIR` (`<checkout>/data`) and every descendant. |
| Any path component starting with `_quiescent_evidence_backup` | Catch-all for relocated evidence epochs. |

The legacy roots are historical, machine-specific declarations and are **never
re-pointed at the current checkout** — doing so would silently move the
protection boundary. The dynamic roots are what protect whichever machine is
actually running the tool, with no assumption about its directory layout.

Containment is evaluated **tri-state** (`containment_relation`): `True` inside,
`False` definitively outside (e.g. a different Windows drive), and `None`
undeterminable. An undeterminable relation is **refused** rather than reported
as "outside", so an unresolvable path can never silently receive write
authorization.

Also rejected: path-traversal sequences and junction/symlink escapes that would
bypass a directory boundary, and any target that escapes the declared staging
root.

No override flag (e.g. `--allow-production`) is permitted.

### Windows regression coverage

`tests/test_staging_canonical_repair.py::test_ic_reg_12_windows_junction_escape_rejected`
creates **real NTFS junctions** under `tmp_path` (never at a protected location)
and asserts the specific rejection reason for three cases: a junction into the
live protected checkout (including reaching `<checkout>/data` *through* it), a
junction into a legacy protected root when that location exists, and a junction
escaping the staging root. Junctions are removed with `os.rmdir` in a `finally`
block; `shutil.rmtree` is never used because on this Python version a junction is
not reported as a link and rmtree would recurse into the protected target.

The test carries an explicit `skipif(os.name != "nt", ...)` marker. It is
**skipped on Linux and must run on Windows** — `.github/workflows/ci.yml` fails
the Windows job if that test is skipped or does not report `PASSED`. A green
Linux-only run is never accepted as cross-platform evidence.

---

## 3. Seven-Phase Transaction State Machine

The staging migration executes across seven explicit lifecycle phases recorded to `journal/disposition_journal.jsonl`:

```text
[1. PREPARED]
      │
      ▼
[2. EVIDENCE_VERIFIED]
      │
      ▼
[3. AUTHORIZED_FOR_STAGING_SIMULATION]
      │
      ▼
[4. COMMIT_INTENT_DURABLE] ─── (Failure Boundary: Safe archive rollback or forward complete)
      │
      ▼
[5. PROJECTION_COMMITTED]  ─── (Failure Boundary: Complete postconditions forward)
      │
      ▼
[6. POSTCONDITIONS_VERIFIED]
      │
      ▼
[7. COMPLETION_RECORDED]
```

### State Definitions
- `PREPARED`: Lock acquired on `.refresh.lock`; store existence and baseline hashes recorded.
- `EVIDENCE_VERIFIED`: Record A and Record B cryptographic fingerprints verified against authoritative baselines; final score format verified; source integrity established.
- `AUTHORIZED_FOR_STAGING_SIMULATION`: Staging simulation mode verified (`--simulation`). Note: This represents staging simulation authorization only, never human production approval.
- `COMMIT_INTENT_DURABLE`: External forensic archive written and verified; proposed candidate hash flushed to journal.
- `PROJECTION_COMMITTED`: Atomic pointer swap (`os.replace`) updates `daily_matches.json`.
- `POSTCONDITIONS_VERIFIED`: Full-array uniqueness (192 matches, 192 unique IDs, exactly 1 target record) confirmed; all 6 non-Canonical stores verified byte-identical.
- `COMPLETION_RECORDED`: Final completion result written to `output/staging-apply-result.json`.

---

## 4. Field Synthesis Specification

The candidate projection synthesizes Record A and Record B according to verified source evidence:

| Canonical Field | Record A Value | Record B Value | Candidate Value | Selection Rationale |
| :--- | :--- | :--- | :--- | :--- |
| `id` | `500l-2026-10-09-1362422` | `500l-2026-10-09-1362422` | `500l-2026-10-09-1362422` | Retain shared Canonical ID |
| `home` | `巴竞技` | `巴拉纳竞技` | `巴拉纳竞技` | Full club name from Record B |
| `away` | `米竞技` | `米内罗竞技` | `米内罗竞技` | Full club name from Record B |
| `status` | `upcoming` | `finished` | `finished` | Terminal finished status |
| `score` | `null` | `{"ft": [2, 2], "ht": [1, 1]}` | `{"ft": [2, 2], "ht": [1, 1]}` | Validated final score from Record B |
| `jczq_no` | `4004` | `""` | `4004` | Official lottery competition number from Record A |
| `odds` | `4.75 / 3.40 / 1.60` | `null` | `4.75 / 3.40 / 1.60` | Closing market odds from Record A |
| `odds_updated_at` | `2026-10-08T21:19:44...`| `null` | `2026-10-08T21:19:44...` | Preserves original odds timestamp |

Candidate Record serialized SHA-256: `93a1a6105b0b5db6fe0ce2b7a5d38d5af4a73d3aa980c62c718c7c6464c1785f`.

---

## 5. CLI Commands

### 5.1 Dataset Inspection (Read-Only)
```powershell
python tools/staging_canonical_repair.py inspect --data-dir <path_to_isolated_data>
```

### 5.2 Staging Dry-Run (Simulation Preview)
```powershell
python tools/staging_canonical_repair.py dry-run --staging-dir <path_to_staging_root>
```

### 5.3 Staging Apply (Guarded Migration)
```powershell
python tools/staging_canonical_repair.py staging-apply --staging-dir <path_to_staging_root> --simulation
```

### 5.4 Staging Verification (Postcondition Audit)
```powershell
python tools/staging_canonical_repair.py staging-verify --staging-dir <path_to_staging_root>
```

### 5.5 Staging Crash Recovery
```powershell
python tools/staging_canonical_repair.py staging-recover --staging-dir <path_to_staging_root>
```

---

## 6. Testing

The tool is accompanied by an offline, portable automated test suite covering positive acceptance gates, negative error detection, lock contention, and fault-injection crash recovery:

```powershell
python -m pytest -v tests/test_staging_canonical_repair.py
```
