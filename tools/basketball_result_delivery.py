"""只读篮球赛果投递诊断 CLI。

用法::

    python -m tools.basketball_result_delivery --data-dir <DATA_DIR> \
        [--observations <json>] [--now <ISO-8601>] [--output <json>] [--markdown <md>]

只读取 daily_matches / prediction_snapshots / settlements；不联网、不获取写锁、
不创建结算、不修改任何存储。输出文件不得位于 DATA_DIR 内。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from utils.result_delivery import (
    build_basketball_result_delivery,
    summarize_result_delivery,
)

EXIT_OK = 0
EXIT_DATA_UNAVAILABLE = 2


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _real(value: str) -> str:
    return os.path.normcase(os.path.realpath(value))


def _inside(path: Path, root: Path) -> bool:
    resolved = _real(str(path))
    base = _real(str(root))
    try:
        return os.path.commonpath([resolved, base]) == base
    except ValueError:
        return False


def _next_action(target: dict) -> str:
    delivery = (target.get("result_delivery") or {}).get("status")
    if delivery == "SOURCE_RETURNED_CONFIRMED_FINAL_RESULT":
        return "EXISTING_CONTROLLED_REFRESH_CAN_SETTLE"
    if delivery == "SOURCE_RECORD_PARSED_BUT_NOT_MATCHED":
        return "RESOLVE_IDENTITY_EVIDENCE"
    if target.get("is_nba"):
        request = target.get("evidence_request") or {}
        return request.get("state") or "PRIOR_PROVIDER_BINDING_REQUIRED"
    return "SOURCE_NOT_AVAILABLE"


def _markdown(report: dict) -> str:
    observation = report.get("observations") or {}
    lines = [
        "# 篮球赛果投递诊断（只读）",
        "",
        f"- 生成时间：`{report.get('generated_at')}`",
        f"- 观测作用域：`{observation.get('scope')}`（进程内，不跨进程共享）",
        f"- 篮球赛果源：`live_basketball`",
        f"- 通用完场源 `wanchang`：`{observation.get('generic_finished_source_scope')}`",
        f"- 待处理目标：**{report.get('pending_total')}**（NBA {report.get('nba_pending')} / 非 NBA {report.get('non_nba_pending')}）",
        f"- 网络请求：`{report.get('network_requests_performed')}`；写入：`{report.get('writes_performed')}`",
        "",
        "## 来源类别计数",
        "",
        "| 类别 | 数量 |",
        "| --- | ---: |",
    ]
    for status, count in (report.get("status_counts") or {}).items():
        lines.append(f"| `{status}` | {count} |")
    lines += [
        "",
        "## NBA 待处理目标",
        "",
        "| 比赛 ID | 日期 | 联赛 | 待处理原因 | 投递类别 | 提供商绑定 | 下一步 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for target in report.get("targets") or []:
        if not target.get("is_nba"):
            continue
        binding = target.get("provider_binding") or {}
        lines.append(
            "| `{id}` | {date} | {league} | `{reason}` | `{delivery}` | `{binding}` | `{action}` |".format(
                id=target.get("match_id"),
                date=target.get("date"),
                league=target.get("league"),
                reason=target.get("pending_reason"),
                delivery=(target.get("result_delivery") or {}).get("status"),
                binding=binding.get("state"),
                action=_next_action(target),
            )
        )
    lines += [
        "",
        "## 非 NBA 待处理目标",
        "",
        "| 比赛 ID | 日期 | 联赛 | 待处理原因 | 投递类别 | 结果路径 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for target in report.get("targets") or []:
        if target.get("is_nba"):
            continue
        path = target.get("result_path") or {}
        lines.append(
            "| `{id}` | {date} | {league} | `{reason}` | `{delivery}` | `{path}` |".format(
                id=target.get("match_id"),
                date=target.get("date"),
                league=target.get("league"),
                reason=target.get("pending_reason"),
                delivery=(target.get("result_delivery") or {}).get("status"),
                path=path.get("state"),
            )
        )
    lines += ["", "## 说明", ""]
    lines.extend(f"- {note}" for note in report.get("notes") or [])
    return "\n".join(lines) + "\n"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--observations")
    parser.add_argument("--now")
    parser.add_argument("--output")
    parser.add_argument("--markdown")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    required = {
        "daily_matches.json": data_dir / "daily_matches.json",
        "prediction_snapshots.json": data_dir / "prediction_snapshots.json",
        "settlements.json": data_dir / "settlements.json",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        print(
            json.dumps(
                {
                    "status": "DATA_UNAVAILABLE",
                    "reason": "REQUIRED_STORE_MISSING",
                    "missing": missing,
                    "data_dir": str(data_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_DATA_UNAVAILABLE

    for output in (args.output, args.markdown):
        if output and _inside(Path(output), data_dir):
            print(
                json.dumps(
                    {
                        "status": "DATA_UNAVAILABLE",
                        "reason": "OUTPUT_INSIDE_DATA_DIR",
                        "data_dir": str(data_dir),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return EXIT_DATA_UNAVAILABLE

    now = None
    if args.now:
        try:
            now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError:
            print(
                json.dumps(
                    {"status": "DATA_UNAVAILABLE", "reason": "INVALID_NOW"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return EXIT_DATA_UNAVAILABLE
        if now.tzinfo is None:
            print(
                json.dumps(
                    {"status": "DATA_UNAVAILABLE", "reason": "NOW_TIMEZONE_REQUIRED"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return EXIT_DATA_UNAVAILABLE

    try:
        daily = _read_json(required["daily_matches.json"])
        snapshots = _read_json(required["prediction_snapshots.json"])
        settlements = _read_json(required["settlements.json"])
        observations = _read_json(Path(args.observations)) if args.observations else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "DATA_UNAVAILABLE",
                    "reason": "STORE_UNREADABLE",
                    "detail": exc.__class__.__name__,
                    "data_dir": str(data_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_DATA_UNAVAILABLE

    matches = daily.get("matches") if isinstance(daily, dict) else None
    if not isinstance(matches, list):
        print(
            json.dumps(
                {"status": "DATA_UNAVAILABLE", "reason": "MALFORMED_CANONICAL_ROOT"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_DATA_UNAVAILABLE

    report = build_basketball_result_delivery(
        matches,
        snapshots,
        settlements,
        observations,
        now=now,
    )
    summary = summarize_result_delivery(report, sample_limit=5)
    payload = dict(report, summary=summary)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run())
