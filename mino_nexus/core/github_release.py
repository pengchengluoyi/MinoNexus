"""Public GitHub release discovery and pinned downloads; never installs code."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import ssl
import certifi
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAX_PACKAGE_BYTES = 4 * 1024**3


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", value):
        raise ValueError("Invalid release identifier")
    return value


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("Invalid configured GitHub repository")
    return value


def open_release(request, timeout):
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def read_remote(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "Mino-release-updater/1", "Accept": "application/json"})
    try:
        with open_release(request, timeout=30) as response:
            if urlsplit(response.url).scheme != "https":
                raise ValueError("Release redirect must use HTTPS")
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("GitHub release or manifest is not published") from None
        if exc.code in {403, 429}:
            raise ValueError("GitHub request denied or rate limited; retry later") from None
        raise ValueError(f"GitHub request failed: HTTP {exc.code}") from None
    if len(data) > limit:
        raise ValueError("Release metadata exceeds size limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Release metadata must be an object")
    return value


def discover(repo, component, tag=""):
    repo = repository(repo)
    if tag:
        identifier(tag)
        if tag.lower() == "latest":
            raise ValueError("Omit tag to discover latest; execution requires a fixed tag")
    endpoint = "tags/" + tag if tag else "latest"
    release = read_remote(f"https://api.github.com/repos/{repo}/releases/{endpoint}", 2 * 1024**2)
    fixed = identifier(release.get("tag_name"))
    if release.get("draft") or release.get("prerelease") or (tag and fixed != tag):
        raise ValueError("Expected a published stable release with the requested tag")
    base = f"https://github.com/{repo}/releases/download/{fixed}/"
    doc = read_remote(base + "manifest.json", 1024**2)
    if doc.get("manifest_schema_version") != 1 or doc.get("component") != component:
        raise ValueError("Manifest schema/component mismatch")
    identifier(doc.get("build_id"))
    if not re.fullmatch(r"[0-9a-f]{40}", str(doc.get("source_commit", ""))):
        raise ValueError("Manifest source commit is invalid")
    version = doc.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version) or fixed != "v" + version:
        raise ValueError("Stable tag/version mismatch")
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(platform.machine(), platform.machine())
    items = doc.get("items")
    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
        raise ValueError("Manifest items must be objects")
    matches = [x for x in items if (x.get("os"), x.get("arch")) == (sys.platform, arch)]
    if len(matches) != 1:
        raise ValueError("Release must contain exactly one package for this OS/architecture")
    item = matches[0]
    if any(item.get(k) != doc.get(k) for k in ("component", "build_id", "source_commit", "version")):
        raise ValueError("Manifest item identity mismatch")
    if item.get("dirty") is not False or item.get("payload") not in ({"binary", "static"} if component == "workbench" else {"binary"}):
        raise ValueError("Only clean supported release packages may be downloaded")
    filename = item.get("filename", "")
    if not isinstance(filename, str) or not re.fullmatch(r"hi3d-[A-Za-z0-9._-]+\.zip", filename):
        raise ValueError("Unsafe package filename")
    if item.get("url") != base + filename:
        raise ValueError("Package URL must belong to the selected repository and fixed release")
    if type(item.get("bytes")) is not int or not 0 < item["bytes"] <= MAX_PACKAGE_BYTES:
        raise ValueError("Invalid package size")
    if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
        raise ValueError("Invalid package SHA256")
    return fixed, doc, item


def check(repo, component, current, tag=""):
    fixed, doc, item = discover(repo, component, tag)
    def semver(value):
        return tuple(map(int, value.split("."))) if re.fullmatch(r"\d+\.\d+\.\d+", value or "") else None
    old, new = semver(current.get("version")), semver(doc["version"])
    return {"repository": repo, "release_tag": fixed, "current_build_id": current["build_id"],
            "current_version": current.get("version"), "target_build_id": doc["build_id"],
            "target_version": doc["version"], "update_available": old is not None and new > old,
            "same_build": current["build_id"] == doc["build_id"], "item": item,
            "package_verified": False, "compatibility_verified": False}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(root: Path, repo, component, tag, build):
    """Caller holds its package lock. Check is read-only; this only stages bytes."""
    if not tag or tag.lower() == "latest":
        raise ValueError("Download requires the fixed release_tag returned by check")
    identifier(build)
    fixed, doc, item = discover(repo, component, tag)
    if doc["build_id"] != build:
        raise ValueError("Release changed since check or build does not match the pinned tag")
    parent = root / "updates/prepared"
    parent.mkdir(parents=True, exist_ok=True)
    if parent.resolve() != parent or parent.is_symlink():
        raise ValueError("Prepared directory escapes installation")
    target = parent / build
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            raise ValueError("Prepared release may not be a symlink")
        manifest, package = target / "hi3d-manifest.json", target / "hi3d-package.zip"
        for path in (manifest, package):
            if path.is_symlink() or path.stat().st_nlink != 1:
                raise ValueError("Prepared files may not be linked")
        if json.loads(manifest.read_text()) != doc or package.stat().st_size != item["bytes"] or digest(package) != item["sha256"]:
            raise ValueError("Prepared build differs; preserved for diagnosis")
        return doc, package
    temporary = Path(tempfile.mkdtemp(prefix=".download-", dir=parent))
    package = temporary / "hi3d-package.zip"
    request = urllib.request.Request(item["url"], headers={"User-Agent": "Mino-release-updater/1"})
    with open_release(request, timeout=60) as response, package.open("xb") as out:
        if urlsplit(response.url).scheme != "https":
            raise ValueError("Package redirect must use HTTPS")
        total = 0
        while block := response.read(1024**2):
            total += len(block)
            if total > item["bytes"]:
                raise ValueError("Download exceeds declared size")
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    if total != item["bytes"] or digest(package) != item["sha256"]:
        raise ValueError("Package size/SHA256 mismatch; partial download preserved")
    with (temporary / "hi3d-manifest.json").open("x") as stream:
        json.dump(doc, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.rename(target)
    return doc, target / package.name
