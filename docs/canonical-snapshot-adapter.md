# Manifest-Bound Canonical Snapshot Adapter Specification and Guide

## 1. Overview and Purpose

The **Canonical Snapshot Adapter** (`tools/canonical_snapshot_adapter.py`) is an isolated, snapshot-aware remediation engine designed to resolve historical Canonical identity collisions in isolated staging environments without hardcoding fixed whole-file hashes or fixed dataset cardinalities.

### Background and Evolution
- **Historical Staging RC (Phase K)**: Designed specifically for the `EPOCH_1` baseline manifest with fixed assumptions ($193 \to 192$ records, fixed full-file SHA-256).
- **Production Preflight Discovery (Phase L)**: Production services continued running and scheduled provider refreshes, expanding matches from $193$ to $210$ records while preserving the exact duplicate collision at indices $55$ and $56$.
- **Phase M Requirement**: Introduce dynamic dataset binding ($N \to N - 1$), manifest-bound snapshot verification, and owner-approved field attribution while maintaining strict byte-level immutability for all 6 non-Canonical data stores and 100% preservation of unrelated match records.

---

## 2. Architecture and Core Principles

```
+-----------------------------------------------------------------------------------+
|                        Isolated Staging Root Workspace                           |
|                                                                                   |
|  +------------------------+      +--------------------+      +-----------------+  |
|  |  Snapshot Manifest     | ---> |  Preflight Check   | ---> |  OS Byte Lock   |  |
|  |  (7 Stores + Metadata) |      |  (Schema & Hashes) |      |  .refresh.lock  |  |
|  +------------------------+      +--------------------+      +-----------------+  |
|                                                                       |           |
|                                                                       v           |
|  +-----------------------------------------------------------------------------+  |
|  |                  7-Phase Transaction State Machine                           |  |
|  |                                                                             |  |
|  |  1. PREPARED (Bind snapshot manifest, pre-store hashes, attempt scoping)   |  |
|  |  2. EVIDENCE_VERIFIED (Verify Record A & B hashes, forensic archive)        |  |
|  |  3. AUTHORIZED_FOR_STAGING_SIMULATION (Confirm simulation & policy)         |  |
|  |  4. COMMIT_INTENT_DURABLE (Record source, candidate & projected hashes)    |  |
|  |  5. PROJECTION_COMMITTED (Atomic replacement via temporary file & fsync)    |  |
|  |  6. POSTCONDITIONS_VERIFIED (Verify N - 1 total, unique IDs, stores)        |  |
|  |  7. COMPLETION_RECORDED (Durable terminal completion record)                |  |
|  +-----------------------------------------------------------------------------+  |
+-----------------------------------------------------------------------------------+
```

---

## 3. Snapshot Manifest Schema and Trust Model

Manifests bind an isolated dataset snapshot cryptographically. Every manifest must conform to schema version `1.0.0` and contain the following mandatory fields:

```json
{
  "manifest_schema_version": "1.0.0",
  "snapshot_id": "SNAPSHOT-SYNTHETIC-210-FIXTURE",
  "snapshot_classification": "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
  "snapshot_source_description": "Isolated synthetic production drift fixture with +17 test matches",
  "dataset_identity": "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
  "captured_at": "2026-10-11T14:30:00Z",
  "capture_consistency_status": "QUIESCENT_CONSISTENT",
  "source_git_sha": "24cbf38ca93a0827622a3a97d0e8f04183e0556f",
  "daily_matches_sha256": "<64-hex SHA-256>",
  "team_strength_sha256": "<64-hex SHA-256>",
  "calibrated_sha256": "<64-hex SHA-256>",
  "prediction_snapshots_sha256": "<64-hex SHA-256>",
  "odds_snapshots_sha256": "<64-hex SHA-256>",
  "settlements_sha256": "<64-hex SHA-256>",
  "evaluation_rows_sha256": "<64-hex SHA-256>",
  "total_matches": 210,
  "unique_canonical_ids": 209,
  "target_canonical_id": "500l-2026-10-09-1362422",
  "target_record_a_sha256": "c692bbff117559e7f320093f5f49e9478522501de012a66fe22442dfff0624ce",
  "target_record_b_sha256": "bbfaed31fba1df112da92b7f4ec14ec647bdedcaa7734265db69d5ef114748c6",
  "field_mapping_policy_id": "POLICY-OWNER-DECISION-A-20261011",
  "field_mapping_policy_sha256": "5bb33bb20e5ff30ba37d40026e6efd4e5f725a3a7850dfd7f6fa66d92965bf19",
  "manifest_generated_at": "2026-10-11T14:30:00Z",
  "manifest_verification_status": "VERIFIED"
}
```

