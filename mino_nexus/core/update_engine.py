"""Crash-conservative Nexus update helper, independent of the active release.

A crash in an effectful phase stops in needs_operator unless the persisted facts
prove a safe continuation. No process kill escalation or post-start DB rollback.
"""
from __future__ import annotations
from mino_nexus.core.deployment import legacy_path
from mino_nexus.core import platform_runtime as platform_runtime
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

from mino_nexus.core.deployment import Deployment, TERMINAL, atomic_json, identifier, read_json
from mino_nexus.core.release_package import fetch_fixed, stage_release


def release_env(d: Deployment, build: str) -> dict:
    cfg = d.config
    release = d.release(build)
    return {**os.environ, "MINO_WORKBENCH_INSTALL_ROOT": cfg.get("workbench_install_root", ""),
            "MINO_NEXUS_MDNS": "0", "MINO_NEXUS_DATA_DIR": cfg["data_dir"],
            "MINO_NEXUS_INSTALL_ROOT": str(d.root), "PYINSTALLER_RESET_ENVIRONMENT": "1",
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def release_command(d: Deployment, build: str, *args) -> list[str]:
    return [str(platform_runtime.executable(d.release(build) / "app/mino-nexus")), *args]


def control_service(d: Deployment, action: str) -> None:
    cfg = d.config
    if cfg["service_manager"] == "user-process":
        if action == "stop": platform_runtime.stop(d.root)
        else: platform_runtime.start(d.root, [platform_runtime.executable(d.root / "launcher/mino-maintain"), "d2", "serve", "--root", d.root])
        return
    if cfg["service_manager"] != "launchd":
        raise RuntimeError("Only macOS launchd is enabled; other service managers need validation")
    domain = f"gui/{os.getuid()}"
    target = domain + "/" + cfg["service_label"]
    if action == "stop":
        # Do not ignore arbitrary bootout failures or infer exit from a lost heartbeat.
        probe = subprocess.run(["launchctl", "print", target], capture_output=True)
        if probe.returncode != 0:
            if b"Could not find service" in probe.stderr:
                # The lifetime lock is checked by _wait_exit before any backup,
                # migration or switch. A missing registration alone proves nothing.
                return
            raise RuntimeError("Registered application service is unavailable; operator verification required")
        # bootout applies launchd's short ExitTimeOut and may SIGKILL a writer.
        # Request a graceful exit first; leave the job registered on timeout.
        if re.search(rb"^\s*pid = \d+", probe.stdout, re.MULTILINE):
            result = subprocess.run(["launchctl", "kill", "SIGTERM", target], capture_output=True, timeout=10)
            if result.returncode:
                raise RuntimeError("Application termination request failed; registration preserved")
        until = time.monotonic() + 40
        while True:
            probe = subprocess.run(["launchctl", "print", target], capture_output=True, timeout=10)
            if probe.returncode:
                raise RuntimeError("Service state changed during graceful stop; operator verification required")
            if not re.search(rb"^\s*pid = \d+", probe.stdout, re.MULTILINE):
                with d.lock("runtime", blocking=False):
                    break
            if time.monotonic() >= until:
                raise RuntimeError("Application has not exited; registration preserved without force kill")
            time.sleep(0.5)
        cmd = ["launchctl", "bootout", target]
    else:
        probe = subprocess.run(["launchctl", "print", target], capture_output=True, timeout=10)
        if probe.returncode == 0:
            cmd = ["launchctl", "kickstart", target]  # no -k: an active service is not restarted
        elif b"Could not find service" in probe.stderr:
            cmd = ["launchctl", "bootstrap", domain, cfg["service_plist"]]
        else:
            raise RuntimeError("Cannot inspect application service before start")
    result = subprocess.run(cmd, capture_output=True, timeout=40)
    if result.returncode:
        raise RuntimeError(f"launchctl {action} failed ({result.returncode})")


def activity(d: Deployment) -> dict:
    """Unbounded aggregate query: no first-page idle inference, no schema mutation."""
    path = Path(d.config["data_dir"]) / "mino.db"
    if not path.exists():
        raise RuntimeError("Registered database missing")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        runs = db.execute("SELECT COUNT(*) FROM app_regression_runs WHERE status='running'").fetchone()[0]
        jobs = db.execute("SELECT COUNT(*) FROM case_compile_jobs WHERE status='running'").fetchone()[0]
        development = 0
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='development_operations'").fetchone():
            development = db.execute("SELECT COUNT(*) FROM development_operations WHERE state='running' OR (state='unknown' AND COALESCE(json_extract(result, '$.manual_review.confirmed_idle'), 0) != 1)").fetchone()[0]
        # Older compatible releases predate Scout updates. Do not create schema
        # from the maintenance helper; unresolved dispatched installs stay busy.
        scouts = 0
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scout_updates'").fetchone():
            scouts = db.execute("SELECT COUNT(*) FROM scout_updates WHERE slot IS NOT NULL AND state NOT IN ('waiting_idle','succeeded','failed','rolled_back','cancelled','expired')").fetchone()[0]
    return {"runs": runs, "compilations": jobs, "development": development, "scout_updates": scouts}


def compatibility(d: Deployment, old: str, new: str) -> dict:
    path = d.root / "config/compatibility" / (old + "--" + new + ".json")
    record = read_json(path)
    if (record.get("from_build"), record.get("to_build")) != (old, new):
        raise ValueError("Compatibility evidence does not match fixed releases")
    for field in ("old_start_and_rw_verified", "migration_reentrant", "writers_audited", "no_external_migration_effects"):
        if record.get(field) is not True:
            raise ValueError("Compatibility evidence incomplete: " + field)
    if not record.get("evidence"):
        raise ValueError("Compatibility record needs actual rehearsal evidence")
    # Bind approval to exact immutable package bytes, not mutable build names.
    for build, key in ((old, "from_sha256"), (new, "to_sha256")):
        if record.get(key) != read_json(d.release(build) / "mino-release.json")["package_sha256"]:
            raise ValueError("Compatibility evidence package mismatch")
    return record


def request(d: Deployment, operation_id: str, build: str, *, wait_seconds=1800, rollback=False) -> dict:
    raise RuntimeError("upgrade_and_rollback_removed")


def cancel(d: Deployment, operation_id: str) -> dict:
    with d.lock():
        row = d.state()
        if row.get("operation_id") != identifier(operation_id):
            raise ValueError("This operation does not own the hold")
        if row["state"] not in {"requested", "waiting_idle"}:
            raise ValueError("Only waiting updates can be cancelled")
        return d.save(row, state="cancelled")


def backup(d: Deployment, row: dict) -> Path:
    source = Path(d.config["data_dir"]).resolve(strict=True)
    target = d.root / "updates" / ("backup-" + row["operation_id"])
    # Back up all managed files, not just mino.db; reject links outside that scope.
    if target.exists():
        raise ValueError("Backup already exists; never overwrite a previous backup")
    for p in source.rglob("*"):
        if p.is_symlink() or (p.is_file() and p.stat().st_nlink > 1):
            raise ValueError("Managed data contains links; requires an explicit backup strategy")
    shutil.copytree(source, target)
    with sqlite3.connect((target / "mino.db").as_uri() + "?mode=ro", uri=True) as db:
        result = db.execute("PRAGMA quick_check").fetchall()
        if result != [("ok",)]:
            raise ValueError("Backup SQLite integrity check failed")
    atomic_json(target / "mino-backup.json", {"operation_id": row["operation_id"], "complete": True,
                                               "source": str(source), "created_at": time.time()})
    return target


def restore_prestart(d: Deployment, row: dict) -> None:
    raise RuntimeError("upgrade_and_rollback_removed")


def health_probe(d: Deployment, build: str) -> None:
    cfg = d.config
    token = (legacy_path(d.root / "config/mino-probe.token")).read_text().strip()
    base = cfg["probe_url"].rstrip("/")
    # Never send a deployment token to redirects or proxy-discovered targets.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def get(path, body=None):
        headers = {"Content-Type": "application/json"}
        if body is not None:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        stage = path + (":" + body["method"] if body else "")
        try:
            with opener.open(req, timeout=5) as response:
                return json.load(response)
        except (OSError, ValueError) as exc:
            # Do not include request headers or credentials in diagnostics.
            raise RuntimeError(f"Health probe failed at {stage}: {type(exc).__name__}; see logs/process.log") from exc
    if get("/health").get("ok") is not True:
        raise RuntimeError("Health endpoint failed")
    info = get("/sys/server_info").get("data", {})
    if info.get("instance_id") != cfg["instance_id"] or info.get("build_id") != build or info.get("health_contract_version") != 1:
        raise RuntimeError("Target instance/build/health contract mismatch")
    for i, method in enumerate(("initialize", "ping", "tools/list"), 1):
        msg = get("/mcp", {"jsonrpc": "2.0", "id": i, "method": method, "params": {}})
        if msg.get("id") != i or "error" in msg or not isinstance(msg.get("result"), dict):
            raise RuntimeError("MCP health probe failed: " + method)


def await_health(d: Deployment, build: str) -> None:
    until = time.monotonic() + 60
    while True:
        try:
            health_probe(d, build)
            return
        except Exception:
            if time.monotonic() >= until:
                raise
            time.sleep(2)


def _wait_exit(d: Deployment):
    until = time.monotonic() + 40
    while True:
        try:
            # Held by installed application for its entire lifetime, not heartbeat.
            guard = d.lock("runtime", blocking=False)
            guard.__enter__()
            return guard
        except BlockingIOError:
            if time.monotonic() >= until:
                raise RuntimeError("Old application has not exited; will not force kill")
            time.sleep(0.5)


def execute(d: Deployment) -> dict:
    # Independent helper exclusion lasts through stop/start/probe. Control lock is
    # short-lived so queries and cancellation remain available while waiting.
    raise RuntimeError("upgrade_and_rollback_removed")
