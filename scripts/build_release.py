"""Build one native, self-contained baseline release; never publishes by itself."""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]

def command(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)

def build(args):
    component = "nexus" if (ROOT / "mino_nexus").is_dir() else "workbench"
    version = ((ROOT / "mino_nexus/VERSION").read_text().strip() if component == "nexus"
               else json.loads((ROOT / "package.json").read_text())["version"])
    commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip())
    if dirty and not args.allow_dirty:
        raise SystemExit("dirty source cannot produce a published release; use --allow-dirty for local verification")
    build_id = version + "-" + commit[:12] + ("-local-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S") if dirty else "")
    osn, arch = sys.platform, "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    out = args.output.expanduser().resolve()
    work = out / ("build-" + build_id)
    work.mkdir(parents=True, exist_ok=False)
    identity = {"component": component, "version":version, "build_id":build_id,
                "source_commit":commit, "dirty":dirty, "os":osn, "arch":arch}
    if component == "nexus":
        identity["maintenance_contract_version"] = 1
    env = dict(os.environ, MINO_NEXUS_DATA_DIR=str(work / "isolated-production-data"), MINO_NEXUS_DEV_DATA_DIR=str(work / "isolated-data"), MINO_NEXUS_MDNS="0",
               PYINSTALLER_CONFIG_DIR=str(work / "pyinstaller-cache"), PYTHONDONTWRITEBYTECODE="1")
    source = work / "source"
    source.mkdir()
    if component == "nexus":
        shutil.copytree(ROOT / "mino_nexus", source / "mino_nexus", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (source / "mino_nexus/BUILD.json").write_text(json.dumps(identity))
        entry = work / "mino-entry.py"
        entry.write_text("from mino_nexus.cli import main\nraise SystemExit(main())\n")
        extra = ["--paths", str(source), "--collect-submodules", "mino_nexus",
                 "--add-data", str(source / "mino_nexus") + os.pathsep + "mino_nexus",
                 "--collect-all", "litellm", "--collect-all", "tiktoken_ext", "--collect-all", "lark_oapi",
                 "--collect-all", "zeroconf", "--collect-all", "sqlalchemy",
                 "--copy-metadata", "litellm", "--copy-metadata", "uvicorn"]
        env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    else:
        web = work / "web"
        command([*(["cmd", "/c", "npm"] if os.name == "nt" else ["npm"]), "run", "build", "--", "--base", "./", "--outDir", str(web)], cwd=ROOT, env=env)
        (web / "BUILD.json").write_text(json.dumps(identity))
        entry = ROOT / "packaging/workbench_server.py"
        extra = ["--add-data", str(web) + os.pathsep + "web"]
    base = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--console", "--python-option", "X utf8",
            "--distpath", str(work / "dist"), "--workpath", str(work / "freeze"),
            "--specpath", str(work)]
    command([*base, "--name", "mino-" + component, *extra, str(entry)], cwd=source, env=env)
    helper_options = (["--paths", str(source), "--collect-submodules", "mino_nexus.core"]
                      if component == "nexus" else [])
    command([*base, "--name", "mino-maintain", *helper_options, str(ROOT / "packaging/maintain.py")], cwd=source, env=env)
    if component == "nexus":
        command([str(work / "dist/mino-maintain" / ("mino-maintain.exe" if osn == "win32" else "mino-maintain")), "d2", "--help"], env=env)
    executable = work / "dist" / ("mino-" + component) / ("mino-" + component + (".exe" if osn == "win32" else ""))
    command([str(executable), "--help"], env=env)
    if component == "nexus":
        command([str(executable), "migrate", "--data-dir", str(work / "package-probe-data")], env=env)
    else:
        command([str(executable), "--identity"], env=env)
    installer_name = "MinoInstaller-" + osn + "-" + arch
    installer = out / (installer_name + (".exe" if osn == "win32" else ""))
    command([sys.executable, "-m", "PyInstaller", "--noconfirm", "--console", "--python-option", "X utf8", "--onefile",
             "--name", installer_name, "--distpath", str(out), "--workpath", str(work / "installer-freeze"),
             "--specpath", str(work), str(ROOT / "packaging/install.py")], cwd=source, env=env)
    command([str(installer), "--help"], env=env)
    (out / ("installer-" + osn + "-" + arch + ".sha256")).write_text(hashlib.sha256(installer.read_bytes()).hexdigest() + "  " + installer.name + "\n")
    archive = out / ("Mino" + component.capitalize() + "-" + build_id + "-" + osn + "-" + arch + ".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("release.json", json.dumps(identity))
        for folder, prefix in ((executable.parent, "app"), (work / "dist/mino-maintain", "launcher")):
            # Materialize framework links as ordinary entries for a portable,
            # path-safe installer. Do not include build data or credentials.
            for current, dirs, files in os.walk(folder, followlinks=True):
                for name in files:
                    path = Path(current) / name
                    z.write(path, prefix + "/" + path.relative_to(folder).as_posix())
    item = {"os":osn, "arch":arch, "filename":archive.name, "sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),
            "bytes":archive.stat().st_size, "payload":"binary", "installer":"baseline-v1", **identity}
    (out / ("item-" + osn + "-" + arch + ".json")).write_text(json.dumps(item, indent=2))
    print(json.dumps(item))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--allow-dirty", action="store_true")
    build(ap.parse_args())
