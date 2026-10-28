"""项目脚本工作目录：检出仓库，并准备只给业务脚本用的虚拟环境。"""
from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from mino_nexus.core.paths import data_dir

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def get_repo(project_id: str) -> dict[str, str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.project_script_repo import ProjectScriptRepo

    pid = str(project_id or "").strip()
    if not pid:
        return {"url": "", "branch": "", "updated_at": ""}
    with session_scope() as db:
        row = db.get(ProjectScriptRepo, pid)
        if row is None:
            return {"url": "", "branch": "", "updated_at": ""}
        return {"url": str(row.url or ""), "branch": str(row.branch or ""), "updated_at": str(row.updated_at or "")}


def save_repo(project_id: str, url: str, branch: str = "") -> dict[str, str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.project_script_repo import ProjectScriptRepo

    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("缺少 project_id")
    cleaned = str(url or "").strip()
    if cleaned and urlsplit(cleaned).scheme not in {"http", "https", "ssh", "git"}:
        raise ValueError("script_repo_url_invalid")
    updated = datetime.now(timezone.utc).isoformat()
    with session_scope() as db:
        row = db.get(ProjectScriptRepo, pid)
        if row is None:
            row = ProjectScriptRepo(project_id=pid)
            db.add(row)
        row.url = cleaned
        row.branch = str(branch or "").strip()
        row.updated_at = updated
        db.flush()
    return {"url": cleaned, "branch": str(branch or "").strip(), "updated_at": updated, "public_url": public_url(cleaned)}


def public_url(url: str) -> str:
    parts = urlsplit(str(url or ""))
    if not parts.username:
        return str(url or "")
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def safe_script_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("script_path_invalid")
    if path.suffix != ".py":
        raise ValueError("script_path_invalid")
    return path.as_posix()


def workspace_root(project_id: str) -> Path:
    return data_dir() / "case_workspaces" / str(project_id)


def workspace_location(project_id: str) -> dict[str, str]:
    """本机工作目录。root 是项目目录，checkout 是仓库检出，venv 是脚本环境。"""
    root = workspace_root(project_id)
    return {
        "root": str(root),
        "checkout": str(root / "repo"),
        "venv": str(root / "venv"),
    }


def ensure_workspace(project_id: str) -> dict[str, Any]:
    """地址已保存但本机还没有检出时就 clone。已有检出则不动，拉取留到执行。"""
    place = workspace_location(project_id)
    repo = get_repo(project_id)
    url = str(repo.get("url") or "").strip()
    result: dict[str, Any] = {**place, "ready": False, "error": ""}
    if not url:
        return result
    if (Path(place["checkout"]) / ".git").is_dir():
        result["ready"] = True
        return result
    try:
        sync_workspace(project_id, repo)
        result["ready"] = True
    except Exception as exc:
        result["error"] = str(exc)[:400]
    return result


def sync_workspace(project_id: str, repo: dict[str, str]) -> Path:
    """没有检出就 clone，已有则拉取。有 requirements.txt 就装进该项目的虚拟环境。"""
    url = str(repo.get("url") or "").strip()
    if not url:
        raise RuntimeError("项目还没有配置用例脚本仓库")
    lock = _lock(project_id)
    with lock:
        root = workspace_root(project_id)
        root.mkdir(parents=True, exist_ok=True)
        checkout = root / "repo"
        _sync_git(checkout, url, str(repo.get("branch") or "").strip())
        python = _ensure_venv(root / "venv")
        requirements = checkout / "requirements.txt"
        if requirements.is_file():
            _run([str(python), "-m", "pip", "install", "-r", str(requirements)], timeout=300)
        return checkout


def resolve_script(checkout: Path, case_id: str, platform: str = "", script_path: str = "") -> Path | None:
    explicit = str(script_path or "").strip()
    if explicit:
        path = (checkout / safe_script_path(explicit)).resolve()
        if checkout.resolve() not in path.parents:
            raise ValueError("script_path_invalid")
        return path if path.is_file() else None
    return case_script_file(checkout, case_id, platform)


def case_script_file(checkout: Path, case_id: str, platform: str = "") -> Path | None:
    folder = checkout / "cases"
    plat = str(platform or "").strip().lower()
    if plat:
        specific = folder / f"{case_id}.{plat}.py"
        if specific.is_file():
            return specific
    common = folder / f"{case_id}.py"
    return common if common.is_file() else None


def hook_file(checkout: Path, name: str) -> Path | None:
    path = checkout / f"{name}.py"
    return path if path.is_file() else None


def venv_python(project_id: str) -> Path:
    root = workspace_root(project_id) / "venv"
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def local_script_text(project_id: str, case_id: str, platform: str = "", script_path: str = "") -> str:
    checkout = workspace_root(project_id) / "repo"
    try:
        path = resolve_script(checkout, case_id, platform, script_path)
    except ValueError:
        return ""
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _lock(project_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[project_id] = lock
        return lock


def _sync_git(checkout: Path, url: str, branch: str) -> None:
    if not (checkout / ".git").is_dir():
        if checkout.exists():
            raise RuntimeError("脚本工作目录已存在，但不是 git 检出")
        command = ["git", "clone", "--depth", "1"]
        if branch:
            command.extend(["--branch", branch])
        command.extend([url, str(checkout)])
        _run(command, timeout=180)
        return
    _run(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin"], timeout=180)
    if _dirty(checkout):
        return
    target = f"origin/{branch}" if branch else "origin/HEAD"
    _run(["git", "-C", str(checkout), "reset", "--hard", target], timeout=60)


def _dirty(checkout: Path) -> bool:
    import subprocess
    result = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    return bool((result.stdout or "").strip())


def _ensure_venv(path: Path):
    python = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_file():
        return python
    import sys
    _run([sys.executable, "-m", "venv", str(path)], timeout=180)
    if not python.is_file():
        raise RuntimeError("虚拟环境没有生成 python")
    return python


def _run(command: list[str], *, timeout: int) -> None:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"准备脚本环境失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise RuntimeError(detail or f"命令退出 {proc.returncode}")
