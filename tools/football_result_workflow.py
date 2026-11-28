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
    # 硬链接别名：同 inode 的既有受保护存储/凭据文件不得被覆写
    if path.exists():
        for name in PROTECTED_STORE_NAMES:
            candidate = data_dir / name
            if candidate.exists():
                try:
                    if os.path.samefile(path, candidate):
                        raise SystemExit(
                            f"refusing to overwrite a protected store via alias: {name}"
                        )
                except OSError:
                    continue


def _assert_no_credentials(text: str) -> None:
    """凭据先检查后落盘：命中已知凭据形态即失败关闭，绝不写入。"""
    for pattern in ("DSL_BDL_API_KEY", "Authorization:", "Bearer ", "api_key="):
        if pattern in text:
            raise SystemExit("refusing to persist credential-bearing content")


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
             allow_network: bool, max_requests: int,
             match_ids: list[str] | None = None) -> dict:
    from utils import fetcher_500
    from utils.football_result_history import (
        evidence_endpoint,
        valid_date,
        PARSER_VERSION,
    )

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
    approved_targets = sorted({str(x).strip() for x in (match_ids or []) if str(x).strip()})
    if len(approved_targets) > max_targets_cap(data_dir):
        return {
            "status": "BLOCKED",
            "reason": "TARGET_LIMIT_EXCEEDED",
            "approved_target_count": len(approved_targets),
            "requests_performed": 0,
        }
    unknown = _unknown_target_ids(data_dir, approved_targets)
    if unknown:
        return {
            "status": "BLOCKED",
            "reason": "UNKNOWN_TARGET_MATCH_ID",
            "unknown_target_match_ids": unknown,
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
        raw_meta = fetcher_500.last_wanchang_raw_observation()
        source_diag = (fetcher_500.source_diagnostics().get("wanchang") or {})
        row_dates = sorted({str(r.get("date")) for r in rows if r.get("date")})
        entry = {
            "provider": "500_com_wanchang",
            "endpoint": evidence_endpoint(date),
            "date": date,
            "started_at": started_at,
            "observed_at": observed_at,
            "http_status": raw_meta.get("http_status"),
            "source_status": source_diag.get("status"),
            "error_type": raw_meta.get("error_type") or error_type,
            "row_count": len(rows),
            "actual_request_started_at": raw_meta.get("request_started_at"),
            "actual_response_observed_at": raw_meta.get("response_observed_at"),
            "final_url": raw_meta.get("final_url"),
            "redirect_count": raw_meta.get("redirect_count"),
            "response_sha256": raw_meta.get("response_sha256"),
            "response_byte_count": raw_meta.get("response_byte_count"),
            "parser_version": PARSER_VERSION,
            "parsed_row_count": len(rows),
            "observed_date_window": {"min": row_dates[0] if row_dates else None,
                                     "max": row_dates[-1] if row_dates else None,
                                     "dates": row_dates[:10]},
            "cache_note": "cache hits do not refresh observation timestamps; "
                          "actual_request_* are the real fetch times",
        }
        requests_log.append(entry)
        rows_path = evidence_dir / f"football-finished-{date}-{run_id}.rows.json"
        body = {
            "provider": "500_com_wanchang",
            "endpoint": entry["endpoint"],
            "date": date,
            "source_observed_at": observed_at,
            "actual_request_started_at": raw_meta.get("request_started_at"),
            "actual_response_observed_at": raw_meta.get("response_observed_at"),
            "http_status": raw_meta.get("http_status"),
            "response_sha256": raw_meta.get("response_sha256"),
            "redirect_count": raw_meta.get("redirect_count"),
            "parser_version": PARSER_VERSION,
            "approved_target_match_ids": approved_targets,
            "code_sha": _code_sha(),
            "rows": rows,
        }
        _write_rows(rows_path, body, data_dir)
        entry["evidence_path"] = str(rows_path)
        entry["evidence_sha256"] = _sha(rows_path.read_bytes())
        evidence_files.append(str(rows_path))
        raw_bytes = fetcher_500.wanchang_raw_response_bytes()
        if raw_bytes:
            raw_text = raw_bytes.decode("gb2312", errors="replace")
            _assert_no_credentials(raw_text)
            raw_path = evidence_dir / f"football-finished-{date}-{run_id}.raw.html"
            _assert_safe(raw_path, data_dir)
            raw_path.write_bytes(raw_bytes)
            entry["raw_evidence_path"] = str(raw_path)
            entry["raw_evidence_sha256"] = _sha(raw_bytes)
        # 注意：raw 证据只登记在请求条目上，不进入 evidence_files —— 预览只消费
        # *.rows.json 解析行文件，绝不把 HTML 当 JSON 解析。
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
        "parser_version": PARSER_VERSION,
        "requested_dates": dates,
        "approved_target_match_ids": approved_targets,
        "approved_target_count": len(approved_targets),
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


def max_targets_cap(data_dir: Path) -> int:
    """显式目标上限：与自动选择共用同一常量来源。"""
    from utils.football_result_history import MAX_TARGETS_LIMIT
    return MAX_TARGETS_LIMIT


def _unknown_target_ids(data_dir: Path, target_ids: list[str]) -> list[str]:
    if not target_ids:
        return []
    matches = _load_doc("daily_matches.json", data_dir).get("matches") or []
    known = {str(m.get("id")) for m in matches if isinstance(m, dict)}
    return [tid for tid in target_ids if tid not in known]


def _preview(data_dir: Path, evidence_dir: Path, evidence_path: str | None,
             match_ids: list[str] | None = None) -> dict:
    from utils.football_result_history import build_preview

    manifest = None
    manifest_path = evidence_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                manifest = value
        except (OSError, ValueError):
            manifest = None

    approved_targets: set[str] | None = None
    target_scope = "UNBOUND"
    if match_ids:
        approved_targets = {str(x).strip() for x in match_ids if str(x).strip()}
        target_scope = "BOUND_EXPLICIT"
    elif manifest and manifest.get("approved_target_match_ids"):
        approved_targets = {str(x) for x in manifest["approved_target_match_ids"]}
        target_scope = "BOUND_MANIFEST"

    consumed: list[dict] = []
    rows: list[dict] = []
    observed = None
    if evidence_path:
        rows, observed = _load_rows(Path(evidence_path))
        consumed.append({"path": evidence_path,
                         "sha256": _sha(Path(evidence_path).read_bytes()),
                         "verified_against_manifest": False})
    elif manifest and manifest.get("evidence_files"):
        # 只消费 manifest 登记的证据文件；逐文件复核 SHA-256，不静默吞下无关缓存
        for path_str in manifest["evidence_files"]:
            path = Path(path_str)
            if not path.is_file():
                return {"status": "BLOCKED", "reason": "EVIDENCE_FILE_MISSING",
                        "missing_evidence_path": path_str, "preview": None}
            digest = _sha(path.read_bytes())
            registered = None
            for req in manifest.get("requests") or []:
                if req.get("evidence_path") == path_str:
                    registered = req.get("evidence_sha256")
                    break
            if registered and registered != digest:
                return {"status": "BLOCKED", "reason": "EVIDENCE_DIGEST_MISMATCH",
                        "evidence_path": path_str, "preview": None}
            part, part_observed = _load_rows(path)
            rows.extend(part)
            observed = max([x for x in (observed, part_observed) if x], default=None)
            consumed.append({"path": path_str, "sha256": digest,
                             "verified_against_manifest": bool(registered)})
    else:
        for path in sorted(evidence_dir.glob("*.rows.json")):
            part, part_observed = _load_rows(path)
            rows.extend(part)
            observed = max([x for x in (observed, part_observed) if x], default=None)
            consumed.append({"path": str(path), "sha256": _sha(path.read_bytes()),
                             "verified_against_manifest": False})
    if not rows:
        return {"status": "BLOCKED", "reason": "NO_EVIDENCE_ROWS", "preview": None}
    preview = build_preview(rows, data_dir, source_observed_at=observed,
                            target_match_ids=approved_targets,
                            consumed_evidence=consumed)
    preview["target_scope"] = target_scope
    _write_json(evidence_dir / "preview.json", preview, data_dir)
    _write_json(evidence_dir / "preview-status.json", {
        "status": preview["status"],
        "preview_sha256": preview.get("preview_sha256"),
        "target_count": preview.get("target_count"),
        "target_match_ids": preview.get("target_match_ids"),
        "target_scope": target_scope,
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
        result = _preview(data_dir, evidence_dir, args.evidence, match_ids=args.match_id)
        _emit(result, args.output, data_dir)
        return EXIT_OK if result.get("status") == "PASS" else EXIT_PREVIEW_BLOCKED

    if not dates:
        _emit({"status": "BLOCKED", "reason": "DATE_REQUIRED"}, args.output, data_dir)
        return EXIT_BLOCKED
    acquired = _acquire(data_dir, evidence_dir, dates, allow_network=args.allow_network,
                        max_requests=args.max_requests, match_ids=args.match_id)
    if args.command == "acquire":
        _emit(acquired, args.output, data_dir)
        return EXIT_OK if acquired.get("status") == "OK" else EXIT_BLOCKED

    if acquired.get("status") != "OK":
        _emit({"acquisition": acquired, "preview": {"status": "NOT_EXECUTED",
                                                     "reason": "ACQUISITION_NOT_OK"}},
              args.output, data_dir)
        return EXIT_BLOCKED
    preview = _preview(data_dir, evidence_dir, None, match_ids=args.match_id)
    _emit({"acquisition": acquired, "preview": preview}, args.output, data_dir)
    return EXIT_OK if preview.get("status") == "PASS" else EXIT_PREVIEW_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
