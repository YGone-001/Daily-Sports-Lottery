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
BINDING_CANDIDATE_FINGERPRINT_CHANGED = "CANDIDATE_FINGERPRINT_CHANGED"
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


def _canonical(path: str) -> str:
    """
    Canonical filesystem form used for containment decisions.

    `realpath` resolves symlinks and Windows directory junctions/reparse points,
    `normcase` makes comparison case-insensitive on Windows and case-sensitive on
    POSIX — exactly the platform's own path semantics.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _is_inside(path: str, root: str) -> bool:
    """True when `path` is `root` itself or lives below it (canonical form)."""
    try:
        return os.path.commonpath([_canonical(path), _canonical(root)]) == _canonical(root)
    except ValueError:
        # Different drives on Windows: cannot be nested.
        return False


def assert_outside_repository(path: str, *, code: str) -> str:
    """
    Reject any path that resolves inside the checkout.

    Containment is decided on **resolved** paths, so a symlink/junction pointing
    back into the repository is refused even when the literal string looks
    external. Ambiguous resolution fails closed.
    """
    try:
        resolved = _canonical(path)
    except OSError as exc:  # pragma: no cover - defensive
        raise EvidenceStoreError("PATH_RESOLUTION_FAILED", f"{path}: {exc}") from None
    if _is_inside(resolved, _repo_root()):
        raise EvidenceStoreError(
            code,
            "path resolves inside the checkout; evidence must live outside the repository",
        )
    return os.path.abspath(os.path.expanduser(path))


def resolve_evidence_dir(configured: str | None = None) -> str:
    """
    解析证据目录。优先级：显式入参 > 环境变量 > 用户主目录默认值。

    位于仓库内的一律拒绝（按**解析后**的真实路径判定，覆盖符号链接、
    Windows 目录联接与大小写差异），也不得依赖 `.gitignore` / `info/exclude`
    来"看起来"没被跟踪。
    """
    candidate = configured or os.environ.get(ENV_VAR)
    if candidate:
        directory = os.path.abspath(os.path.expanduser(candidate))
    else:
        directory = os.path.abspath(os.path.join(os.path.expanduser("~"), DEFAULT_SUBDIR))

    return assert_outside_repository(directory, code="EVIDENCE_DIR_INSIDE_REPOSITORY")


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
    """
    读取并**完整校验**一个分片。

    损坏、结构不符、分片元数据与 `pair_key` 不一致、或其中任一条历史记录
    不合 schema 时一律抛错：绝不静默重置，也绝不用空文件覆盖。
    """
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

    if str(payload.get("schema_version")) != STORE_SCHEMA_VERSION:
        raise EvidenceStoreError("CORRUPT_EVIDENCE_FILE", f"{key}: unsupported shard schema")
    if payload.get("kind") not in (None, STORE_KIND):
        raise EvidenceStoreError("CORRUPT_EVIDENCE_FILE", f"{key}: unexpected shard kind")
    if str(payload.get("pair_key")) != key:
        raise EvidenceStoreError("CORRUPT_EVIDENCE_FILE", f"{key}: pair_key mismatch")

    canonical = payload.get("canonical_ids")
    if not isinstance(canonical, list):
        raise EvidenceStoreError("CORRUPT_EVIDENCE_FILE", f"{key}: canonical_ids missing")
    try:
        derived = pair_key(canonical)
    except EvidenceStoreError as exc:
        raise EvidenceStoreError("CORRUPT_EVIDENCE_FILE", f"{key}: {exc.code}") from None
    if derived != key:
        raise EvidenceStoreError("CORRUPT_EVIDENCE_FILE", f"{key}: canonical ids do not match shard")

    seen: dict[tuple[str, str], str] = {}
    for index, record in enumerate(payload["records"]):
        if not isinstance(record, Mapping):
            raise EvidenceStoreError("CORRUPT_EVIDENCE_FILE", f"{key}[{index}]: not an object")
        try:
            clean = validate_record(record)
        except EvidenceStoreError as exc:
            raise EvidenceStoreError(
                "CORRUPT_EVIDENCE_FILE", f"{key}[{index}]: {exc.code}"
            ) from None
        if sorted(clean["canonical_ids"]) != sorted(canonical):
            raise EvidenceStoreError(
                "CORRUPT_EVIDENCE_FILE", f"{key}[{index}]: record pair differs from shard"
            )
        identity = _record_identity(clean)
        digest = _content_sha(clean)
        if identity in seen and seen[identity] != digest:
            raise EvidenceStoreError(
                "CORRUPT_EVIDENCE_FILE", f"{key}[{index}]: conflicting duplicate identity"
            )
        seen[identity] = digest
    return payload


EVIDENCE_LOCK_NAME = ".evidence.lock"
EVIDENCE_LOCK_TIMEOUT_SECONDS = 10.0


def append_record(record: Mapping[str, Any], *, configured_dir: str | None = None) -> dict[str, Any]:
    """
    追加一条人工决定。返回写入结果摘要。

    - 读-校验-追加-替换整体位于**同一把跨进程排他锁**内：`os.replace` 只保证
      单文件不撕裂，**不**能防止两个写者各自读到旧视图后互相覆盖；
    - 完全相同的记录重复提交 -> `IDEMPOTENT_DUPLICATE`，不产生第二份副本；
    - 同一 (reviewer, decided_at) 但内容不同 -> `CONFLICTING_REWRITE`，拒绝；
    - 两个有效但互斥的决定**都保留**，由读取端报告 `CONFLICTING_LABEL`；
    - 历史记录不可修改、不可删除。
    """
    from utils.atomic_json import atomic_write_json
    from utils.concurrency import SidecarLockBusy, acquire_sidecar_lock

    payload = validate_record(record)
    directory = resolve_evidence_dir(configured_dir)
    record_dir = os.path.join(directory, RECORD_DIRNAME)
    os.makedirs(record_dir, exist_ok=True)

    key = pair_key(payload["canonical_ids"])
    path = _record_path(directory, key)

    try:
        with acquire_sidecar_lock(
            EVIDENCE_LOCK_NAME,
            data_dir=directory,
            timeout=EVIDENCE_LOCK_TIMEOUT_SECONDS,
        ):
            existing = load_pair(directory, key)
            created_shard = existing is None
            if existing is None:
                existing = {
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
                            "path": path,
                        }
                    raise EvidenceStoreError(
                        "CONFLICTING_REWRITE", "same reviewer/time with different content"
                    )

            updated = dict(existing)
            updated["records"] = list(existing["records"]) + [payload]
            atomic_write_json(path, updated)
    except SidecarLockBusy as exc:
        # 失败即关闭：绝不在没有拿到写者锁的情况下覆盖别人刚写入的历史。
        raise EvidenceStoreError("EVIDENCE_WRITER_BUSY", str(exc)) from None

    summary = decision_summary(updated)
    return {
        "status": "APPENDED",
        "pair_key": key,
        "record_count": len(updated["records"]),
        "path": path,
        "shard_created": created_shard,
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
    current_candidate_fingerprint: str | None = None,
) -> dict[str, Any]:
    """
    Classify how a stored decision relates to the **current** dataset.

    Persisting evidence never makes stale evidence valid: `BOUND_TO_CURRENT_CANDIDATE`
    still requires `OPERATOR_REVIEW_REQUIRED` before the decision may be reused,
    because the dataset fingerprint and candidate fingerprint recorded at
    decision time are part of the evidence. Team names are deliberately not an
    input here.

    ``current_candidate_fingerprint`` is optional for backward compatibility; when
    it is supplied the stored value must match, otherwise the binding is reported
    as drifted instead of silently trusted.
    """
    stored_ids = sorted({str(item) for item in (record.get("canonical_ids") or [])})
    current_ids = sorted({str(item) for item in (current_canonical_ids or [])})
    stored_candidate_fingerprint = str(record.get("candidate_fingerprint") or "")

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
    elif (
        current_candidate_fingerprint is not None
        and str(current_candidate_fingerprint) != stored_candidate_fingerprint
    ):
        binding = BINDING_CANDIDATE_FINGERPRINT_CHANGED
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
        "stored_candidate_fingerprint": stored_candidate_fingerprint or None,
        "current_candidate_fingerprint": current_candidate_fingerprint,
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

EXPORT_STATUS_ADVISORY = "COMPLETED_ADVISORY"
EXPORT_STATUS_UNSTABLE = "COMPLETED_BUT_SOURCE_CHANGED_DURING_COPY"


def _sha256_file(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def retain_source_copy(
    source_path: str,
    *,
    configured_dir: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """
    保存被引用的外部来源副本（或至少记录其完整性摘要），供日后复核。

    刻意**不**产生任何溯源声明：不写 reviewer、不写 decided_at、不改
    `verification_status`、不声称有人读过该来源。它只是"字节留存 + 摘要"。
    """
    from utils.atomic_json import atomic_write_json

    directory = resolve_evidence_dir(configured_dir)
    origin = os.path.abspath(os.path.expanduser(source_path))
    if not os.path.isfile(origin):
        raise EvidenceStoreError("SOURCE_REFERENCE_NOT_FOUND", origin)

    digest = _sha256_file(origin)
    store_dir = os.path.join(directory, "source_references")
    os.makedirs(store_dir, exist_ok=True)
    basename = label or os.path.basename(origin) or "source.bin"
    safe = "".join(ch for ch in basename if ch.isalnum() or ch in "._-") or "source.bin"
    target = os.path.join(store_dir, f"{digest[:16]}-{safe}")
    if not os.path.isfile(target):
        shutil.copyfile(origin, target)

    manifest = {
        "kind": "RETAINED_SOURCE_REFERENCE",
        "schema_version": STORE_SCHEMA_VERSION,
        "sha256": digest,
        "size": os.path.getsize(target),
        "stored_as": target,
        "original_path": origin,
        # 明确的非声明：留存字节不等于有人核验过。
        "verified_by_human": False,
        "sets_review_provenance": False,
        "sets_verification_status": False,
    }
    atomic_write_json(os.path.join(store_dir, f"{digest[:16]}.manifest.json"), manifest)
    return manifest


def export_copy(destination: str, *, configured_dir: str | None = None) -> dict[str, Any]:
    """
    只读导出：把分片连同清单复制到目标目录。

    - 导出前逐个**完整校验**源分片，损坏直接拒绝（`CORRUPT_EVIDENCE_FILE`）；
    - 复制后再校验目标与源一致；若复制期间源发生变化，显式标记为
      `COMPLETED_BUT_SOURCE_CHANGED_DURING_COPY`，**不**声称点时间一致；
    - 不改变任何决定，不写入仓库内路径（含符号链接/目录联接解析后位于仓库内的情况）。
    """
    from utils.atomic_json import atomic_write_json

    source = resolve_evidence_dir(configured_dir)
    target_dir = assert_outside_repository(destination, code="EXPORT_INTO_REPOSITORY")

    record_dir = os.path.join(source, RECORD_DIRNAME)
    entries: list[dict[str, Any]] = []
    unstable: list[str] = []
    os.makedirs(target_dir, exist_ok=True)
    if os.path.isdir(record_dir):
        for name in sorted(os.listdir(record_dir)):
            if not name.endswith(".json"):
                continue
            key = name[: -len(".json")]
            # 完整校验（含每条历史记录），损坏即中止，绝不导出半坏集合。
            load_pair(source, key)

            src = os.path.join(record_dir, name)
            before = _sha256_file(src)
            dst = os.path.join(target_dir, name)
            shutil.copyfile(src, dst)
            after = _sha256_file(src)
            copied = _sha256_file(dst)
            if not (before == after == copied):
                unstable.append(name)
            entries.append(
                {"file": name, "sha256": copied, "source_sha256": before, "stable": before == after}
            )

    manifest = {
        "kind": "HUMAN_REVIEW_EVIDENCE_EXPORT",
        "schema_version": STORE_SCHEMA_VERSION,
        "export_status": EXPORT_STATUS_UNSTABLE if unstable else EXPORT_STATUS_ADVISORY,
        "source_directory": source,
        "file_count": len(entries),
        "files": entries,
        "unstable_files": unstable,
        "read_only": True,
        "no_decisions_changed": True,
        # 导出是逐个文件复制的，**不**是跨文件点时间快照。
        "point_in_time_consistent": False,
        "limitation": (
            "per-file copy with digest verification; a concurrent append between two "
            "files would not be represented atomically"
        ),
    }
    atomic_write_json(os.path.join(target_dir, "export_manifest.json"), manifest)
    return manifest
