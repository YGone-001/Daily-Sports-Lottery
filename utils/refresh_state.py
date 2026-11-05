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


def read_state(data_dir: str) -> dict[str, Any] | None:
    """
    宽容读取刷新状态。

    文件缺失、损坏、半截、类型不符一律返回 `None`；调用方必须把 `None`
    当作「不可判定」而不是「一致」。
    """
    path = state_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


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
    """标记本次刷新结束，并把结束时七个库的摘要一并记下。"""
    digests = store_digests(data_dir)
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "status": outcome,
        "generation": generation,
        "owner": dict(owner),
        "started_at": owner.get("started_at"),
        "finished_at": _now_iso(),
        "stores": digests,
        "error": error,
    }
    _write_state(data_dir, payload)
    return payload


def consistency(data_dir: str) -> dict[str, Any]:
    """
    只读判定：盘上七个库是否属于同一次成功刷新的完整快照。

    返回 `consistent` 为 True 才表示「可安全当作同一快照读取」。
    `None` 状态（无标记）按**不可判定**处理，不算一致。
    """
    state = read_state(data_dir)
    current = store_digests(data_dir)
    if state is None:
        return {
            "decidable": False,
            "consistent": False,
            "status": None,
            "generation": None,
            "owner": None,
            "reason": "NO_REFRESH_STATE_MARKER",
            "mismatched": sorted(current),
            "missing": sorted(set(PROTECTED_STORES) - set(current)),
            "unexpected": [],
        }

    status = state.get("status")
    recorded_raw = state.get("stores")
    recorded = recorded_raw if isinstance(recorded_raw, dict) else {}
    expected = set(recorded)

    # 标记记录过但盘上已消失 / 盘上摘要与标记不一致 / 标记之后才出现的新库，
    # 三者都说明当前不属于该代的完整快照。
    mismatched = sorted(
        name
        for name, digest in current.items()
        if name in expected and recorded.get(name) != digest
    )
    missing = sorted(expected - set(current))
    unexpected = sorted(set(current) - expected)

    if status == STATUS_IN_PROGRESS:
        reason = "REFRESH_IN_PROGRESS"
    elif status == STATUS_FAILED:
        reason = "REFRESH_FAILED"
    elif not recorded:
        reason = "MARKER_WITHOUT_STORE_DIGESTS"
    elif mismatched or unexpected:
        reason = "STORES_CHANGED_SINCE_MARKER"
    elif missing:
        reason = "STORE_MISSING"
    else:
        reason = "CONSISTENT_SNAPSHOT"

    return {
        "decidable": True,
        "consistent": reason == "CONSISTENT_SNAPSHOT",
        "status": status,
        "generation": state.get("generation"),
        "owner": state.get("owner"),
        "reason": reason,
        "mismatched": mismatched,
        "missing": missing,
        "unexpected": unexpected,
    }
