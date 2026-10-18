"""从真实发布清单生成跨机器安装命令。"""
from __future__ import annotations

import ipaddress
import os
import shlex
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from mino_nexus.core.mdns import bind_port, http_origin


def public_nexus_url(request: Request) -> str:
    configured = str(os.environ.get("MINO_NEXUS_PUBLIC_URL") or "").strip().rstrip("/")
    base = configured or str(request.base_url).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=503, detail="Nexus 对外地址无效，请检查 MINO_NEXUS_PUBLIC_URL")
    hostname = parsed.hostname.lower()
    local = hostname == "localhost" or hostname.endswith(".localhost")
    try:
        address = ipaddress.ip_address(hostname)
        local = local or address.is_loopback or address.is_unspecified
    except ValueError:
        pass
    if local:
        if configured:
            raise HTTPException(status_code=503, detail="Nexus 对外地址不能是 localhost，请配置目标电脑可访问的域名")
        return http_origin(bind_port())
    return base


def installation_release() -> dict:
    from mino_nexus.routers.rReleases import _fetch_manifest, _manifest_url, _norm_arch, _norm_os

    manifest_url = _manifest_url()
    manifest = _fetch_manifest(manifest_url)
    items = manifest.get("items")
    rows = items if isinstance(items, list) else [manifest]
    targets: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        system = _norm_os(row.get("os"))
        arch = _norm_arch(row.get("arch"))
        if system in ("darwin", "win32", "linux") and arch in ("arm64", "x64"):
            targets.setdefault(system, [])
            if arch not in targets[system]:
                targets[system].append(arch)
    if not targets:
        raise HTTPException(status_code=503, detail="Scout 尚无可用安装包，请等待发布后重试")
    return {"manifest_url": manifest_url, "targets": targets}


def install_commands(release: dict, *, token: str, nexus_url: str) -> dict[str, str]:
    manifest_url = release["manifest_url"]
    directory = manifest_url.rsplit("/", 1)[0]
    commands = {}
    for system in release["targets"]:
        if system == "win32":
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            script_url = quote(directory + "/bootstrap.ps1")
            commands[system] = (
                "$script = Join-Path ([IO.Path]::GetTempPath()) ('mino-scout-' + [guid]::NewGuid() + '.ps1'); "
                f"Invoke-WebRequest -UseBasicParsing -Uri {script_url} -OutFile $script; "
                f"& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Token {quote(token)} "
                f"-NexusUrl {quote(nexus_url)} -ManifestUrl {quote(manifest_url)}"
            )
        else:
            commands[system] = (
                f"curl -fsSL {shlex.quote(directory + '/bootstrap.sh')} | bash -s -- "
                f"--token {shlex.quote(token)} --nexus-url {shlex.quote(nexus_url)} "
                f"--manifest-url {shlex.quote(manifest_url)}"
            )
    return commands


def development_installation(token: str) -> dict:
    """Source development pairs locally; never invokes a production installer."""
    from pathlib import Path
    import platform
    import sys
    source = Path(__file__).resolve().parents[3] / "MinoScout"
    if not (source / "mino_scout/cli.py").is_file():
        raise HTTPException(status_code=503, detail="本机缺少同级 MinoScout 源码，不能生成开发接入命令")
    system = {"Darwin": "darwin", "Windows": "win32", "Linux": "linux"}.get(platform.system())
    if not system:
        raise HTTPException(status_code=503, detail="当前开发宿主不支持 Scout 接入")
    arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    interpreter = source / ".venv" / ("Scripts/python.exe" if system == "win32" else "bin/python")
    if not interpreter.is_file():
        raise HTTPException(status_code=503, detail="请先在 MinoScout/.venv 安装 Scout 开发依赖；不复用 Nexus 解释器")
    args = [str(interpreter), "-m", "mino_scout.cli", "configure-development",
            "--nexus-url", "http://127.0.0.1:50101", "--install-token", token]
    if system == "win32":
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        command = "Set-Location -LiteralPath " + quote(str(source)) + "; & " + " ".join(quote(x) for x in args)
    else:
        command = "cd " + shlex.quote(str(source)) + " && " + " ".join(shlex.quote(x) for x in args)
    return {"environment": "development", "nexus_url": "http://127.0.0.1:50101",
            "targets": {system: [arch]}, "commands": {system: command}}
