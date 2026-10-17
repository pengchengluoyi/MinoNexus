"""Local installation/maintenance entry point; no ORM imports before explicit data root."""
from __future__ import annotations
from mino_nexus.core.deployment import legacy_path
from mino_nexus.core import platform_runtime
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import uuid

from mino_nexus.core.deployment import Deployment, atomic_json, identifier, read_json
from mino_nexus.core.release_package import stage_release, fetch_fixed


def _explicit_data(raw: str) -> Path:
    if not raw or not Path(raw).expanduser().is_absolute():
        raise ValueError("An explicit absolute data directory is required")
    path = Path(raw).expanduser().resolve()
    os.environ["MINO_NEXUS_DATA_DIR"] = str(path)
    return path


def migrate(raw: str) -> dict:
    data = _explicit_data(raw)
    from mino_nexus.core.database import Base, ensure_db, engine
    from sqlalchemy import inspect, text
    ensure_db()  # same create_all/migration/bootstrap implementation as normal startup
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        actual = {c["name"] for c in inspector.get_columns(table.name)}
        if not {c.name for c in table.columns}.issubset(actual):
            raise RuntimeError("Schema missing columns: " + table.name)
    with engine.connect() as conn:
        if conn.execute(text("PRAGMA quick_check")).fetchall() != [("ok",)]:
            raise RuntimeError("SQLite integrity check failed")
    return {"schema_valid": True, "data_dir": str(data)}


