"""Reusable NBA result-evidence workflow: select -> plan -> acquire -> strict preview.

Default invocation is offline and read-only. Network acquisition requires explicit
``--allow-network`` plus a bounded budget; credentials are never written to evidence.

Examples::

    python -m tools.nba_result_workflow select --data-dir data
    python -m tools.nba_result_workflow plan --data-dir data --allow-network --max-requests 7
    python -m tools.nba_result_workflow acquire --data-dir data --allow-network --max-requests 7
    python -m tools.nba_result_workflow preview --data-dir data
    python -m tools.nba_result_workflow run --data-dir data --allow-network --max-requests 7
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import config
from utils import nba_result_evidence as evidence
from utils.nba_result_evidence import (
    BDL_PACING_SECONDS,
    DEFAULT_MAX_TARGETS,
    MAX_TARGETS_LIMIT,
    OFFICIAL_PAGE_PACING_SECONDS,
    PROVIDER_BDL,
    PROVIDER_OFFICIAL,
    STATUS_ELIGIBLE,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_DIR = ROOT / "reports" / "result-evidence"
BJ = timezone(timedelta(hours=8))

EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_REJECTED = 3
EXIT_PREVIEW_BLOCKED = 4


def _now_iso() -> str:
    return datetime.now(BJ).isoformat()


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)),
                                   os.path.normcase(os.path.realpath(root))]) == \
            os.path.normcase(os.path.realpath(root))
    except ValueError:
        return False


def _code_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:  # noqa: BLE001 - evidence metadata must not fail the run
        return "UNKNOWN"


def _load_credential() -> str | None:
    key = os.environ.get("DSL_BDL_API_KEY")
    if not key:
        path = ROOT / ".DSL_BDL_API_KEY"
        if path.is_file():
            key = path.read_text(encoding="utf-8").strip()
    return key or None


def _session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
    return session


class _BudgetExceeded(RuntimeError):
    pass


class _Pacer:
    def __init__(self, seconds: float):
        self.seconds = max(0.0, float(seconds))
        self.last = None

    def wait(self) -> None:
        if self.last is not None:
            delay = self.seconds - (time.monotonic() - self.last)
            if delay > 0:
                time.sleep(delay)
        self.last = time.monotonic()


def _get(session, url: str, headers: dict, *, timeout: float, counter: dict, pacer: _Pacer,
         provider: str, target_ids: list[str]) -> tuple[bytes | None, dict]:
    if counter["requests"] >= counter["max_requests"]:
        raise _BudgetExceeded("REQUEST_BUDGET_EXCEEDED")
    pacer.wait()
    counter["requests"] += 1
    entry = {
        "provider": provider,
        "endpoint": url,
        "started_at": _now_iso(),
        "target_match_ids": target_ids,
        "code_sha": _code_sha(),
    }
    try:
        response = session.get(url, headers=headers, timeout=timeout, allow_redirects=False)
        content = response.content
        entry.update({
            "observed_at": _now_iso(),
            "http_status": response.status_code,
            "response_size": len(content),
            "response_sha256": _sha(content),
        })
        return (content if response.status_code == 200 else None), entry
    except Exception as exc:  # noqa: BLE001 - record type only, no credential-bearing message
        entry.update({"observed_at": _now_iso(), "error_type": type(exc).__name__})
        return None, entry


def _manifest_path(evidence_dir: Path) -> Path:
    return evidence_dir / "manifest.json"


def load_manifest(evidence_dir: Path) -> dict:
    path = _manifest_path(evidence_dir)
    if not path.is_file():
        return {}
    try:
        value = _read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _valid_cache(entry: dict | None, target: dict) -> bool:
    if not isinstance(entry, dict) or entry.get("status") != "VALIDATED":
        return False
    if entry.get("snapshot_digest") != target.get("snapshot_digest"):
        return False
    if str(entry.get("bdl_game_id")) != str(target.get("bdl_game_id")):
        return False
    for key in ("official_path", "bdl_path", "observed_at", "source_reference"):
        if not entry.get(key):
            return False
    for path_key, hash_key in (("official_path", "official_sha256"), ("bdl_path", "bdl_sha256")):
        path = Path(entry[path_key])
        if not path.is_file() or _sha(path.read_bytes()) != entry.get(hash_key):
            return False
    return True


def _load_targets(target_ids, max_targets, now=None) -> dict:
    return evidence.select_pending_targets(target_ids or None, max_targets=max_targets, now=now)


def _run_acquire(selection: dict, *, evidence_dir: Path, allow_network: bool, max_requests: int,
                 allowed_providers: list[str], max_cost: float, official_game_ids: dict[str, str],
                 pacing_seconds: float, session=None, credential: str | None = None,
                 timeout: float = 25.0) -> dict:
    targets = [t for t in selection.get("targets") or [] if t.get("selection_status") == STATUS_ELIGIBLE]
    created = _now_iso()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = load_manifest(evidence_dir)
    cached = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}

    accepted: list[dict] = []
    rejected: list[dict] = [dict(r) for r in selection.get("rejected") or []]
    requests_log: list[dict] = []
    cache_hits = 0
    needs_network: list[dict] = []

    for target in targets:
        entry = cached.get(target["match_id"])
        if _valid_cache(entry, target):
            cache_hits += 1
            accepted.append(dict(entry))
        else:
            needs_network.append(target)

    if not targets:
        return {"status": "BLOCKED", "reason": "NO_ELIGIBLE_TARGETS", "requests_performed": 0,
                "cache_hits": 0, "accepted": [], "rejected": rejected, "evidence_dir": str(evidence_dir)}

    if needs_network and not allow_network:
        return {
            "status": "BLOCKED",
            "reason": "NETWORK_AUTHORIZATION_REQUIRED",
            "targets_requiring_network": [t["match_id"] for t in needs_network],
            "requests_planned": len({t.get("official_discovery_date") for t in needs_network}) + 2 * len(needs_network),
            "requests_performed": 0,
            "cache_hits": cache_hits,
            "accepted": accepted,
            "rejected": rejected,
            "evidence_dir": str(evidence_dir),
        }

    plan = evidence.build_request_plan(
        needs_network, allow_network=allow_network, max_requests=max_requests,
        allowed_providers=allowed_providers, max_cost=max_cost, official_game_ids=official_game_ids,
    )
    if needs_network and plan["status"] != "PLAN_READY":
        return {"status": "BLOCKED", "reason": plan["status"], "plan": plan,
                "requests_performed": 0, "cache_hits": cache_hits,
                "accepted": accepted, "rejected": rejected, "evidence_dir": str(evidence_dir)}

    if needs_network:
        credential = credential if credential is not None else _load_credential()
        if not credential:
            return {"status": "BLOCKED", "reason": "BLOCKED_MISSING_CREDENTIALS",
                    "requests_performed": 0, "cache_hits": cache_hits,
                    "accepted": accepted, "rejected": rejected, "evidence_dir": str(evidence_dir)}

    session = session or _session()
    counter = {"requests": 0, "max_requests": max_requests}
    official_pacer = _Pacer(max(OFFICIAL_PAGE_PACING_SECONDS, 0.0))
    bdl_pacer = _Pacer(max(BDL_PACING_SECONDS, pacing_seconds))
    nba_dir = evidence_dir / "nba-official"
    bdl_dir = evidence_dir / "balldontlie"
    nba_dir.mkdir(parents=True, exist_ok=True)
    bdl_dir.mkdir(parents=True, exist_ok=True)

    try:
        official_ids = dict(official_game_ids)
        missing_official = [t for t in needs_network if t["match_id"] not in official_ids]
        by_date: dict[str, list[dict]] = {}
        for target in missing_official:
            by_date.setdefault(str(target.get("official_discovery_date")), []).append(target)
        for date, date_targets in sorted(by_date.items()):
            url = f"https://www.nba.com/games?date={date}"
            raw_path = nba_dir / f"nba-discovery-{date}.response"
            if raw_path.is_file():
                content = raw_path.read_bytes()
                requests_log.append({"provider": PROVIDER_OFFICIAL, "endpoint": url,
                                     "cache_hit": True, "response_sha256": _sha(content),
                                     "response_size": len(content), "target_match_ids": [t["match_id"] for t in date_targets]})
            else:
                content, entry = _get(session, url, {"User-Agent": "Mozilla/5.0"}, timeout=timeout,
                                      counter=counter, pacer=official_pacer, provider=PROVIDER_OFFICIAL,
                                      target_ids=[t["match_id"] for t in date_targets])
                requests_log.append(entry)
                if content is None:
                    return {"status": "BLOCKED", "reason": "OFFICIAL_DISCOVERY_FETCH_FAILED",
                            "requests_performed": counter["requests"], "cache_hits": cache_hits,
                            "accepted": accepted, "rejected": rejected + [{"selection_status": "OFFICIAL_DISCOVERY_FETCH_FAILED",
                                                                            "match_id": t["match_id"]} for t in date_targets],
                            "evidence_dir": str(evidence_dir)}
                raw_path.write_bytes(content)
            parsed = evidence.parse_discovery_page(content)
            mapping, unresolved = evidence.resolve_official_game_ids(parsed, date_targets)
            official_ids.update(mapping)
            rejected.extend(unresolved)

        for target in needs_network:
            mid = target["match_id"]
            gid = official_ids.get(mid)
            if not gid:
                if not any(r.get("match_id") == mid for r in rejected):
                    rejected.append({"match_id": mid, "selection_status": "OFFICIAL_GAME_ID_UNRESOLVED",
                                     "reason": "OFFICIAL_GAME_ID_UNRESOLVED"})
                continue
            official_path = nba_dir / f"nba-game-{gid}.response"
            if official_path.is_file():
                official_content = official_path.read_bytes()
                official_entry = {"provider": PROVIDER_OFFICIAL, "cache_hit": True,
                                  "response_sha256": _sha(official_content), "response_size": len(official_content),
                                  "target_match_ids": [mid]}
            else:
                url = evidence.official_page_url(target, gid)
                official_content, official_entry = _get(
                    session, url, {"User-Agent": "Mozilla/5.0"}, timeout=timeout,
                    counter=counter, pacer=official_pacer, provider=PROVIDER_OFFICIAL, target_ids=[mid])
                requests_log.append(official_entry)
                if official_content is None:
                    rejected.append({"match_id": mid, "selection_status": "OFFICIAL_PAGE_FETCH_FAILED",
                                     "reason": "OFFICIAL_PAGE_FETCH_FAILED"})
                    continue
                official_path.write_bytes(official_content)
            if credential and credential.encode() in official_content:
                return {"status": "BLOCKED", "reason": "CREDENTIAL_LEAK_DETECTED",
                        "requests_performed": counter["requests"], "cache_hits": cache_hits,
                        "accepted": accepted, "rejected": rejected, "evidence_dir": str(evidence_dir)}

            bdl_path = bdl_dir / f"bdl-{target['bdl_game_id']}.response"
            if bdl_path.is_file():
                bdl_content = bdl_path.read_bytes()
                bdl_entry = {"provider": PROVIDER_BDL, "cache_hit": True,
                             "response_sha256": _sha(bdl_content), "response_size": len(bdl_content),
                             "target_match_ids": [mid]}
            else:
                url = f"https://api.balldontlie.io/v1/games/{target['bdl_game_id']}"
                bdl_content, bdl_entry = _get(
                    session, url, {"Authorization": credential or ""}, timeout=timeout,
                    counter=counter, pacer=bdl_pacer, provider=PROVIDER_BDL, target_ids=[mid])
                requests_log.append(bdl_entry)
                if bdl_content is None:
                    rejected.append({"match_id": mid, "selection_status": "BDL_FETCH_FAILED",
                                     "reason": "BDL_FETCH_FAILED"})
                    continue
                bdl_path.write_bytes(bdl_content)
            if credential and credential.encode() in bdl_content:
                return {"status": "BLOCKED", "reason": "CREDENTIAL_LEAK_DETECTED",
                        "requests_performed": counter["requests"], "cache_hits": cache_hits,
                        "accepted": accepted, "rejected": rejected, "evidence_dir": str(evidence_dir)}

            observed_at = bdl_entry.get("observed_at") or official_entry.get("observed_at") or _now_iso()
            source_reference = evidence.official_page_url(target, gid)
            validation = evidence.validate_target_evidence(
                target, official_path, bdl_path, observed_at=observed_at, source_reference=source_reference,
            )
            entry = {
                "match_id": mid,
                "snapshot_id": target["snapshot_id"],
                "snapshot_digest": target["snapshot_digest"],
                "bdl_game_id": target["bdl_game_id"],
                "official_game_id": gid,
                "official_path": str(official_path),
                "bdl_path": str(bdl_path),
                "official_sha256": _sha(official_path.read_bytes()),
                "bdl_sha256": _sha(bdl_path.read_bytes()),
                "observed_at": observed_at,
                "source_reference": source_reference,
                "status": "VALIDATED" if validation.get("qualified") else "REJECTED",
                "validation": validation,
                "provider": [PROVIDER_OFFICIAL, PROVIDER_BDL],
            }
            if validation.get("qualified"):
                accepted.append(entry)
            else:
                rejected.append({"match_id": mid, "selection_status": "EVIDENCE_REJECTED",
                                 "reason": validation.get("reason")})
    except _BudgetExceeded:
        return {"status": "BLOCKED", "reason": "REQUEST_BUDGET_EXCEEDED",
                "requests_performed": counter["requests"], "cache_hits": cache_hits,
                "accepted": accepted, "rejected": rejected, "evidence_dir": str(evidence_dir)}

    result = {
        "status": "OK" if accepted else "BLOCKED",
        "reason": None if accepted else "NO_VALIDATED_TARGETS",
        "run_id": run_id,
        "generated_at": created,
        "code_sha": _code_sha(),
        "evidence_dir": str(evidence_dir),
        "requests_performed": counter["requests"],
        "cache_hits": cache_hits,
        "requests": requests_log,
        "targets": {entry["match_id"]: entry for entry in accepted},
        "accepted": [entry["match_id"] for entry in accepted],
        "rejected": rejected,
        "authorization": {
            "allow_network": bool(allow_network),
            "max_requests": max_requests,
            "allowed_providers": allowed_providers,
            "max_cost_usd": max_cost,
            "bdl_pacing_seconds": max(BDL_PACING_SECONDS, pacing_seconds),
        },
    }
    _write_json(_manifest_path(evidence_dir), result)
    _write_json(evidence_dir / "runs" / f"{run_id}.json", result)
    preview_targets = [evidence.target_for_preview(entry) for entry in accepted]
    _write_json(evidence_dir / "preview-targets.json", preview_targets)
    return result


def _run_preview(evidence_dir: Path, data_dir: Path | None = None) -> dict:
    from tools.nba_confirmed_result import _official_game, preview_batch

    manifest = load_manifest(evidence_dir)
    entries = []
    for entry in (manifest.get("targets") or {}).values():
        if not _valid_cache(entry, entry):
            continue
        official = _official_game(entry["official_path"])
        bdl_payload = _read_json(Path(entry["bdl_path"]))
        bdl = bdl_payload.get("data") if isinstance(bdl_payload, dict) else None
        if not isinstance(bdl, dict):
            continue
        entries.append({
            "match_id": entry["match_id"],
            "snapshot_id": entry["snapshot_id"],
            "official": official,
            "bdl": bdl,
            "observed_at": entry["observed_at"],
            "source_reference": entry["source_reference"],
        })
    if not entries:
        return {"status": "BLOCKED", "reason": "NO_VALIDATED_EVIDENCE", "preview": None}
    try:
        plan = preview_batch(entries)
    except ValueError as exc:
        return {"status": "BLOCKED", "reason": str(exc), "preview": None}
    _write_json(evidence_dir / "preview.json", plan)
    return {
        "status": "PASS",
        "preview_sha256": plan.get("preview_sha256"),
        "generation": plan.get("generation"),
        "target_count": len(plan.get("targets") or []),
        "targets": [{"match_id": t["match_id"], "snapshot_id": t["snapshot_id"],
                     "settlement_id": t["settlement_id"], "evaluation_id": t["evaluation_id"]}
                    for t in plan.get("targets") or []],
        "write_scope": plan.get("write_scope"),
        "preview": plan,
    }


def _parse_official_ids(values: list[str] | None) -> dict[str, str]:
    out = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit(f"--official-game-id expects MATCH_ID=GAME_ID, got {value!r}")
        mid, gid = value.split("=", 1)
        if not (len(gid) == 10 and gid.isdigit()):
            raise SystemExit(f"official game id must be 10 digits: {value!r}")
        out[mid.strip()] = gid.strip()
    return out


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir")
    parser.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR))
    parser.add_argument("--target-match-id", action="append", default=[])
    parser.add_argument("--max-targets", type=int, default=DEFAULT_MAX_TARGETS)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--allowed-provider", action="append", default=[])
    parser.add_argument("--max-cost", type=float, default=0)
    parser.add_argument("--official-game-id", action="append", default=[])
    parser.add_argument("--pacing-seconds", type=float, default=BDL_PACING_SECONDS)
    parser.add_argument("--output")


def _data_dir(value: str | None) -> Path:
    path = Path(value).resolve() if value else Path(config.DATA_DIR).resolve()
    return path


def _apply_data_dir(path: Path) -> None:
    config.DATA_DIR = str(path)


def _emit(result: dict, output: str | None) -> None:
    if output:
        _write_json(Path(output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("select", "plan", "acquire", "preview", "run"):
        child = sub.add_parser(name)
        _common(child)
    args = parser.parse_args(argv)

    if args.max_targets < 1 or args.max_targets > MAX_TARGETS_LIMIT:
        raise SystemExit(f"--max-targets must be 1..{MAX_TARGETS_LIMIT}")
    if args.pacing_seconds < BDL_PACING_SECONDS:
        raise SystemExit(f"--pacing-seconds cannot be below {BDL_PACING_SECONDS}")

    data_dir = _data_dir(args.data_dir)
    evidence_dir = Path(args.evidence_dir).resolve()
    if _inside(evidence_dir, data_dir):
        raise SystemExit("evidence directory must not be inside DATA_DIR")
    _apply_data_dir(data_dir)
    official_ids = _parse_official_ids(args.official_game_id)
    allowed = args.allowed_provider or [PROVIDER_OFFICIAL, PROVIDER_BDL]

    selection = _load_targets(args.target_match_id, args.max_targets)
    if args.command == "select":
        _emit(selection, args.output)
        return EXIT_OK

    if args.command == "plan":
        plan = evidence.build_request_plan(
            selection["targets"], allow_network=args.allow_network, max_requests=args.max_requests,
            allowed_providers=allowed, max_cost=args.max_cost, official_game_ids=official_ids,
        )
        _emit({"selection": selection, "plan": plan}, args.output)
        return EXIT_OK if plan.get("status") in ("PLAN_READY", "NETWORK_AUTHORIZATION_REQUIRED") else EXIT_BLOCKED

    if args.command == "preview":
        result = _run_preview(evidence_dir, data_dir)
        _emit(result, args.output)
        return EXIT_OK if result["status"] == "PASS" else EXIT_PREVIEW_BLOCKED

    acquire_result = _run_acquire(
        selection, evidence_dir=evidence_dir, allow_network=args.allow_network,
        max_requests=args.max_requests, allowed_providers=allowed, max_cost=args.max_cost,
        official_game_ids=official_ids, pacing_seconds=args.pacing_seconds,
    )
    if args.command == "acquire":
        _emit(acquire_result, args.output)
        return EXIT_OK if acquire_result["status"] == "OK" else EXIT_BLOCKED

    if acquire_result["status"] != "OK":
        _emit({"acquisition": acquire_result, "preview": {"status": "NOT_EXECUTED",
                                                          "reason": "ACQUISITION_NOT_OK"}}, args.output)
        return EXIT_BLOCKED
    preview_result = _run_preview(evidence_dir, data_dir)
    _emit({"acquisition": acquire_result, "preview": preview_result}, args.output)
    return EXIT_OK if preview_result["status"] == "PASS" else EXIT_PREVIEW_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