### Snapshot Classifications
1. `PRESERVED_EPOCH1_FIXTURE`: Pinned historical baseline ($193$ matches).
2. `EXPLICIT_SYNTHETIC_DRIFT_FIXTURE`: Isolated synthetic test fixtures ($210$ matches with synthetic events).
3. `FUTURE_OWNER_APPROVED_FROZEN_SNAPSHOT`: Reserved for future production execution; **strictly prohibited** from execution in Phase M.

---

## 4. Owner Decision A Field Mapping

The candidate projection synthesizes Record A and Record B according to explicit Owner Decision A:

| Field | Source Record | Value | Rationale |
| :--- | :--- | :--- | :--- |
| `id` | Shared | `500l-2026-10-09-1362422` | Target Canonical Identifier |
| `home` | Record B | `巴拉纳竞技` | Standard club name (Record A has abbreviated `巴竞技`) |
| `away` | Record B | `米内罗竞技` | Standard club name (Record A has abbreviated `米竞技`) |
| `status` | Record B | `finished` | Terminal state (Record A is stale `upcoming`) |
| `score` | Record B | `{"ft": [2, 2], "ht": [1, 1]}` | Verified final score (Record A has `null`) |
| `jczq_no` | Record A | `4004` | Authoritative lottery match identifier |
| `odds` | Record A | `{"home_win": 4.75, "draw": 3.40, "away_win": 1.60}` | Market closing odds |
| `odds_updated_at` | Record A | `2026-10-08T21:19:44.432817+08:00` | Original timestamp of market closing odds |

**Approved Candidate SHA-256**: `93a1a6105b0b5db6fe0ce2b7a5d38d5af4a73d3aa980c62c718c7c6464c1785f`

---

## 5. Dynamic $N \to N - 1$ Dataset Transformation

The adapter projects the dataset dynamically:
- **Source Count**: $N$
- **Target Collision**: Exactly $2$ active records at indices `idx_a` and `idx_b`.
- **Projected Count**: $N - 1$
- **Unrelated Matches**: Slices `matches[:idx_a] + [candidate] + matches[idx_a + 1:idx_b] + matches[idx_b + 1:]`.
- **Unrelated Record Loss**: **0** records lost, altered, or reordered.
- **Metadata Semantics**:
  - `meta.total`: Dynamically updated to $N - 1$.
  - `meta.last_refresh`: Preserved byte-for-byte.
  - `meta.sources`: Preserved byte-for-byte.
  - Any extra top-level keys: Preserved exactly.

---

## 6. Six Non-Canonical Stores Immutability

The following 6 data stores are strictly verified for byte-for-byte immutability across the migration:
1. `team_strength.json` (no Elo recalculation)
2. `calibrated.json` (no ID removal or addition)
3. `prediction_snapshots.json` (no snapshot mutation)
4. `odds_snapshots.json` (no odds historical snapshot mutation)
5. `settlements.json` (no settlement rows rewritten)
6. `evaluation_rows.json` (no evaluation gate adjustments)

---

## 7. CLI Subcommands and Usage

```bash
# Generate manifest from an isolated data directory
python -m tools.canonical_snapshot_adapter generate-manifest \
  --data-dir /path/to/isolated/working/data \
  --snapshot-id SNAPSHOT-SYNTHETIC-210 \
  --classification EXPLICIT_SYNTHETIC_DRIFT_FIXTURE \
  --dataset-identity EXPLICIT_SYNTHETIC_DRIFT_FIXTURE \
  --output /path/to/isolated/snapshot_manifest.json

# Preview candidate projection and field diff (read-only)
python -m tools.canonical_snapshot_adapter preview-projection \
  --staging-root /path/to/staging_root \
  --manifest /path/to/snapshot_manifest.json

# Execute migration in isolated staging
python -m tools.canonical_snapshot_adapter staging-apply \
  --staging-root /path/to/staging_root \
  --manifest /path/to/snapshot_manifest.json \
  --simulation

# Recover interrupted staging migration
python -m tools.canonical_snapshot_adapter staging-recover \
  --staging-root /path/to/staging_root \
  --manifest /path/to/snapshot_manifest.json
```

---

## 8. Safety and Path Protection Guarantees

The adapter enforces strict safety boundaries via `assert_safe_path`:
- **Production Roots**: Strictly rejects `Daily-Sports-Lottery`, `Daily-Sports-Lottery/data`, and all descendant paths.
- **Evidence Epochs**: Strictly rejects `EPOCH_0` and `EPOCH_1` preserved archives.
- **Source Repository**: Strictly rejects the git checkout directory as mutable data storage.
- **Production Flags**: Passing `--allow-production` immediately raises `ProtectedPathViolationError`.
