"""User launchd service for a wheel installation; never handles application data migration."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL = "com.mino.nexus"


def service_document(data: Path, logs: Path, host: str, port: int) -> dict:
    # Keep the venv path: resolving its python symlink would lose installed dependencies.
    python = os.path.abspath(sys.executable)
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "mino_nexus.cli", "--prod", "--host", host,
                             "--port", str(port), "--data-dir", str(data)],
        "WorkingDirectory": str(data),
        "EnvironmentVariables": {"MINO_NEXUS_MODE": "production", "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "nexus.log"),
        "StandardErrorPath": str(logs / "nexus-error.log"),
    }


def launchctl(*args: str, check: bool = True):
    result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "launchctl failed")
    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mino-nexus service")
    ap.add_argument("command", choices=["install", "start", "stop", "restart", "status"])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=50104)
    ap.add_argument("--data-dir")
    args = ap.parse_args(argv)
    if sys.platform != "darwin":
        ap.error("service currently supports macOS launchd only")
    if not 1 <= args.port <= 65535:
        ap.error("port must be between 1 and 65535")
    path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    domain = f"gui/{os.getuid()}"
    target = domain + "/" + LABEL
    try:
        state = launchctl("print", target, check=False)
        if state.returncode and "Could not find service" not in state.stderr:
            raise RuntimeError(state.stderr.strip() or "Cannot determine launchd service state")
        loaded = state.returncode == 0
        if args.command == "status":
            print(json.dumps({"installed": path.is_file(), "loaded": loaded,
                              "running": loaded and "state = running" in state.stdout,
                              "details": state.stdout if loaded else "not loaded"}))
            return 0
        if args.command == "install":
            os.environ["MINO_NEXUS_MODE"] = "production"
            if args.data_dir is not None:
                if not args.data_dir.strip():
                    ap.error("--data-dir must not be empty")
                os.environ["MINO_NEXUS_DATA_DIR"] = args.data_dir
            from mino_nexus.core.paths import get_app_data_dir
            data = get_app_data_dir()
            logs = Path.home() / "Library/Logs/Mino/nexus"
            doc = service_document(data, logs, args.host, args.port)
            if path.exists():
                if path.is_symlink() or plistlib.loads(path.read_bytes()) != doc:
                    raise ValueError(f"Existing service configuration differs; preserve and review {path}")
            else:
                logs.mkdir(parents=True, exist_ok=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    plistlib.dump(doc, stream)
            if not loaded:
                launchctl("bootstrap", domain, str(path))
        elif args.command == "stop":
            if loaded:
                launchctl("bootout", target)
        else:
            if not path.is_file():
                raise ValueError("Run mino-nexus service install first")
            if args.command == "restart" and loaded:
                launchctl("kickstart", "-k", target)
            elif not loaded:
                launchctl("bootstrap", domain, str(path))
            elif "state = running" not in state.stdout:
                launchctl("kickstart", target)
        print(f"{LABEL}: {args.command} complete")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
