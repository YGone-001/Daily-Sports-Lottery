"""
刷新所有权与跨文件一致性标记
============================
本模块只做一件事：让「谁在刷新」和「盘上这七个库是否属于同一次刷新」可被
**只读地**观测到。

它刻意**不**声称七文件原子性
------------------------------
七个 JSON 各自通过 :func:`utils.atomic_json.atomic_write_json` 原子替换，
但一次刷新跨越多个文件、耗时可达数分钟，因此七个文件之间**不是**一个事务。
本模块用「代」(generation) 记录每次刷新的开始/结束与结束时各库的摘要，
读方据此判断当前盘上内容是否属于**同一次**成功刷新，而不是假装它们同时落盘。

语义
----
- `status = IN_PROGRESS`：有刷新正在写，盘上可能是混合状态。
- `status = COMPLETED` 且 `digests` 与盘上摘要全部一致：属于同一代的完整快照。
- `status = COMPLETED` 但摘要不一致，或 `status = FAILED`：混合或未完成状态。

写入时机
--------
状态文件只允许在**持有刷新锁**时写入，因此它本身是单写者产物。

约束
----
- 不写入 `data/` 以外的位置，不新增受保护存储，不改动七个库的内容。
- 读取函数对损坏/缺失/半截内容一律降级返回，绝不抛出。
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from typing import Any, Mapping

STATE_FILENAME = ".refresh_state.json"
STATE_SCHEMA_VERSION = "1.0.0"

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"

#: 受保护存储的固定顺序。用于摘要比较，顺序本身不影响结果。
PROTECTED_STORES: tuple[str, ...] = (
    "daily_matches.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
    "team_strength.json",
    "calibrated.json",
)


def state_path(data_dir: str) -> str:
    return os.path.join(data_dir, STATE_FILENAME)


def owner_record(action: str) -> dict[str, Any]:
    """标识当前写入者。`pid` + `host` 让第二个实例能说出锁被谁持有。"""
    return {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "action": str(action),
        "started_at": _now_iso(),
    }


def _now_iso() -> str:
    # 与项目其它运行时戳保持一致：北京时间（UTC+8，无夏令时）。
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).isoformat()


STATUS_KNOWN = frozenset({STATUS_IN_PROGRESS, STATUS_COMPLETED, STATUS_FAILED})

#: 一致性判定结果代码。除 `CONSISTENT_SNAPSHOT` 外，其余都不允许被读作"同一快照"。
REASON_CONSISTENT = "CONSISTENT_SNAPSHOT"
REASON_NO_MARKER = "NO_REFRESH_STATE_MARKER"
REASON_UNREADABLE = "REFRESH_STATE_UNREADABLE"
REASON_MALFORMED = "MALFORMED_REFRESH_MARKER"
REASON_UNSUPPORTED = "UNSUPPORTED_MARKER_SCHEMA"
REASON_UNKNOWN_STATUS = "UNKNOWN_REFRESH_STATUS"
REASON_INCOMPLETE_MARKER = "INCOMPLETE_MARKER"
REASON_NO_DIGESTS = "MARKER_WITHOUT_STORE_DIGESTS"
REASON_IN_PROGRESS = "REFRESH_IN_PROGRESS"
REASON_FAILED = "REFRESH_FAILED"
REASON_STORE_MISSING = "STORE_MISSING"
REASON_STORES_CHANGED = "STORES_CHANGED_SINCE_MARKER"
REASON_GENERATION_CHANGED = "GENERATION_CHANGED_DURING_READ"


def inspect_state(data_dir: str) -> dict[str, Any]:
    """
    读取刷新状态并区分「缺失」与「存在但不可读/不合契约」。

    `read_state()` 只返回 ``dict | None``，无法表达"损坏"；一致性判定必须
    能区分二者，否则会把损坏标记静默当成"没有标记"。
    """
    path = state_path(data_dir)
    if not os.path.exists(path):
        return {"state": None, "error": REASON_NO_MARKER}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"state": None, "error": REASON_UNREADABLE}
    if not isinstance(payload, dict):
        return {"state": None, "error": REASON_MALFORMED}

    if payload.get("schema_version") != STATE_SCHEMA_VERSION:
        return {"state": payload, "error": REASON_UNSUPPORTED}
    status = payload.get("status")
    if status not in STATUS_KNOWN:
        return {"state": payload, "error": REASON_UNKNOWN_STATUS}
    if not isinstance(payload.get("generation"), int):
        return {"state": payload, "error": REASON_MALFORMED}
    stores = payload.get("stores")
    if stores is None or not isinstance(stores, dict):
        return {"state": payload, "error": REASON_MALFORMED}
    if status == STATUS_IN_PROGRESS:
        # 进行中的标记尚未产生摘要，结构合法即可；一致性由状态本身否决。
        return {"state": payload, "error": None}
    if set(stores) != set(PROTECTED_STORES):
        return {"state": payload, "error": REASON_INCOMPLETE_MARKER}
    if not stores:
        return {"state": payload, "error": REASON_NO_DIGESTS}
    # `None` = 该库在完成时刻不存在（由 complete() 显式记录），不是格式错误。
    if any(value is not None and not isinstance(value, str) for value in stores.values()):
        return {"state": payload, "error": REASON_MALFORMED}
    return {"state": payload, "error": None}


def read_state(data_dir: str) -> dict[str, Any] | None:
    """
    宽容读取刷新状态（保持既有签名）。

    文件缺失、损坏、半截、类型不符一律返回 `None`；调用方必须把 `None`
    当作「不可判定」而不是「一致」。需要区分原因时用 :func:`inspect_state`。
    """
    return inspect_state(data_dir)["state"]


def store_digests(data_dir: str) -> dict[str, str]:
    """对七个受保护存储计算 SHA-256；缺失的文件不出现在结果里。"""
    digests: dict[str, str] = {}
    for name in PROTECTED_STORES:
        try:
            with open(os.path.join(data_dir, name), "rb") as handle:
                digests[name] = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            continue
    return digests


def _write_state(data_dir: str, payload: Mapping[str, Any]) -> None:
    from utils.atomic_json import atomic_write_json

    atomic_write_json(state_path(data_dir), dict(payload))


def begin(data_dir: str, *, owner: Mapping[str, Any]) -> int:
    """
    标记一次刷新开始，返回本次的代号。

    代号在上一代基础上 +1；上一代内容损坏或缺失时从 1 重新开始
    （只影响可观测性，不参与任何身份判定）。
    """
    previous = read_state(data_dir) or {}
    try:
        generation = int(previous.get("generation") or 0) + 1
    except (TypeError, ValueError):
        generation = 1
    _write_state(
        data_dir,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "status": STATUS_IN_PROGRESS,
            "generation": generation,
            "owner": dict(owner),
            "started_at": owner.get("started_at"),
            "finished_at": None,
            "stores": {},
            "error": None,
        },
    )
    return generation


def complete(
    data_dir: str,
    *,
    owner: Mapping[str, Any],
    generation: int,
    outcome: str,
    error: str | None = None,
) -> dict[str, Any]:
    """
    标记本次刷新结束，并把结束时**七个**库的摘要一并记下。

    缺失的库记为 `None`，从而让一致性判定能明确区分"该库当时不存在"，
    而不是把缺失从标记里悄悄省略掉。
    """
    present = store_digests(data_dir)
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "status": outcome,
        "generation": generation,
        "owner": dict(owner),
        "started_at": owner.get("started_at"),
        "finished_at": _now_iso(),
        "stores": {name: present.get(name) for name in PROTECTED_STORES},
        "error": error,
    }
    _write_state(data_dir, payload)
    return payload


def consistency(data_dir: str) -> dict[str, Any]:
    """
    只读判定：盘上七个库是否属于同一次**成功**刷新的完整快照。

    这是一个**顾问性**观测，不是事务保证：

    - 只有 ``consistent = True`` 才表示"此刻看起来属于同一代"；
    - 任何时刻都不代表后续读一定一致——非刷新写入（例如 HTTP GET 物化预测快照）
      会立刻让已发布的摘要失效；
    - **不**声称七文件原子性。

    失败即关闭：标记缺失/损坏/字段不全/状态未知/缺库/摘要不符/读取期间代号变化，
    一律返回 ``consistent = False``。
    """
    first = inspect_state(data_dir)
    state = first["state"]
    status = state.get("status") if isinstance(state, dict) else None
    generation = state.get("generation") if isinstance(state, dict) else None
    owner = state.get("owner") if isinstance(state, dict) else None

    current = store_digests(data_dir)
    # 读完摘要再读一次标记：若期间换代或改写，则本次观测本身是撕裂的。
    second = inspect_state(data_dir)

    base = {
        "advisory": True,
        "permanent_guarantee": False,
        "seven_file_atomicity": False,
        "status": status,
        "generation": generation,
        "owner": owner,
        "mismatched": [],
        "missing": sorted(set(PROTECTED_STORES) - set(current)),
        "unexpected": [],
    }

    if first["error"]:
        reason = first["error"]
        return dict(base, decidable=(reason not in (REASON_NO_MARKER, REASON_UNREADABLE)), consistent=False, reason=reason)

    recorded = state.get("stores") or {}
    expected = set(PROTECTED_STORES)

    mismatched = sorted(
        name for name, digest in current.items() if recorded.get(name) != digest
    )
    unexpected = sorted(set(current) - expected)
    missing = sorted(expected - set(current))
    base.update(mismatched=mismatched, missing=missing, unexpected=unexpected)

    if status == STATUS_IN_PROGRESS:
        reason = REASON_IN_PROGRESS
    elif status == STATUS_FAILED:
        reason = REASON_FAILED
    elif not recorded:
        reason = REASON_NO_DIGESTS
    elif missing:
        reason = REASON_STORE_MISSING
    elif mismatched or unexpected:
        reason = REASON_STORES_CHANGED
    elif second["state"] is None or second["state"].get("generation") != generation:
        reason = REASON_GENERATION_CHANGED
    elif second["state"].get("status") != status:
        reason = REASON_GENERATION_CHANGED
    elif second["error"]:
        reason = second["error"]
    else:
        reason = REASON_CONSISTENT

    return dict(
        base,
        decidable=True,
        consistent=reason == REASON_CONSISTENT,
        reason=reason,
        observed_generation_after_digest=(second["state"] or {}).get("generation"),
    )
