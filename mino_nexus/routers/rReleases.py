"""Scout 安装包：可选代理 GitHub manifest，本机不存二进制。"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Query

from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session

router = APIRouter(prefix="/releases", tags=["Releases"])

_FETCH_TIMEOUT = 15


def _guess_installer(filename: str, os_name: str) -> str:
    lower = filename.lower()
    for ext in ("pkg", "dmg", "msi", "exe", "zip"):
        if lower.endswith("." + ext):
            return ext
    if os_name == "darwin":
        return "zip"
    if os_name == "win32":
        return "zip"
    return "zip"


def _norm_os(value: str) -> str:
    s = str(value or "").strip().lower()
    if s in ("darwin", "macos", "mac"):
        return "darwin"
    if s in ("win32", "windows", "win"):
        return "win32"
    if s == "linux":
        return "linux"
    return s


def _norm_arch(value: str) -> str:
    s = str(value or "").strip().lower()
    if s in ("arm64", "aarch64"):
        return "arm64"
    if s in ("x64", "amd64", "x86_64"):
        return "x64"
    return s


def _basename(url: str) -> str:
    return str(url or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _row(item: dict, version: str, os_name: str) -> dict:
    filename = str(item.get("filename") or _basename(str(item.get("url") or "")))
    return {
        "version": str(item.get("version") or version or ""),
        "url": item.get("url"),
        "sha256": str(item.get("sha256") or ""),
        "installer": str(item.get("installer") or _guess_installer(filename, os_name)),
        "filename": filename,
    }


def pick_item(manifest: dict, os_name: str, arch_name: str) -> dict | None:
    """Accept items[] , a single-entry object, or a legacy {os:{arch:{}}} map."""
    if not isinstance(manifest, dict):
        return None
    want_os = _norm_os(os_name)
    want_arch = _norm_arch(arch_name)
    version = str(manifest.get("version") or "")

    items = manifest.get("items")
    if isinstance(items, list):
        matched = [r for r in items if isinstance(r, dict) and r.get("url")]
        hit = next((r for r in matched if _norm_os(r.get("os")) == want_os and _norm_arch(r.get("arch")) == want_arch), None)
        if hit is None:
            hit = next((r for r in matched if _norm_os(r.get("os")) == want_os), None)
        return _row(hit, version, want_os) if hit else None

    if manifest.get("url"):
        if manifest.get("os") and _norm_os(manifest.get("os")) != want_os:
            return None
        if manifest.get("arch") and _norm_arch(manifest.get("arch")) != want_arch:
            return None
        return _row(manifest, version, want_os)

    nested_os = manifest.get(want_os)
    if isinstance(nested_os, dict):
        nested = nested_os.get(want_arch)
        if isinstance(nested, dict) and nested.get("url"):
            return _row(nested, str(nested.get("version") or version), want_os)
    return None


def _fetch_manifest(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MinoNexus-scout-manifest",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Scout manifest HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Scout manifest unreachable: {exc.reason}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Scout manifest 无法解析: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Scout manifest 不是对象")
    return data


@router.get("/scout/latest")
def scout_latest(
    os_query: str = Query("", alias="os", description="darwin|win32|linux"),
    arch: str = Query("", description="arm64|x64"),
    _sess: dict = Depends(current_session),
):
    """Thin proxy of MINO_SCOUT_MANIFEST_URL. No local blobs, no data-dir hosting."""
    manifest_url = str(os.environ.get("MINO_SCOUT_MANIFEST_URL") or "").strip()
    if not manifest_url:
        raise HTTPException(
            status_code=404,
            detail="尚未配置 MINO_SCOUT_MANIFEST_URL。安装包在 Scout GitHub Release，本机不保存二进制。",
        )
    os_name = _norm_os(os_query) or "darwin"
    arch_name = _norm_arch(arch) or "arm64"
    row = pick_item(_fetch_manifest(manifest_url), os_name, arch_name)
    if not row or not row.get("url"):
        raise HTTPException(status_code=404, detail="manifest 里没有当前 os/arch 的 Scout 安装包")
    return ok(row)
