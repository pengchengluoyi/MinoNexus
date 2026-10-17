"""Fixed-build release validation and bounded extraction (no application imports)."""
from __future__ import annotations
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

from mino_nexus.core.deployment import Deployment, atomic_json, file_digest, identifier, read_json

MAX_BYTES = 4 * 1024**3


def host_platform():
    return sys.platform, {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(platform.machine(), platform.machine())


def validate_manifest(doc: dict, *, build: str) -> dict:
    if doc.get("manifest_schema_version") != 1 or doc.get("component") != "nexus":
        raise ValueError("Unsupported Nexus manifest schema")
    if doc.get("build_id") != identifier(build) or not doc.get("source_commit"):
        raise ValueError("Build/source commit mismatch")
    matches = [x for x in doc.get("items", []) if (x.get("os"), x.get("arch")) == host_platform()]
    if len(matches) != 1:
        raise ValueError("Manifest requires exactly one package for this OS/architecture")
    item = matches[0]
    import re
    if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
        raise ValueError("Missing package SHA256")
    if type(item.get("bytes")) is not int or not 0 < item["bytes"] <= MAX_BYTES:
        raise ValueError("Invalid package size")
    return item


def fetch_fixed(d: Deployment, tag: str) -> tuple[dict, Path]:
    """Repository is installation configuration, never an arbitrary request URL."""
    tag = identifier(tag)
    repo = d.config["release_repository"]
    import re
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid configured repository")
    base = f"https://github.com/{repo}/releases/download/{tag}/"
    with urllib.request.urlopen(base + "manifest.json", timeout=30) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Manifest too large")
    doc = json.loads(raw)
    item = validate_manifest(doc, build=doc["build_id"])
    url = item.get("url", "")
    if not url.startswith(base) or urllib.parse.urlparse(url).query or '/' in url[len(base):]:
        raise ValueError("Package must belong to the fixed configured GitHub release")
    stage = d.root / "updates" / ("stage-" + tag)
    stage.mkdir(parents=True, exist_ok=True)
    package = stage / "hi3d-package.zip"
    if package.exists():
        if package.stat().st_size == item["bytes"] and file_digest(package) == item["sha256"]:
            return doc, package
        raise ValueError("Existing staged package differs; preserve it for diagnosis")
    partial = stage / (".download-" + os.urandom(6).hex())
    try:
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("xb") as out:
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > item["bytes"]:
                    raise ValueError("Download exceeds declared size")
                out.write(block)
        if total != item["bytes"] or file_digest(partial) != item["sha256"]:
            raise ValueError("Package size/SHA256 mismatch")
        os.replace(partial, package)
    except Exception:
        # Preserve uncertain/failed downloads; never overwrite them on retry.
        raise
    atomic_json(stage / "hi3d-manifest.json", doc)
    return doc, package


def stage_release(d: Deployment, manifest: dict, package: Path) -> Path:
    build = identifier(manifest["build_id"])
    item = validate_manifest(manifest, build=build)
    if package.stat().st_size != item["bytes"] or file_digest(package) != item["sha256"]:
        raise ValueError("Package size/SHA256 mismatch")
    dest = d.release(build)
    if dest.exists():
        saved = read_json(dest / "hi3d-release.json")
        if saved.get("package_sha256") != item["sha256"]:
            raise ValueError("Immutable build already exists with different bytes")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".release-", dir=dest.parent))
    with zipfile.ZipFile(package) as z:
        total = 0
        seen = set()
        for info in z.infolist():
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (path.is_absolute() or not path.parts or any(x in ("..", "") for x in path.parts)
                    or "\\" in info.filename or ":" in path.parts[0]
                    or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                raise ValueError("Unsafe archive member")
            if info.filename in seen:
                raise ValueError("Duplicate archive member")
            seen.add(info.filename)
            total += info.file_size
            if total > MAX_BYTES * 3 or len(seen) > 200000:
                raise ValueError("Archive expansion limit exceeded")
            target = temp.joinpath(*path.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("xb") as out:
                    shutil.copyfileobj(src, out)
                target.chmod(0o755 if mode & 0o111 else 0o644)
    for name in ("app/mino-nexus", "launcher/mino-maintain", "release.json"):
        if not (temp / name).is_file():
            raise ValueError(f"Incomplete release: {name}")
    metadata = read_json(temp / "release.json")
    if metadata.get("maintenance_contract_version") != 1:
        raise ValueError("Release does not support Nexus maintenance contract v1")
    if metadata.get("build_id") != build or metadata.get("source_commit") != manifest["source_commit"]:
        raise ValueError("Package build metadata mismatch")
    if (metadata.get("os"), metadata.get("arch")) != host_platform():
        raise ValueError("Package platform mismatch")
    atomic_json(temp / "hi3d-release.json", {**manifest, "package_sha256": item["sha256"]})
    # Not replace: an existing immutable build must never be overwritten.
    os.rename(temp, dest)
    return dest
