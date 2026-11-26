"""Reusable football historical-result workflow (read-only by default).

Subcommands::

    python -m tools.football_result_workflow select --data-dir data
    python -m tools.football_result_workflow acquire --dates 2026-10-29 --allow-network --max-requests 1
    python -m tools.football_result_workflow preview --evidence reports/football-result-evidence/...

``acquire`` performs one request per requested date through the existing
``utils.fetcher_500.fetch_finished_matches`` adapter. There is no retry, no paid
provider and no production write. Settlement execution is intentionally not
implemented here; a later separately authorized operation must reuse the
existing lifecycle/settlement/evaluation machinery rather than this preview.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_DIR = ROOT / "reports" / "football-result-evidence"
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")

EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_PREVIEW_BLOCKED = 4

PROTECTED = (
    "daily_matches.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
    "team_strength.json",
    "calibrated.json",
    ".refresh_state.json",
)


def _now_iso() -> str:
    return datetime.now(BEIJING).isoformat()


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _code_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                       text=True).strip()
    except Exception:  # noqa: BLE001 - evidence metadata only
        return "UNKNOWN"


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)),
                                   os.path.normcase(os.path.realpath(root))]) == \
            os.path.normcase(os.path.realpath(root))
    except ValueError:
        return False


def _assert_safe(path: Path, data_dir: Path) -> None:
    if path.is_symlink():
        raise SystemExit("refusing to write through a symlink")
    if _inside(path, data_dir):
        raise SystemExit("refusing to write inside DATA_DIR")
    if os.path.normcase(os.path.realpath(path)) == \
            os.path.normcase(os.path.realpath(ROOT / ".DSL_BDL_API_KEY")):
        raise SystemExit("refusing to overwrite the credential file")


def _write_json(path: Path, value, data_dir: Path) -> None:
    _assert_safe(path, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_rows(path: Path, value, data_dir: Path) -> None:
    _assert_safe(path, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_doc(name: str, data_dir: Path) -> dict:
    path = data_dir / name
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig") as fh:
        value = json.load(fh)
    return value if isinstance(value, dict) else {}


def _load_rows(path: Path) -> tuple[list[dict], str | None]:
    with Path(path).open(encoding="utf-8-sig") as fh:
        value = json.load(fh)
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)], None
    if isinstance(value, dict):
        rows = value.get("rows") or []
        return [r for r in rows if isinstance(r, dict)], value.get("source_observed_at")
    return [], None


def _selection(data_dir: Path, max_targets: int, match_ids: list[str]) -> dict:
    from utils.football_result_history import select_pending_targets

    matches = _load_doc("daily_matches.json", data_dir).get("matches") or []
    snapshots = _load_doc("prediction_snapshots.json", data_dir).get("snapshots") or {}
    settlements = _load_doc("settlements.json", data_dir).get("settlements") or {}
    return select_pending_targets(matches, snapshots, settlements,
                                  max_targets=max_targets, match_ids=match_ids or None)


def _acquire(data_dir: Path, evidence_dir: Path, dates: list[str], *,
             allow_network: bool, max_requests: int) -> dict:
    from utils import fetcher_500
    from utils.football_result_history import evidence_endpoint, valid_date

    invalid = [d for d in dates if not valid_date(d)]
    if invalid:
        return {"status": "BLOCKED", "reason": "INVALID_DATE", "invalid_dates": invalid,
                "requests_performed": 0}
    if not allow_network:
        return {
            "status": "BLOCKED",
            "reason": "NETWORK_AUTHORIZATION_REQUIRED",
            "requested_dates": dates,
            "requests_planned": len(dates),
            "requests_performed": 0,
            "expected_cost_usd": 0,
        }
    if max_requests < len(dates):
        return {
            "status": "BLOCKED",
            "reason": "REQUEST_BUDGET_INSUFFICIENT",
            "requested_dates": dates,
            "requests_planned": len(dates),
            "max_requests": max_requests,
            "requests_performed": 0,
        }
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    requests_log, all_rows = [], []
    evidence_files = []
    for date in dates:
        started_at = _now_iso()
        try:
            rows = fetcher_500.fetch_finished_matches(date)
            error_type = None
        except Exception as exc:  # noqa: BLE001 - sanitized type only
            rows = []
            error_type = type(exc).__name__
        observed_at = _now_iso()
        source_diag = (fetcher_500.source_diagnostics().get("wanchang") or {})
        entry = {
            "provider": "500_com_wanchang",
            "endpoint": evidence_endpoint(date),
            "date": date,
            "started_at": started_at,
            "observed_at": observed_at,
            "http_status": None,
            "source_status": source_diag.get("status"),
            "error_type": error_type,
            "row_count": len(rows),
        }
        requests_log.append(entry)
        rows_path = evidence_dir / f"football-finished-{date}-{run_id}.rows.json"
        body = {
            "provider": "500_com_wanchang",
            "endpoint": entry["endpoint"],
            "date": date,
            "source_observed_at": observed_at,
            "code_sha": _code_sha(),
            "rows": rows,
        }
        _write_rows(rows_path, body, data_dir)
        entry["evidence_path"] = str(rows_path)
        entry["evidence_sha256"] = _sha(rows_path.read_bytes())
        evidence_files.append(str(rows_path))
        all_rows.extend(rows)
        if error_type:
            break

    failed = any(r.get("error_type") or r.get("source_status") == "unavailable"
                 for r in requests_log)
    empty = any(r.get("row_count") == 0 for r in requests_log)
    result = {
        "status": "OK" if all_rows else "BLOCKED",
        "reason": None if all_rows else (
            "SOURCE_REQUEST_FAILED" if failed else "SOURCE_RETURNED_EMPTY"),
        "run_id": run_id,
        "generated_at": _now_iso(),
        "code_sha": _code_sha(),
        "provider": "500_com_wanchang",
        "requested_dates": dates,
        "requests_performed": len(requests_log),
        "max_requests": max_requests,
        "expected_cost_usd": 0,
        "requests": requests_log,
        "evidence_files": evidence_files,
        "row_count": len(all_rows),
        "partial": bool(all_rows) and (failed or empty),
        "source_observed_at": max((r["observed_at"] for r in requests_log), default=None),
    }
    _write_json(evidence_dir / "manifest.json", result, data_dir)
    _write_json(evidence_dir / "runs" / f"{run_id}.json", result, data_dir)
    return result


def _preview(data_dir: Path, evidence_dir: Path, evidence_path: str | None) -> dict:
    from utils.football_result_history import build_preview

    if evidence_path:
        rows, observed = _load_rows(Path(evidence_path))
    else:
        rows, observed = [], None
        for path in sorted(evidence_dir.glob("*.rows.json")):
            part, part_observed = _load_rows(path)
            rows.extend(part)
            observed = max([x for x in (observed, part_observed) if x], default=None)
    if not rows:
        return {"status": "BLOCKED", "reason": "NO_EVIDENCE_ROWS", "preview": None}
    preview = build_preview(rows, data_dir, source_observed_at=observed)
    _write_json(evidence_dir / "preview.json", preview, data_dir)
    _write_json(evidence_dir / "preview-status.json", {
        "status": preview["status"],
        "preview_sha256": preview.get("preview_sha256"),
        "target_count": preview.get("target_count"),
        "target_match_ids": preview.get("target_match_ids"),
        "write_scope": preview.get("preview", {}).get("write_scope"),
        "network_requests": 0,
        "canonical_reconciliation": False,
    }, data_dir)
    return preview


def _emit(value: dict, output: str | None, data_dir: Path) -> None:
    if output:
        _write_json(Path(output), value, data_dir)
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("select", "acquire", "preview", "run"):
        child = sub.add_parser(name)
        child.add_argument("--data-dir")
        child.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR))
        child.add_argument("--evidence")
        child.add_argument("--date", action="append", default=[])
        child.add_argument("--dates", action="append", default=[])
        child.add_argument("--match-id", action="append", default=[])
        child.add_argument("--max-targets", type=int, default=20)
        child.add_argument("--allow-network", action="store_true")
        child.add_argument("--max-requests", type=int, default=0)
        child.add_argument("--output")
    args = parser.parse_args(argv)

    if args.max_targets < 1 or args.max_targets > 100:
        raise SystemExit("--max-targets must be 1..100")
    data_dir = Path(args.data_dir).resolve() if args.data_dir else Path(config.DATA_DIR).resolve()
    evidence_dir = Path(args.evidence_dir).resolve()
    _assert_safe(evidence_dir, data_dir)
    if args.output:
        _assert_safe(Path(args.output).resolve(), data_dir)

    dates = list(dict.fromkeys(args.date + [d for chunk in args.dates for d in chunk.split(",") if d]))

    if args.command == "select":
        result = _selection(data_dir, args.max_targets, args.match_id)
        _emit(result, args.output, data_dir)
        return EXIT_OK

    if args.command == "preview":
        result = _preview(data_dir, evidence_dir, args.evidence)
        _emit(result, args.output, data_dir)
        return EXIT_OK if result.get("status") == "PASS" else EXIT_PREVIEW_BLOCKED

    if not dates:
        _emit({"status": "BLOCKED", "reason": "DATE_REQUIRED"}, args.output, data_dir)
        return EXIT_BLOCKED
    acquired = _acquire(data_dir, evidence_dir, dates, allow_network=args.allow_network,
                        max_requests=args.max_requests)
    if args.command == "acquire":
        _emit(acquired, args.output, data_dir)
        return EXIT_OK if acquired.get("status") == "OK" else EXIT_BLOCKED

    if acquired.get("status") != "OK":
        _emit({"acquisition": acquired, "preview": {"status": "NOT_EXECUTED",
                                                     "reason": "ACQUISITION_NOT_OK"}},
              args.output, data_dir)
        return EXIT_BLOCKED
    preview = _preview(data_dir, evidence_dir, None)
    _emit({"acquisition": acquired, "preview": preview}, args.output, data_dir)
    return EXIT_OK if preview.get("status") == "PASS" else EXIT_PREVIEW_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
