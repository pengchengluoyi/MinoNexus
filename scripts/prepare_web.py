"""Bundle a built Workbench directory into the wheel, without a release/lock prerequisite."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


def git_value(source: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(source), *args], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True, type=Path, help="Built Workbench directory")
    ap.add_argument("--version", help="Product version from the release tag; otherwise use VERSION")
    ap.add_argument("--source-commit", default="", help="Workbench commit when its .git is not available")
    ap.add_argument("--package-json", type=Path, help="Workbench package.json (defaults to dist parent)")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = args.dist.resolve()
    target = root / "mino_nexus/web"
    if not (source / "index.html").is_file():
        ap.error("Workbench build has no index.html; run npm run build first")
    if source == target.resolve() or source.is_relative_to(target.resolve()) or target.resolve().is_relative_to(source):
        ap.error("input and generated package directory must not overlap")
    if target.is_symlink():
        ap.error("generated web directory must not be a symlink")
    # Generated package data only: replace instead of retaining stale hashed assets.
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    package = args.package_json or source.parent / "package.json"
    metadata = json.loads(package.read_text()) if package.is_file() else {}
    commit = args.source_commit or git_value(source.parent, "rev-parse", "HEAD")
    web_version = metadata.get("version", "local")
    build = {"version": web_version, "build_id": f"workbench-{web_version}-{commit or 'local'}",
             "source_commit": commit}
    (target / "BUILD.json").write_text(json.dumps(build, indent=2) + "\n")
    version_file = root / "mino_nexus/VERSION"
    if args.version:
        version_file.write_text(args.version + "\n")
    version = version_file.read_text().strip()
    commit = os.environ.get("GITHUB_SHA") or git_value(root, "rev-parse", "HEAD")
    (root / "mino_nexus/BUILD.json").write_text(json.dumps({
        "build_id": f"nexus-{version}", "source_commit": commit,
        "dirty": bool(git_value(root, "status", "--porcelain")) if not os.environ.get("GITHUB_ACTIONS") else False,
    }, indent=2) + "\n")
    print(f"Bundled Workbench {web_version} into Nexus {version}")


if __name__ == "__main__":
    main()