def _plist(path: Path, doc: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        plistlib.dump(doc, f)


def install(root: Path, manifest_path: Path, package: Path, data: str, port: int, host: str, repo: str) -> dict:
    if sys.platform not in {"darwin", "linux", "win32"}:
        raise ValueError("Unsupported installation platform")
    d = Deployment(root)
    if d.config_path.exists() or (d.root / "current").exists() or (d.root / "current.json").exists() or (d.root / "config/installation.json").exists():
        raise ValueError("Deployment already exists; use staged update")
    data_path = _explicit_data(data)
    # A managed data root must be stable and outside releases/updates/runtime.
    if data_path == d.root or d.root.is_relative_to(data_path) or (data_path.is_relative_to(d.root) and data_path != d.root / "data"):
        raise ValueError("Data root overlaps deployment infrastructure")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    with socket.socket() as s:
        s.bind((host, port))  # startup performs its own strict bind as well
    manifest = read_json(manifest_path)
    release = stage_release(d, manifest, package)
    build = manifest["build_id"]
    launcher = d.root / "launcher"
    if launcher.exists():
        raise ValueError("Previous installation attempt exists; preserve and inspect it")
    shutil.copytree(release / "launcher", launcher)
    label = "local.mino.nexus." + uuid.uuid4().hex[:12]
    service = d.root / "config/mino-nexus.plist"
    helper = d.root / "config/mino-update-helper.plist"
    cfg = {"version": 1, "environment": "production", "instance_id": "", "data_dir": str(data_path),
        "host": host, "port": port, "probe_url": f"http://127.0.0.1:{port}",
        "release_repository": repo, "service_manager": "launchd" if sys.platform == "darwin" else "user-process", "service_label": label,
        "helper_label": label + ".updater", "service_plist": str(service), "helper_plist": str(helper)}
    atomic_json(d.config_path, cfg)
    d.switch(build)
    if sys.platform != "darwin":
        return {"installed": True, "registered": False, "build_id": build, "instance_id": "",
                "next": "Provision probe, then register and start the user process"}
    common = {"WorkingDirectory": str(launcher), "KeepAlive": False, "RunAtLoad": False}
    command = [str(launcher / "mino-maintain"), "d2"]
    _plist(service, {**common, "Label": label, "RunAtLoad": True,
        "ProgramArguments": command + ["serve", "--root", str(d.root)]})
    _plist(helper, {**common, "Label": cfg["helper_label"],
        "ProgramArguments": command + ["helper", "--root", str(d.root)]})
    # No migration, token issuance or service start during installation planning.
    return {"installed": True, "registered": False, "build_id": build, "instance_id": "",
            "next": "Provision probe in this deployment, then register the two services"}


def provision(d: Deployment):
    """Use the current native application to provision; never the helper's ORM."""
    from mino_nexus.core.update_engine import release_command, release_env
    target = legacy_path(d.root / "config/mino-probe.token")
    if target.exists():
        raise ValueError("Probe already provisioned; refuse to overwrite")
    build = d.current_build()
    result = subprocess.run(release_command(d, build, "probe", "issue", "--data-dir", d.config["data_dir"], "--output", str(target)),
        env=release_env(d, build), cwd=d.release(build), capture_output=True)
    if result.returncode:
        raise RuntimeError("Target release probe provisioning failed")
    # build_info owns this identity. First offline initialization can create it;
    # otherwise create it using the same UUID file contract, without replacing it.
    path = Path(d.config["data_dir"]) / "instance-id"
    try:
        with path.open("x") as f:
            f.write(str(uuid.uuid4()))
    except FileExistsError:
        pass
    instance = str(uuid.UUID(path.read_text().strip()))
    atomic_json(d.config_path, {**d.config, "instance_id": instance})
    return {"probe_provisioned": True, "instance_id": instance}


def serve(d: Deployment):
    cfg = d.config
    build = d.current_build()
    row = d.state()
    if row and row.get("state") not in {"succeeded", "rolled_back", "cancelled", "expired", "failed", "waiting_idle", "requested"}:
        if row.get("phase") not in {"starting", "probing", "rolling_back", "operator_recovery"}:
            raise RuntimeError("Unresolved maintenance phase prevents normal startup")
    from mino_nexus.core.update_engine import release_command, release_env
    env = release_env(d, build)
    command = release_command(d, build, "maintenance", "run-server", "--root", str(d.root))
    if cfg["service_manager"] == "user-process":
        return platform_runtime.run_service(d.root, command, env)
    os.execve(command[0], command, env)


def run_server(d: Deployment):
    cfg = d.config
    _explicit_data(cfg["data_dir"])
    with d.lock("runtime", blocking=False):
        row = d.state()
        if row and row.get("state") not in {"succeeded", "rolled_back", "failed", "cancelled", "expired", "requested", "waiting_idle"}:
            if row.get("phase") not in {"starting", "probing", "rolling_back", "operator_recovery"}:
                raise RuntimeError("Unresolved maintenance prevents startup")
        # No detached child supervisor: app itself holds the lifetime lock.
        import logging
        from logging.handlers import RotatingFileHandler
        logdir = d.root / "logs"; logdir.mkdir(exist_ok=True)
        handler = RotatingFileHandler(logdir / "mino-nexus.log", maxBytes=5*1024*1024, backupCount=5)
        from mino_nexus.core.log import logger, LogFormatter
        handler.setFormatter(LogFormatter())
        logger.addHandler(handler)
        from mino_nexus.cli import main as server_main
        return server_main(["--host", cfg["host"], "--port", str(cfg["port"])])


def kick(d: Deployment):
    if d.config["service_manager"] == "user-process":
        platform_runtime.spawn([platform_runtime.executable(d.root / "launcher/mino-maintain"), "d2", "helper", "--root", d.root], d.root)
        return
    result = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{d.config['helper_label']}"], capture_output=True)
    if result.returncode:
        raise RuntimeError("Update is persisted but helper kickstart failed; query/resume the same operation locally")


def register(d: Deployment):
    if not (legacy_path(d.root / "config/mino-probe.token")).is_file():
        raise ValueError("Provision probe before registering services")
    if d.config["service_manager"] == "user-process":
        from mino_nexus.core.update_engine import control_service, await_health
        control_service(d, "start")
        await_health(d, d.current_build())
        atomic_json(d.config_path, {**d.config, "registered": True})
        return {"registered": True, "health_verified": True, "service_manager": "user-process"}
    for key, label in (("helper_plist", "helper_label"), ("service_plist", "service_label")):
        target = f"gui/{os.getuid()}/{d.config[label]}"
        existing = subprocess.run(["launchctl", "print", target], capture_output=True)
        if existing.returncode == 0:
            if label == "service_label":
                # Without -k this starts an exited job without restarting a live one.
                result = subprocess.run(["launchctl", "kickstart", target], capture_output=True, timeout=40)
                if result.returncode:
                    raise RuntimeError("Registered application could not be started")
            continue  # Resume a partially completed registration, preserving its label.
        if b"Could not find service" not in existing.stderr:
            raise RuntimeError("Cannot inspect service registration: " + label)
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", d.config[key]], capture_output=True)
        if result.returncode:
            raise RuntimeError("Service registration failed: " + key)
    from mino_nexus.core.update_engine import await_health
    await_health(d, d.current_build())
    atomic_json(d.config_path, {**d.config, "registered": True})
    return {"registered": True, "health_verified": True}


def recover(d: Deployment, mode: str):
    from mino_nexus.core.update_engine import _wait_exit, control_service, restore_prestart, await_health, compatibility
    with d.lock("helper", blocking=False):
        row = d.state()
        if row.get("state") != "needs_operator":
            raise ValueError("Recovery requires needs_operator state")
        if mode not in {"code-only", "prestart-backup", "restart-previous"}:
            raise ValueError("Unknown recovery mode")
        if mode == "restart-previous" and (
            row.get("phase") not in {"stopping", "backing_up"}
            or row.get("writes_reopened")
            or d.current_build() != row["previous_build"]
        ):
            raise ValueError("Restart without restore requires proof that migration never started")
        # Never infer that old/new processes have stopped from a missing journal.
        control_service(d, "stop")
        stopped = _wait_exit(d)
        try:
            compatibility(d, row["previous_build"], row["target_build"])
            if mode == "prestart-backup":
                restore_prestart(d, row)
            elif mode == "code-only" and not row.get("writes_reopened"):
                raise ValueError("Pre-start failure requires explicit backup recovery")
            d.switch(row["previous_build"])
            row = d.save(row, state="restarting", phase="operator_recovery", writes_reopened=True)
        finally:
            stopped.__exit__(None, None, None)
        try:
            control_service(d, "start")
            await_health(d, row["previous_build"])
            return d.save(row, state="rolled_back", phase="complete")
        except Exception as exc:
            return d.save(row, state="needs_operator", recovery_error=str(exc))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mino-nexus maintenance")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("migrate"); p.add_argument("--data-dir", required=True); p.add_argument("--root", type=Path)
    for name in ("install", "stage", "fetch", "check-update", "bind-workbench", "request", "status", "cancel", "helper", "serve", "run-server", "provision-probe", "register", "probe", "recover", "start", "stop", "service-status"):
        p = sub.add_parser(name); p.add_argument("--root", type=Path, required=True)
        if name in {"install", "stage"}:
            p.add_argument("--manifest", type=Path, required=True); p.add_argument("--package", type=Path, required=True)
        if name == "install":
            p.add_argument("--data-dir", required=True); p.add_argument("--port", type=int)
            p.add_argument("--host", default="0.0.0.0"); p.add_argument("--repository", default="pengchengluoyi/MinoNexus")
        if name in {"request", "status", "cancel"}:
            p.add_argument("--operation-id", required=name != "status", default="")
        if name in {"fetch", "check-update"}:
            p.add_argument("--tag", default="", required=name == "fetch")
        if name == "fetch":
            p.add_argument("--build", required=True)
        if name == "bind-workbench":
            p.add_argument("--workbench-root", type=Path, required=True)
        if name == "request":
            p.add_argument("--build", required=True); p.add_argument("--wait-seconds", type=int, default=1800)
            p.add_argument("--rollback", action="store_true")
        if name == "recover":
            p.add_argument("--mode", choices=["code-only", "prestart-backup", "restart-previous"], required=True)
    args = ap.parse_args(argv)
    try:
        if args.command == "migrate":
            if args.root:
                with Deployment(args.root).lock("runtime", blocking=False):
                    result = migrate(args.data_dir)
            else:
                result = migrate(args.data_dir)
        else:
            d = Deployment(args.root)
            from mino_nexus.core import update_engine as engine
            if args.command == "install":
                result = install(args.root, args.manifest, args.package, args.data_dir, args.port if args.port is not None else 50104, args.host, args.repository)
            elif args.command in {"start", "stop"}:
                engine.control_service(d, args.command)
                if args.command == "start": engine.await_health(d, d.current_build())
                result = {"service": args.command}
            elif args.command == "service-status":
                if d.config["service_manager"] == "user-process": result = {"running": platform_runtime.running(d.root)}
                else:
                    probe = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/" + d.config['service_label']], capture_output=True)
                    result = {"registered": probe.returncode == 0}
            elif args.command == "stage":
                with d.lock():
                    result = {"release": str(stage_release(d, read_json(args.manifest), args.package))}
            elif args.command == "bind-workbench":
                path = args.workbench_root.expanduser().resolve(strict=True)
                config = read_json(path / "config/installation.json")
                if config.get("component") != "workbench" or not config.get("installation_id"):
                    raise ValueError("Workbench installation does not support managed updates")
                with d.lock():
                    atomic_json(d.config_path, {**d.config, "workbench_install_root": str(path)})
                result = {"workbench_bound": True, "installation_id": config["installation_id"]}
            elif args.command == "check-update":
                from mino_nexus.core.github_release import check
                result = check(d.config["release_repository"], "nexus", read_json(d.release(d.current_build()) / "release.json"), args.tag)
            elif args.command == "fetch":
                with d.lock("package", blocking=False):
                    manifest, package = fetch_fixed(d, args.tag, args.build)
                    result = {"release": str(stage_release(d, manifest, package))}
            elif args.command == "request":
                result = engine.request(d, args.operation_id, args.build, wait_seconds=args.wait_seconds, rollback=args.rollback)
                if result["state"] in {"requested", "waiting_idle"}:
                    kick(d)
            elif args.command == "status":
                result = d.operation(args.operation_id) if args.operation_id else d.state()
            elif args.command == "cancel": result = engine.cancel(d, args.operation_id)
            elif args.command == "helper": result = engine.execute(d)
            elif args.command == "serve": return serve(d)
            elif args.command == "run-server": return run_server(d)
            elif args.command == "provision-probe": result = provision(d)
            elif args.command == "register": result = register(d)
            elif args.command == "probe":
                engine.health_probe(d, d.current_build()); result = {"health_verified": True}
            elif args.command == "recover": result = recover(d, args.mode)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("state") in {"failed", "needs_operator"} else 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
