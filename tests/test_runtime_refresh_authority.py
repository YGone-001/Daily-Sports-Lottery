"""
本地刷新授权：启动器默认值与后端权威运行状态（AGENTS.md §4.35 / Workstream D）。

覆盖三件事：

1. 启动器**默认只读**：仅显式 ``--refresh`` 才同时开启主写开关与本地界面开关，
   首次抓取同样受该显式参数门控；启动器从不在未要求时开启后台刷新。
2. 后端 ``_runtime_capability()`` 是权威：五态（READ_ONLY_APPLICATION /
   LOCAL_REFRESH_AVAILABLE / REFRESH_IN_PROGRESS / REFRESH_COMPLETED /
   REFRESH_FAILED）由开关与刷新标记推导，浏览器请求头不能改变它。
3. ``/api/refresh`` 默认拒绝：主写开关关闭时回环同源头也不能触发刷新；
   本地界面通道与令牌通道分别校验；锁占用时返回建议性 owner 而不宣称触发来源。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import app as app_module
import config
from utils import refresh_state
from utils.concurrency import RefreshBusyError
from utils.daily_loader import override_beijing_now

from test_basketball_operational_acceptance import basketball_match, write_stores


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT_PATH = os.path.join(ROOT, "start.bat")
SH_PATH = os.path.join(ROOT, "start.sh")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _marker(data_dir, status: str) -> int:
    owner = refresh_state.owner_record("pytest")
    generation = refresh_state.begin(str(data_dir), owner=owner)
    if status != refresh_state.STATUS_IN_PROGRESS:
        refresh_state.complete(
            str(data_dir),
            owner=owner,
            generation=generation,
            outcome=status,
            error="synthetic failure" if status == refresh_state.STATUS_FAILED else None,
        )
    return generation


def _configure(monkeypatch, tmp_path, *, writes: bool, local_ui: bool, token=None):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setitem(app_module.app.config, "RUNTIME_WRITES_ENABLED", writes)
    monkeypatch.setitem(app_module.app.config, "LOCAL_UI_REFRESH_ENABLED", local_ui)
    monkeypatch.setitem(app_module.app.config, "RUNTIME_WRITE_TOKEN", token)


# ---------------------------------------------------------------------------
# 启动器：默认只读 + 显式 --refresh
# ---------------------------------------------------------------------------


def test_windows_launcher_defaults_to_read_only_and_requires_explicit_refresh():
    text = _read(BAT_PATH)
    assert 'if /I "%~1"=="--refresh" set "REFRESH_MODE=1"' in text
    pattern = (
        r'if defined REFRESH_MODE \(\s*'
        r'set "DSL_RUNTIME_WRITES_ENABLED=true"\s*'
        r'set "DSL_LOCAL_UI_REFRESH_ENABLED=true"\s*'
        r'\) else \(\s*'
        r'set "DSL_RUNTIME_WRITES_ENABLED=false"\s*'
        r'set "DSL_LOCAL_UI_REFRESH_ENABLED=false"\s*'
        r'\)'
    )
    assert re.search(pattern, text), "start.bat must gate both switches behind REFRESH_MODE"
    assert "DSL_BACKGROUND_REFRESH_ENABLED=true" not in text
    first_run = re.search(
        r'if not exist "data\\daily_matches\.json" \(\s*'
        r'if defined REFRESH_MODE \(.*?utils\.scraper.*?\) else \(',
        text,
        re.S,
    )
    assert first_run, "first-run scrape must be gated by REFRESH_MODE"
    assert "start.bat --refresh" in text


def test_posix_launcher_defaults_to_read_only_and_requires_explicit_refresh():
    text = _read(SH_PATH)
    assert "--refresh) REFRESH_MODE=1" in text
    pattern = (
        r'if \[ -n "\$REFRESH_MODE" \]; then\s*'
        r'export DSL_RUNTIME_WRITES_ENABLED=true\s*'
        r'export DSL_LOCAL_UI_REFRESH_ENABLED=true\s*'
        r'else\s*'
        r'export DSL_RUNTIME_WRITES_ENABLED=false\s*'
        r'export DSL_LOCAL_UI_REFRESH_ENABLED=false\s*'
        r'fi'
    )
    assert re.search(pattern, text), "start.sh must gate both switches behind REFRESH_MODE"
    assert "DSL_BACKGROUND_REFRESH_ENABLED=true" not in text
    first_run = re.search(
        r'if \[ ! -f "data/daily_matches\.json" \]; then\s*'
        r'if \[ -n "\$REFRESH_MODE" \]; then.*?utils\.scraper.*?else',
        text,
        re.S,
    )
    assert first_run, "first-run scrape must be gated by REFRESH_MODE"
    assert "./start.sh --refresh" in text


def _stub_launcher_sandbox(tmp_path):
    sandbox = tmp_path / "sandbox"
    (sandbox / "data").mkdir(parents=True)
    (sandbox / "data" / "daily_matches.json").write_text(
        '{"matches": [], "meta": {}}', encoding="utf-8"
    )
    (sandbox / "frontend" / "node_modules").mkdir(parents=True)
    (sandbox / "start.sh").write_text(_read(SH_PATH), encoding="utf-8")

    python_stub = sandbox / ".venv" / "bin" / "python"
    python_stub.parent.mkdir(parents=True)
    python_stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'if [ "$1" = "-m" ]; then echo "SCRAPE $2" >> "$STUB_DIR/calls.log"; exit 0; fi\n'
        'if [ "$1" = "app.py" ]; then\n'
        "  {\n"
        '    echo "RUNTIME_WRITES=$DSL_RUNTIME_WRITES_ENABLED"\n'
        '    echo "LOCAL_UI=$DSL_LOCAL_UI_REFRESH_ENABLED"\n'
        '    echo "BACKGROUND=${DSL_BACKGROUND_REFRESH_ENABLED:-unset}"\n'
        '  } > "$STUB_DIR/runtime.env"\n'
        '  echo "APP" >> "$STUB_DIR/calls.log"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(python_stub, 0o755)

    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    npm_stub = stub_bin / "npm"
    npm_stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "NPM $*" >> "$STUB_DIR/calls.log"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(npm_stub, 0o755)
    os.chmod(sandbox / "start.sh", 0o755)
    return sandbox, stub_bin


def _run_launcher(sandbox, stub_bin, tmp_path, *args):
    for name in ("calls.log", "runtime.env"):
        path = tmp_path / name
        if path.exists():
            path.unlink()
    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    env["STUB_DIR"] = str(tmp_path)
    env.pop("DSL_RUNTIME_WRITES_ENABLED", None)
    env.pop("DSL_LOCAL_UI_REFRESH_ENABLED", None)
    env.pop("DSL_BACKGROUND_REFRESH_ENABLED", None)
    return subprocess.run(
        ["bash", "start.sh", *args],
        cwd=str(sandbox),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _read_calls(tmp_path):
    path = tmp_path / "calls.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _read_runtime(tmp_path):
    text = (tmp_path / "runtime.env").read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


@pytest.mark.skipif(sys.platform == "win32", reason="start.sh behavior is exercised under bash on POSIX")
def test_posix_launcher_runs_without_enabling_production_writes(tmp_path):
    sandbox, stub_bin = _stub_launcher_sandbox(tmp_path)

    plain = _run_launcher(sandbox, stub_bin, tmp_path)
    assert plain.returncode == 0, plain.stderr + plain.stdout
    assert _read_runtime(tmp_path) == {
        "RUNTIME_WRITES": "false",
        "LOCAL_UI": "false",
        "BACKGROUND": "unset",
    }
    assert "只读" in plain.stdout
    assert "SCRAPE" not in _read_calls(tmp_path), "read-only start must not scrape"

    refreshed = _run_launcher(sandbox, stub_bin, tmp_path, "--refresh")
    assert refreshed.returncode == 0, refreshed.stderr + refreshed.stdout
    assert _read_runtime(tmp_path) == {
        "RUNTIME_WRITES": "true",
        "LOCAL_UI": "true",
        "BACKGROUND": "unset",
    }
    assert "本地界面同步可用" in refreshed.stdout
    assert "NPM run build" in _read_calls(tmp_path)

    unknown = _run_launcher(sandbox, stub_bin, tmp_path, "--force")
    assert unknown.returncode == 0, unknown.stderr + unknown.stdout
    assert "[警告] 未知参数" in unknown.stdout
    assert _read_runtime(tmp_path)["RUNTIME_WRITES"] == "false"


@pytest.mark.skipif(sys.platform == "win32", reason="start.sh behavior is exercised under bash on POSIX")
def test_posix_launcher_first_run_requires_explicit_refresh(tmp_path):
    sandbox, stub_bin = _stub_launcher_sandbox(tmp_path)
    (sandbox / "data" / "daily_matches.json").unlink()

    blocked = _run_launcher(sandbox, stub_bin, tmp_path)
    assert blocked.returncode == 1
    assert "start.sh --refresh" in blocked.stdout
    assert "SCRAPE" not in _read_calls(tmp_path)
    assert "APP" not in _read_calls(tmp_path)

    allowed = _run_launcher(sandbox, stub_bin, tmp_path, "--refresh")
    assert allowed.returncode == 0, allowed.stderr + allowed.stdout
    assert "SCRAPE utils.scraper" in _read_calls(tmp_path)
    assert "APP" in _read_calls(tmp_path)


# ---------------------------------------------------------------------------
# 后端：权威能力 / 五态 / 只读端点
# ---------------------------------------------------------------------------


def test_runtime_capability_defaults_to_read_only(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=False, local_ui=False)
    runtime = app_module._runtime_capability()
    assert runtime["mode"] == "READ_ONLY_APPLICATION"
    assert runtime["operating_state"] == "READ_ONLY_APPLICATION"
    assert runtime["runtime_writes_enabled"] is False
    assert runtime["background_refresh_enabled"] is False
    assert runtime["refresh_available_via_ui"] is False
    assert runtime["last_refresh"]["status"] is None
    assert runtime["last_refresh"]["marker_error"] == "NO_REFRESH_STATE_MARKER"
    assert runtime["last_refresh"]["advisory"] is True


def test_runtime_capability_reports_token_mode_without_local_ui(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=True, local_ui=False, token="secret-token")
    runtime = app_module._runtime_capability()
    assert runtime["mode"] == "TOKEN_REQUIRED"
    assert runtime["operating_state"] == "TOKEN_REQUIRED"
    assert runtime["refresh_available_via_ui"] is False


def test_runtime_capability_maps_refresh_marker_states(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=True, local_ui=True)

    generation = _marker(tmp_path, refresh_state.STATUS_IN_PROGRESS)
    in_progress = app_module._runtime_capability()
    assert in_progress["mode"] == "LOCAL_REFRESH_AVAILABLE"
    assert in_progress["operating_state"] == "REFRESH_IN_PROGRESS"
    assert in_progress["last_refresh"]["status"] == refresh_state.STATUS_IN_PROGRESS
    assert in_progress["last_refresh"]["generation"] == generation

    refresh_state.complete(
        str(tmp_path),
        owner=refresh_state.owner_record("pytest"),
        generation=generation,
        outcome=refresh_state.STATUS_COMPLETED,
    )
    completed = app_module._runtime_capability()
    assert completed["operating_state"] == "REFRESH_COMPLETED"
    assert completed["last_refresh"]["status"] == refresh_state.STATUS_COMPLETED
    assert completed["last_refresh"]["generation"] == generation
    assert completed["last_refresh"]["finished_at"]

    refresh_state.complete(
        str(tmp_path),
        owner=refresh_state.owner_record("pytest"),
        generation=generation,
        outcome=refresh_state.STATUS_FAILED,
        error="source unavailable",
    )
    failed = app_module._runtime_capability()
    assert failed["operating_state"] == "REFRESH_FAILED"
    assert failed["last_refresh"]["error"] == "source unavailable"


def test_runtime_capability_reads_marker_without_modifying_it(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=False, local_ui=False)
    _marker(tmp_path, refresh_state.STATUS_IN_PROGRESS)
    marker_path = tmp_path / refresh_state.STATE_FILENAME
    before = marker_path.read_bytes()
    runtime = app_module._runtime_capability()
    assert runtime["operating_state"] == "REFRESH_IN_PROGRESS"
    assert marker_path.read_bytes() == before


def test_runtime_capability_never_exposes_the_refresh_token(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=True, local_ui=False, token="super-secret")
    runtime = app_module._runtime_capability()
    payload = str(runtime)
    assert "super-secret" not in payload
    assert "RUNTIME_WRITE_TOKEN" not in payload
    assert set(runtime) == {
        "runtime_writes_enabled",
        "background_refresh_enabled",
        "local_ui_refresh_enabled",
        "refresh_available_via_ui",
        "mode",
        "operating_state",
        "last_refresh",
    }


def test_api_status_exposes_runtime_and_result_delivery(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    match = basketball_match("500lq-2030-05-16-7312")
    snapshots = {
        "snapshots": {
            "snap-1": {
                "snapshot_id": "snap-1",
                "match_id": match["id"],
                "kickoff_at": "2030-05-16T10:00:00+08:00",
                "generated_at": "2030-05-15T12:00:00+08:00",
            }
        },
        "version": 1,
    }
    write_stores(tmp_path, [match], extras={"prediction_snapshots.json": snapshots})
    monkeypatch.setitem(app_module.app.config, "RUNTIME_WRITES_ENABLED", False)
    monkeypatch.setitem(app_module.app.config, "LOCAL_UI_REFRESH_ENABLED", False)
    monkeypatch.setitem(app_module.app.config, "RUNTIME_WRITE_TOKEN", "hidden")

    clock = datetime(2030, 5, 17, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    with override_beijing_now(clock):
        client = app_module.app.test_client()
        response = client.get("/api/status")
    assert response.status_code == 200
    data = response.get_json()
    assert data["runtime"]["operating_state"] == "READ_ONLY_APPLICATION"
    assert data["runtime"]["refresh_available_via_ui"] is False
    assert "result_delivery" in data["source_health"]
    delivery = data["source_health"]["result_delivery"]
    assert delivery["pending_total"] >= 1
    assert delivery["sample_targets"]
    assert "hidden" not in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# /api/refresh：默认拒绝，浏览器头不构成授权
# ---------------------------------------------------------------------------


BROWSER_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Site": "same-origin",
    "Origin": "http://127.0.0.1:5000",
    "Referer": "http://127.0.0.1:5000/",
}


def _spy_refresh(monkeypatch, *, error=None):
    calls = []

    def fake_refresh(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return {"status": "ok", "synthetic": True}

    monkeypatch.setattr(app_module.scraper, "refresh", fake_refresh)
    return calls


def test_api_refresh_rejects_even_same_origin_browser_headers_when_writes_disabled(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=False, local_ui=True, token="token-x")
    calls = _spy_refresh(monkeypatch)
    response = app_module.app.test_client().post("/api/refresh", headers=BROWSER_HEADERS)
    assert response.status_code == 403
    assert response.get_json()["error"] == "RUNTIME_WRITES_DISABLED"
    assert calls == []


def test_api_refresh_requires_token_when_local_ui_channel_is_closed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=True, local_ui=False, token="token-x")
    calls = _spy_refresh(monkeypatch)
    client = app_module.app.test_client()

    refused = client.post("/api/refresh", headers=BROWSER_HEADERS)
    assert refused.status_code == 403
    assert refused.get_json()["error"] == "REFRESH_AUTHORIZATION_REQUIRED"
    assert calls == []

    wrong = client.post("/api/refresh", headers={**BROWSER_HEADERS, "X-Refresh-Token": "nope"})
    assert wrong.status_code == 403
    assert calls == []

    allowed = client.post("/api/refresh", headers={"X-Refresh-Token": "token-x"})
    assert allowed.status_code == 200
    assert allowed.get_json()["synthetic"] is True
    assert len(calls) == 1


def test_api_refresh_rejects_non_loopback_even_with_local_ui_enabled(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=True, local_ui=True, token=None)
    calls = _spy_refresh(monkeypatch)
    response = app_module.app.test_client().post(
        "/api/refresh",
        headers=BROWSER_HEADERS,
        environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert response.status_code == 403
    assert response.get_json()["error"] == "REFRESH_AUTHORIZATION_REQUIRED"
    assert calls == []


def test_api_refresh_allows_loopback_local_ui_only_with_both_switches(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=True, local_ui=True, token=None)
    calls = _spy_refresh(monkeypatch)
    response = app_module.app.test_client().post("/api/refresh", headers=BROWSER_HEADERS)
    assert response.status_code == 200
    assert len(calls) == 1


def test_api_refresh_reports_lock_owner_as_advisory_busy(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, writes=True, local_ui=True, token=None)
    owner = {"pid": 4242, "host": "host-a", "action": "refresh", "started_at": "2030-05-16T00:00:00+08:00"}
    _spy_refresh(monkeypatch, error=RefreshBusyError("refresh in progress", owner=owner))
    response = app_module.app.test_client().post("/api/refresh", headers=BROWSER_HEADERS)
    assert response.status_code == 409
    body = response.get_json()
    assert body["status"] == "busy"
    assert body["active_writer"] == owner
    # 触发器来源不从 owner 推断：响应只给出建议性持有者。
    assert "trigger" not in body
