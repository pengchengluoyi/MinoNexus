"""Fresh-install baseline only. Does not implement in-place update or database rollback."""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import zipfile

def load(root):
    root = root.expanduser().resolve(strict=True)
    data = json.loads((root / "config/installation.json").read_text())
    return root, data

def install(args):
    root = args.root.expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise ValueError("installation root already exists; baseline installer never overwrites it")
    if root.parent.resolve() != root.parent:
        raise ValueError("installation parent must be resolved, not a symlink")
    archive = args.package.resolve(strict=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != args.sha256:
        raise ValueError("package digest mismatch")
    with socket.socket() as listener:
        listener.bind((args.host, args.port))
    with zipfile.ZipFile(archive) as z:
        info = json.loads(z.read("release.json"))
        component, build = info["component"], info["build_id"]
        if component not in {"nexus", "workbench"} or not re.fullmatch(r"[A-Za-z0-9._-]+", build):
            raise ValueError("invalid component/build")
        import platform
        machine = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        if (info["os"], info["arch"]) != (sys.platform, machine):
            raise ValueError("package platform mismatch")
        for row in z.infolist():
            part = Path(row.filename)
            if part.is_absolute() or ".." in part.parts or "\\" in row.filename:
                raise ValueError("unsafe archive path")
            if stat.S_ISLNK(row.external_attr >> 16):
                raise ValueError("archive symlinks are not accepted")
        root.mkdir(mode=0o700)
        for name in ("launcher", "releases", "config", "updates", "logs"):
            (root / name).mkdir()
        release = root / "releases" / build
        release.mkdir()
        for row in z.infolist():
            if row.filename.startswith("app/") and not row.is_dir():
                target = release / row.filename[4:]
            elif row.filename.startswith("launcher/") and not row.is_dir():
                target = root / row.filename
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(z.read(row))
            target.chmod((row.external_attr >> 16) & 0o777 or 0o644)
        (release / "release.json").write_text(json.dumps(info, indent=2))
    data_root = args.data_dir.expanduser().resolve() if args.data_dir else root / "data"
    if component == "nexus" and args.data_dir is None:
        data_root.mkdir()
    if component == "nexus" and not data_root.is_dir():
        raise ValueError("registered data root must already exist")
    conf = {"component": component, "build_id": build, "data_dir": str(data_root),
            "host":args.host, "port":args.port, "nexus_url":args.nexus_url,
            "mdns":args.mdns, "label":args.label or "com.mino." + component}
    if not re.fullmatch(r"[A-Za-z0-9.-]+", conf["label"]):
        raise ValueError("invalid service label")
    (root / "config/installation.json").write_text(json.dumps(conf, indent=2))
    (root / "current").symlink_to(Path("releases") / build, target_is_directory=True)
    print(json.dumps({"installed":str(root), "component":component, "build_id":build}))

def service(args):
    root, conf = load(args.root)
    if sys.platform != "darwin":
        raise ValueError("this baseline service adapter is macOS only")
    target = f"gui/{os.getuid()}/{conf['label']}"
    record = root / "config/service.json"
    if args.command == "register":
        dest = args.service_file.expanduser().absolute()
        if dest.exists() or dest.is_symlink() or dest.parent.resolve() != dest.parent:
            raise ValueError("service file must be a new path in a resolved directory")
        existing = subprocess.run(["launchctl", "print", target], capture_output=True)
        if existing.returncode == 0:
            raise ValueError("service label already registered")
        data = {"Label":conf["label"], "ProgramArguments":[str(root / "launcher/mino-maintain"), "run", "--root", str(root)],
                "RunAtLoad":False, "KeepAlive":False,
                "WorkingDirectory":str(root), "EnvironmentVariables":{"MINO_NEXUS_DATA_DIR":conf["data_dir"]}}
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as stream:
            plistlib.dump(data, stream)
        record.write_text(json.dumps({"label":conf["label"], "plist":str(dest)}))
        print("service definition registered; use start to bootstrap")
    elif args.command == "start":
        saved = json.loads(record.read_text())
        if saved["label"] != conf["label"]:
            raise ValueError("service registration mismatch")
        probe = subprocess.run(["launchctl", "print", target], capture_output=True)
        if probe.returncode:
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", saved["plist"]], check=True)
        subprocess.run(["launchctl", "kickstart", target], check=True)
    elif args.command == "stop":
        subprocess.run(["launchctl", "bootout", target], check=True)
    elif args.command == "status":
        return subprocess.run(["launchctl", "print", target]).returncode
    return 0

def run(args):
    root, conf = load(args.root)
    release = (root / "current").resolve(strict=True)
    if release.parent != root / "releases":
        raise ValueError("current must resolve to a registered release")
    component = conf["component"]
    executable = release / ("mino-" + component)
    command = [str(executable), "--host", conf["host"], "--port", str(conf["port"])]
    env = dict(os.environ)
    if component == "nexus":
        env["MINO_NEXUS_DATA_DIR"] = conf["data_dir"]
        env["MINO_NEXUS_MDNS"] = "1" if conf["mdns"] else "0"
        command += ["--data-dir", conf["data_dir"]]
    else:
        command += ["--nexus-url", conf["nexus_url"], "--releases-root", str(root / "releases")]
    log = logging.getLogger("service")
    log.setLevel(logging.INFO)
    log.addHandler(RotatingFileHandler(root / "logs/service.log", maxBytes=5*1024*1024, backupCount=3))
    child = subprocess.Popen(command, cwd=release, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    def stop(signum, frame):
        if child.poll() is None:
            child.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        chunk = os.read(child.stdout.fileno(), 16384)
        if not chunk:
            break
        log.info(chunk.decode("utf-8", errors="replace").rstrip())
    return child.wait()

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "d2":
        from mino_nexus.core.deployment_cli import main as deployment_main
        return deployment_main(sys.argv[2:])
    ap = argparse.ArgumentParser(prog="mino-maintain")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install")
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--data-dir", type=Path)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--nexus-url", default="http://mino.local:50104")
    p.add_argument("--label")
    p.add_argument("--mdns", action="store_true")
    for name in ("register", "start", "stop", "status", "run"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, required=True)
        if name == "register":
            p.add_argument("--service-file", type=Path, required=True)
    args = ap.parse_args()
    if args.command == "install":
        install(args)
        return 0
    if args.command == "run":
        return run(args)
    return service(args)

if __name__ == "__main__":
    raise SystemExit(main())
