"""
持久化人工复核证据存储
======================
把操作者的**人工复核决定**存到仓库之外的、显式配置的本地目录，使其：

- 不进入 Git 跟踪（路径位于仓库外，不依赖 `.gitignore`）；
- 能跨应用重启与数据集刷新存活（按**稳定源记录身份**分片，不按数据集指纹）；
- 原子写入（复用 `utils.atomic_json`）；
- 拒绝损坏记录、拒绝冲突改写、拒绝自动化决定；
- 保留原始 reviewer / source reference / 决定历史；
- 支持只读导出与备份。

它**不**做什么
--------------
- 不做生产授权，不选存活 Canonical ID，不删除记录。
- 不把「已持久化」当作「仍然有效」：持久化、有效性、候选身份、
  数据集新鲜度、生产授权是五个独立维度，详见 :func:`inspect_record`。
- 不打印任何证据正文。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import Any, Mapping, Sequence

STORE_SCHEMA_VERSION = "1.0.0"
STORE_KIND = "DURABLE_HUMAN_REVIEW_EVIDENCE"

ENV_VAR = "DSL_EVIDENCE_DIR"
DEFAULT_SUBDIR = os.path.join(".daily-sports-lottery", "evidence")
RECORD_DIRNAME = "review_records"

#: 记录必须携带的字段。`notes` 可缺省。
REQUIRED_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "candidate_fingerprint",
        "dataset_fingerprint",
        "canonical_ids",
        "decision",
        "reviewer",
        "decided_at",
        "label_provenance",
        "evidence_reference",
    }
)

#: 绑定判定（与有效性、授权分开陈述）。
BINDING_BOUND = "BOUND_TO_CURRENT_CANDIDATE"
BINDING_STALE_FINGERPRINT = "STALE_DATASET_FINGERPRINT"
BINDING_CANDIDATE_CHANGED = "CANDIDATE_ID_CHANGED"
BINDING_CANONICAL_CHANGED = "CANONICAL_IDS_CHANGED"
BINDING_AMBIGUOUS = "AMBIGUOUS_IDENTITY"
BINDING_UNKNOWN_CANDIDATE = "CANDIDATE_NOT_IN_CURRENT_DATASET"


class EvidenceStoreError(Exception):
    """Raised for rejected or unreadable evidence records."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}{(': ' + detail) if detail else ''}")
        self.code = code
        self.detail = detail


