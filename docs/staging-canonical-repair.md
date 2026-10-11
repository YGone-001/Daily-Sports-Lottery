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

The tool evaluates all target paths against normalized canonical paths and immediately raises `ProtectedPathViolationError` if any target path resolves into:
- Production repository root (`program\Daily-Sports-Lottery`)
- Production data directory (`program\Daily-Sports-Lottery\data`)
- Historical evidence epoch archives (`_quiescent_evidence_backup_*`)
- Path traversal sequences or junction escapes attempting to bypass directory boundaries.

No override flag (e.g. `--allow-production`) is permitted.

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
