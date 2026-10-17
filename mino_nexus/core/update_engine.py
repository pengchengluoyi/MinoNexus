"""Crash-conservative Nexus update helper, independent of the active release.

A crash in an effectful phase stops in needs_operator unless the persisted facts
prove a safe continuation. No process kill escalation or post-start DB rollback.
"""
from __future__ import annotations
from mino_nexus.core.deployment import legacy_path
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
    return [str(d.release(build) / "app/mino-nexus"), *args]


def control_service(d: Deployment, action: str) -> None:
    cfg = d.config
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
        cmd = ["launchctl", "bootstrap", domain, cfg["service_plist"]]
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
    identifier(operation_id); identifier(build)
    if not d.config.get("registered"):
        raise ValueError("Formal service baseline has not passed registration and health verification")
    if not 1 <= wait_seconds <= 1800:
        raise ValueError("wait_seconds must be 1..1800")
    with d.lock():
        history = d.root / "updates" / ("mino-" + operation_id + ".json")
        if legacy_path(history).exists():
            old = read_json(history)
            if (old["target_build"], old["rollback"], old["wait_seconds"]) != (build, rollback, wait_seconds):
                raise ValueError("operation_id already belongs to different parameters")
            return old
        active = d.state()
        if active and active.get("state") not in TERMINAL:
            if active.get("state") in {"requested", "waiting_idle"} and active["deadline"] <= time.time():
                d.save(active, state="expired")
            else:
                raise ValueError("Another update or unresolved maintenance operation owns this deployment")
        current = d.current_build()
        if current == build:
            raise ValueError("Target build is already current")
        # Package staging/compatibility approval happens before claiming the hold.
        compatibility(d, current, build)
        return d.save({"operation_id": operation_id, "target_build": build, "previous_build": current,
            "rollback": rollback, "wait_seconds": wait_seconds, "created_at": time.time(),
            "deadline": time.time() + wait_seconds, "state": "waiting_idle", "phase": "waiting_idle",
            "writes_reopened": False, "backup": "", "error": ""})


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
    if row["writes_reopened"]:
        raise RuntimeError("Data restore forbidden after normal startup")
    source = Path(row["backup"])
    if not read_json(source / "mino-backup.json").get("complete"):
        raise RuntimeError("No complete backup")
    data = Path(d.config["data_dir"])
    retained = data.with_name(data.name + ".failed-" + row["operation_id"])
    if retained.exists():
        raise RuntimeError("Previous recovery preserved; operator required")
    prepared = data.with_name(data.name + ".restore-" + row["operation_id"])
    shutil.copytree(source, prepared)
    os.rename(data, retained)  # Preserve failed migration data; do not delete it.
    os.rename(prepared, data)


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
        with opener.open(req, timeout=5) as response:
            return json.load(response)
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
    with d.lock("helper", blocking=False):
        resume_target = ""
        while True:
            with d.lock():
                row = d.state()
                if not row or row["state"] in TERMINAL or row["state"] == "needs_operator":
                    return row
                if row["state"] not in {"waiting_idle", "requested"}:
                    # Probe once without replaying migration, startup or package
                    # installation. A successful target can complete the original op.
                    if row["phase"] in {"starting", "probing", "rolling_back", "operator_recovery"}:
                        target = row["previous_build"] if row["phase"] in {"rolling_back", "operator_recovery"} else row["target_build"]
                        # HTTP middleware also uses control; probe after releasing it.
                        resume_target = target
                        break
                    return d.save(row, state="needs_operator", error="Helper interrupted during " + row["phase"])
                if time.time() >= row["deadline"]:
                    return d.save(row, state="expired")
                compatibility(d, row["previous_build"], row["target_build"])
                busy = activity(d)
                try:
                    with d.lock("writers", blocking=False):
                        idle = not any(busy.values())
                except BlockingIOError:
                    idle = False
                if idle:
                    row = d.save(row, state="installing", phase="stopping")
                    break
            time.sleep(2)
        if resume_target:
            try:
                await_health(d, resume_target)
                return d.save(row, state="succeeded" if resume_target == row["target_build"] else "rolled_back", phase="complete")
            except Exception as exc:
                return d.save(row, state="needs_operator", error="Interrupted startup could not be verified: " + type(exc).__name__)
        exited = None
        try:
            control_service(d, "stop")
            exited = _wait_exit(d)
            row = d.save(row, phase="backing_up")
            snapshot = backup(d, row)
            row = d.save(row, backup=str(snapshot), phase="migrating")
            build = row["target_build"]
            log_path = d.root / "updates" / ("mino-" + row["operation_id"] + "-migration.log")
            exited.__exit__(None, None, None); exited = None
            with log_path.open("ab") as log:
                result = subprocess.run(release_command(d, build, "maintenance", "migrate", "--data-dir", d.config["data_dir"], "--root", str(d.root)),
                    env=release_env(d, build), cwd=d.release(build), stdout=log, stderr=log)
            exited = _wait_exit(d)
            if result.returncode:
                raise RuntimeError(f"Offline migration failed ({result.returncode})")
            row = d.save(row, phase="switching")
            d.switch(build)
            # Persist before bootstrapping: crash here must prohibit restoring DB.
            row = d.save(row, state="restarting", phase="starting", writes_reopened=True)
            exited.__exit__(None, None, None); exited = None
            control_service(d, "start")
            row = d.save(row, state="verifying", phase="probing")
            await_health(d, build)
            return d.save(row, state="succeeded", phase="complete")
        except Exception as exc:
            row = d.save(row, error=type(exc).__name__ + ": " + str(exc))
            try:
                if not row["backup"]:
                    raise RuntimeError("No complete backup; operator must verify stop/start state")
                if row["writes_reopened"]:
                    control_service(d, "stop")
                    exited = _wait_exit(d)
                else:
                    restore_prestart(d, row)
                compatibility(d, row["previous_build"], row["target_build"])
                row = d.save(row, phase="rolling_back")
                d.switch(row["previous_build"])
                row = d.save(row, writes_reopened=True)
                if exited:
                    exited.__exit__(None, None, None); exited = None
                control_service(d, "start")
                await_health(d, row["previous_build"])
                return d.save(row, state="rolled_back", phase="complete")
            except Exception as recovery:
                return d.save(row, state="needs_operator", recovery_error=type(recovery).__name__ + ": " + str(recovery))
        finally:
            if exited:
                exited.__exit__(None, None, None)