def _schema() -> dict[str, Any]:
    """
    Lazily borrow the review schema constants so importing this module from
    ``utils`` never pulls the ``tools`` package into the application import graph.
    """
    from tools.real_sports_duplicate_review import (
        DECIDED_VALUES,
        PROHIBITED_REVIEW_KEYS,
        PROVENANCE_HUMAN,
        REVIEW_SCHEMA_VERSION,
    )

    return {
        "decided_values": DECIDED_VALUES,
        "prohibited_keys": PROHIBITED_REVIEW_KEYS,
        "provenance_human": PROVENANCE_HUMAN,
        "schema_version": REVIEW_SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_evidence_dir(configured: str | None = None) -> str:
    """
    解析证据目录。优先级：显式入参 > 环境变量 > 用户主目录默认值。

    位于仓库内的一律拒绝：证据必须留在 Git 跟踪范围之外，且不得依赖
    `.gitignore` 或 `info/exclude` 来"看起来"没被跟踪。
    """
    candidate = configured or os.environ.get(ENV_VAR)
    if candidate:
        directory = os.path.abspath(os.path.expanduser(candidate))
    else:
        directory = os.path.abspath(os.path.join(os.path.expanduser("~"), DEFAULT_SUBDIR))

    repo = _repo_root()
    if directory == repo or directory.startswith(repo + os.sep):
        raise EvidenceStoreError(
            "EVIDENCE_DIR_INSIDE_REPOSITORY",
            "evidence must live outside the checkout so it cannot become tracked content",
        )
    return directory


def describe_storage(configured: str | None = None) -> dict[str, Any]:
    """只读描述：路径、来源、是否已存在、是否位于仓库外。"""
    explicit = configured or os.environ.get(ENV_VAR)
    directory = resolve_evidence_dir(configured)
    return {
        "kind": STORE_KIND,
        "schema_version": STORE_SCHEMA_VERSION,
        "directory": directory,
        "configured_by": "ARGUMENT" if configured else ("ENV" if explicit else "DEFAULT"),
        "exists": os.path.isdir(directory),
        "outside_repository": True,
        "record_dir": os.path.join(directory, RECORD_DIRNAME),
    }


# ---------------------------------------------------------------------------
# Identity: stable source-record key
# ---------------------------------------------------------------------------

def pair_key(canonical_ids: Sequence[str]) -> str:
    """
    Stable key over the **sorted Canonical ID pair**.

    Canonical IDs identify source records, so the key survives a dataset
    refresh that merely changes the dataset fingerprint. Nothing about team
    names, scores or provider ordering enters this key on purpose.
    """
    cleaned = sorted({str(item) for item in canonical_ids if str(item).strip()})
    if len(cleaned) != 2:
        raise EvidenceStoreError(
            "INVALID_CANONICAL_ID_PAIR",
            f"exactly two canonical ids are required, got {len(cleaned)}",
        )
    digest = hashlib.sha256("\x1f".join(cleaned).encode("utf-8")).hexdigest()
    return f"PK-{digest[:32]}"


def _record_path(evidence_dir: str, key: str) -> str:
    return os.path.join(evidence_dir, RECORD_DIRNAME, f"{key}.json")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """
    严格校验一条人工复核记录。

    复用既有 schema 常量：禁止生产语义字段，禁止自动化溯源。
    """
    if not isinstance(record, Mapping):
        raise EvidenceStoreError("MALFORMED_RECORD", "record must be an object")

    schema = _schema()
    payload = dict(record)
    prohibited = sorted({str(key).lower() for key in payload} & schema["prohibited_keys"])
    if prohibited:
        raise EvidenceStoreError("PROHIBITED_REVIEW_FIELD", ", ".join(prohibited))

    missing = sorted(REQUIRED_RECORD_KEYS - set(payload))
    if missing:
        raise EvidenceStoreError("INCOMPLETE_RECORD", f"missing fields: {missing}")

    if str(payload.get("schema_version")) != schema["schema_version"]:
        raise EvidenceStoreError("UNSUPPORTED_SCHEMA_VERSION", str(payload.get("schema_version")))

    provenance = str(payload.get("label_provenance") or "")
    if provenance != schema["provenance_human"]:
        raise EvidenceStoreError("AUTOMATED_LABEL_REJECTED", provenance or "<empty>")

    decision = str(payload.get("decision") or "")
    if decision not in schema["decided_values"]:
        raise EvidenceStoreError("UNSUPPORTED_DECISION", decision or "<empty>")

    reviewer = str(payload.get("reviewer") or "").strip()
    if not reviewer:
        raise EvidenceStoreError("MISSING_REVIEWER", "a human reviewer identity is required")
    if reviewer.lower() in ("model", "bot", "auto", "automated", "ai", "proxy", "system"):
        raise EvidenceStoreError("AUTOMATED_REVIEWER_REJECTED", reviewer)

    decided_at = str(payload.get("decided_at") or "").strip()
    if not decided_at:
        raise EvidenceStoreError("MISSING_DECIDED_AT", "decision timestamp is required")

    reference = str(payload.get("evidence_reference") or "").strip()
    if not reference:
        raise EvidenceStoreError("MISSING_EVIDENCE_REFERENCE", "source reference is required")

    canonical = payload.get("canonical_ids")
    if not isinstance(canonical, list) or len(canonical) != 2:
        raise EvidenceStoreError("INVALID_CANONICAL_ID_PAIR", repr(canonical))
    pair_key(canonical)

    for field in ("candidate_id", "candidate_fingerprint", "dataset_fingerprint"):
        if not str(payload.get(field) or "").strip():
            raise EvidenceStoreError("INCOMPLETE_RECORD", f"{field} must be non-empty")

    unknown = sorted(set(payload) - REQUIRED_RECORD_KEYS - {"notes"})
    if unknown:
        raise EvidenceStoreError("UNKNOWN_RECORD_FIELD", ", ".join(unknown))

    return payload


def _record_identity(record: Mapping[str, Any]) -> tuple[str, str]:
    """
    一条决定的不可变身份 = (reviewer, decided_at)。

    刻意**不含** decision：同一个人在同一时刻写下不同结论属于改写既有记录，
    必须拒绝，而不是被当成一条新决定。
    """
    return (str(record.get("reviewer")), str(record.get("decided_at")))


def _content_sha(record: Mapping[str, Any]) -> str:
    blob = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def load_pair(evidence_dir: str, key: str) -> dict[str, Any] | None:
    """读取一个分片；损坏时抛错，绝不静默重置。"""
    path = _record_path(evidence_dir, key)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EvidenceStoreError("CORRUPT_EVIDENCE_FILE", f"{key}: {type(exc).__name__}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise EvidenceStoreError("CORRUPT_EVIDENCE_FILE", f"{key}: unexpected shape")
    return payload


def append_record(record: Mapping[str, Any], *, configured_dir: str | None = None) -> dict[str, Any]:
    """
    追加一条人工决定。返回写入结果摘要。

    - 完全相同的记录重复提交 -> `IDEMPOTENT_DUPLICATE`，不产生第二份副本；
    - 同一 (reviewer, decided_at) 但内容不同 -> `CONFLICTING_REWRITE`，拒绝；
    - 历史记录不可修改、不可删除。
    """
    from utils.atomic_json import atomic_write_json

    payload = validate_record(record)
    directory = resolve_evidence_dir(configured_dir)
    record_dir = os.path.join(directory, RECORD_DIRNAME)
    os.makedirs(record_dir, exist_ok=True)

    key = pair_key(payload["canonical_ids"])
    existing = load_pair(directory, key) or {
        "schema_version": STORE_SCHEMA_VERSION,
        "kind": STORE_KIND,
        "pair_key": key,
        "canonical_ids": sorted({str(item) for item in payload["canonical_ids"]}),
        "records": [],
    }

    for prior in existing["records"]:
        if _record_identity(prior) == _record_identity(payload):
            if _content_sha(prior) == _content_sha(payload):
                return {
                    "status": "IDEMPOTENT_DUPLICATE",
                    "pair_key": key,
                    "record_count": len(existing["records"]),
                    "path": _record_path(directory, key),
                }
            raise EvidenceStoreError("CONFLICTING_REWRITE", "same reviewer/time with different content")

    updated = dict(existing)
    updated["records"] = list(existing["records"]) + [payload]
    atomic_write_json(_record_path(directory, key), updated)

    summary = decision_summary(updated)
    return {
        "status": "APPENDED",
        "pair_key": key,
        "record_count": len(updated["records"]),
        "path": _record_path(directory, key),
        # 追加后若该对出现互相矛盾的决定，立即在结果里暴露，绝不在读取端按时间取胜。
        "decision_state": summary["reason"],
        "conflict": summary["conflict"],
    }


def list_pairs(evidence_dir: str | None = None) -> list[dict[str, Any]]:
    """只读枚举全部证据分片（不含记录正文）。"""
    directory = resolve_evidence_dir(evidence_dir)
    record_dir = os.path.join(directory, RECORD_DIRNAME)
    if not os.path.isdir(record_dir):
        return []
    out: list[dict[str, Any]] = []
    for name in sorted(os.listdir(record_dir)):
        if not name.endswith(".json"):
            continue
        key = name[: -len(".json")]
        try:
            payload = load_pair(directory, key)
        except EvidenceStoreError as exc:
            out.append({"pair_key": key, "status": "CORRUPT", "code": exc.code})
            continue
        if payload is None:
            continue
        out.append(
            {
                "pair_key": key,
                "status": "OK",
                "canonical_ids": payload.get("canonical_ids"),
                "record_count": len(payload.get("records") or []),
            }
        )
    return out


def decision_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """
    汇总一个分片的决定。

    同一对 Canonical ID 出现互相矛盾的决定时**不**按时间取胜：
    `effective_decision` 返回 `None` 且 `conflict = True`。
    """
    records = list(payload.get("records") or [])
    decisions = sorted({str(item.get("decision")) for item in records})
    if not records:
        return {"record_count": 0, "decisions": [], "conflict": False, "effective_decision": None}
    conflict = len(decisions) > 1
    return {
        "record_count": len(records),
        "decisions": decisions,
        "conflict": conflict,
        "effective_decision": None if conflict else decisions[0],
        "reason": "CONFLICTING_LABEL" if conflict else "SINGLE_DECISION",
    }


# ---------------------------------------------------------------------------
# Binding vs validity (kept separate on purpose)
# ---------------------------------------------------------------------------

def inspect_record(
    record: Mapping[str, Any],
    *,
    current_canonical_ids: Sequence[str] | None,
    current_candidate_id: str | None,
    current_dataset_fingerprint: str | None,
    candidates_sharing_pair: int = 1,
) -> dict[str, Any]:
    """
    Classify how a stored decision relates to the **current** dataset.

    Persisting evidence never makes stale evidence valid: `BOUND_TO_CURRENT_CANDIDATE`
    still requires `OPERATOR_REVIEW_REQUIRED` before the decision may be reused,
    because the dataset fingerprint recorded at decision time is part of the
    evidence. Team names are deliberately not an input here.
    """
    stored_ids = sorted({str(item) for item in (record.get("canonical_ids") or [])})
    current_ids = sorted({str(item) for item in (current_canonical_ids or [])})

    if candidates_sharing_pair > 1:
        binding = BINDING_AMBIGUOUS
    elif not current_ids:
        binding = BINDING_UNKNOWN_CANDIDATE
    elif current_ids != stored_ids:
        binding = BINDING_CANONICAL_CHANGED
    elif not current_candidate_id:
        binding = BINDING_UNKNOWN_CANDIDATE
    elif str(record.get("candidate_id")) != str(current_candidate_id):
        binding = BINDING_CANDIDATE_CHANGED
    elif str(record.get("dataset_fingerprint")) != str(current_dataset_fingerprint):
        binding = BINDING_STALE_FINGERPRINT
    else:
        binding = BINDING_BOUND

    needs_reattachment = binding != BINDING_BOUND
    return {
        "pair_key": pair_key(record["canonical_ids"]),
        "binding": binding,
        "stored_candidate_id": record.get("candidate_id"),
        "current_candidate_id": current_candidate_id,
        "stored_dataset_fingerprint": record.get("dataset_fingerprint"),
        "current_dataset_fingerprint": current_dataset_fingerprint,
        # 五个维度分别陈述：已持久化 ≠ 当前有效 ≠ 已重绑定 ≠ 已授权生产。
        "persisted": True,
        "valid_for_current_dataset": binding == BINDING_BOUND,
        "auto_reattached": False,
        "reattachment_required": needs_reattachment,
        "next_required_action": (
            "REATTACH_BY_OPERATOR_REQUIRED" if needs_reattachment else "OPERATOR_REVIEW_REQUIRED"
        ),
        "production_authorized": False,
    }


# ---------------------------------------------------------------------------
# Read-only export / backup
# ---------------------------------------------------------------------------

def export_copy(destination: str, *, configured_dir: str | None = None) -> dict[str, Any]:
    """
    只读导出：把分片连同清单复制到目标目录。

    导出不改变任何决定，不写入仓库内路径，不生成新的证据身份。
    """
    from utils.atomic_json import atomic_write_json

    source = resolve_evidence_dir(configured_dir)
    target_dir = os.path.abspath(os.path.expanduser(destination))
    repo = _repo_root()
    if target_dir == repo or target_dir.startswith(repo + os.sep):
        raise EvidenceStoreError("EXPORT_INTO_REPOSITORY", target_dir)

    record_dir = os.path.join(source, RECORD_DIRNAME)
    entries: list[dict[str, Any]] = []
    os.makedirs(target_dir, exist_ok=True)
    if os.path.isdir(record_dir):
        for name in sorted(os.listdir(record_dir)):
            if not name.endswith(".json"):
                continue
            src = os.path.join(record_dir, name)
            dst = os.path.join(target_dir, name)
            shutil.copyfile(src, dst)
            with open(dst, "rb") as handle:
                entries.append({"file": name, "sha256": hashlib.sha256(handle.read()).hexdigest()})

    manifest = {
        "kind": "HUMAN_REVIEW_EVIDENCE_EXPORT",
        "schema_version": STORE_SCHEMA_VERSION,
        "source_directory": source,
        "file_count": len(entries),
        "files": entries,
        "read_only": True,
        "no_decisions_changed": True,
    }
    atomic_write_json(os.path.join(target_dir, "export_manifest.json"), manifest)
    return manifest
