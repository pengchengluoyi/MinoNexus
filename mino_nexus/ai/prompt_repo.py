"""从 mino_nexus/prompts/ 加载 prompt。文件 + git 是唯一真源。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

import yaml

_LOCK = threading.RLock()
_STATE: "Repo | None" = None
_MTIME = 0.0


class PromptRepoError(RuntimeError):
    pass


class Repo:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}
        self.roles: list[dict[str, Any]] = []
        self.stack: dict[str, Any] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.cards: dict[str, dict[str, Any]] = {}
        self.fingerprints: dict[tuple[str, str], tuple[int, str]] = {}
        self.paths: dict[tuple[str, str], str] = {}


def prompts_dir() -> Path:
    """开发树和 wheel 都从包内的 prompts/ 读。"""
    located = Path(__file__).resolve().parent.parent / "prompts"
    if located.is_dir():
        return located
    from importlib.resources import files

    resource = files("mino_nexus").joinpath("prompts")
    candidate = Path(str(resource))
    if candidate.is_dir():
        return candidate
    raise PromptRepoError(f"找不到 prompts 目录：{located}")


def _reload_enabled() -> bool:
    return os.environ.get("MINO_PROMPTS_RELOAD") == "1"


def load_yaml(path: Path) -> Any:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if data is not None else {}


def content_sha256(paths: list[Path], *, root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(paths, key=lambda p: p.relative_to(root).as_posix())
    for path in files:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _entry_files(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file() and p.name != ".gitkeep"]


def _max_mtime(root: Path) -> float:
    latest = 0.0
    for path in root.rglob("*"):
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    return latest


def expand_blocks(base: Path, blocks: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in blocks or []:
        if not isinstance(raw, dict):
            continue
        block = dict(raw)
        rel = block.pop("file", None)
        if rel:
            path = base / str(rel)
            if not path.is_file():
                raise PromptRepoError(f"块引用的文件不存在：{path}")
            block["text"] = path.read_text(encoding="utf-8")
        out.append(block)
    return out


def job_to_public(spec: dict[str, Any], blocks_base: Path) -> dict[str, Any]:
    role = spec.get("role", spec.get("role_id", ""))
    version = spec.get("version", spec.get("prompt_version", 1))
    return {
        "id": str(spec.get("id") or ""),
        "label": str(spec.get("label") or spec.get("id") or ""),
        "summary": str(spec.get("summary") or ""),
        "engine": str(spec.get("engine") or "text_chat"),
        "role_id": str(role or ""),
        "output_schema": str(spec.get("output_schema") or ""),
        "slots": list(spec.get("slots") or []),
        "flags": list(spec.get("flags") or []),
        "system_blocks": expand_blocks(blocks_base, spec.get("system")),
        "user_blocks": expand_blocks(blocks_base, spec.get("user")),
        "image": dict(spec.get("image") or {}),
        "call": dict(spec.get("call") or {}),
        "enabled": spec.get("enabled", True) is not False,
        "builtin": False,
        "sort_order": int(spec.get("sort_order") or 0),
        "seed_rev": "",
        "prompt_version": int(version or 1),
        "overrides_json": {},
    }


def _slot_names(row: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for slot in row.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        key = str(slot.get("name") or slot.get("id") or "").strip()
        if key:
            out.add(key)
    return out


def _validate_job(row: dict[str, Any]) -> None:
    slots = _slot_names(row)
    for side in ("system_blocks", "user_blocks"):
        for block in row.get(side) or []:
            if not isinstance(block, dict):
                continue
            if str(block.get("when") or "").strip():
                raise PromptRepoError(f"{row.get('id')} 仍含 when 条件块")
            slot = block.get("slot")
            if slot and str(slot) not in slots:
                raise PromptRepoError(f"{row.get('id')} 块引用了未声明槽：{slot}")
            try:
                cap = int(block.get("max_chars") or 0)
            except (TypeError, ValueError):
                cap = 0
            if cap and not (1 <= cap <= 20000):
                raise PromptRepoError(f"{row.get('id')} max_chars 须在 1..20000")
    if not any(str(b.get("text") or b.get("slot") or "").strip() for b in (row.get("system_blocks") or [])):
        raise PromptRepoError(f"{row.get('id')} system 块不能全空")


def _fake_slots(row: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in row.get("slots") or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or spec.get("id") or "")
        kind = str(spec.get("kind") or "text")
        if kind == "json":
            out[name] = "{}"
        elif kind == "image":
            out[name] = "aaaa"
        else:
            out[name] = "X"
    return out


def _smoke_render(row: dict[str, Any]) -> None:
    from mino_nexus.ai.prompt_render import JobRenderError, render_job

    try:
        render_job(row, _fake_slots(row))
    except JobRenderError as exc:
        raise PromptRepoError(str(exc)) from exc


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(__file__).resolve().parent.parent.parent).as_posix()
    except ValueError:
        return path.as_posix()


def _require(spec: dict[str, Any], keys: tuple[str, ...], *, where: str) -> None:
    missing = [key for key in keys if spec.get(key) in (None, "")]
    if missing:
        raise PromptRepoError(f"{where} 缺少字段：{', '.join(missing)}")


def _scan(root: Path) -> Repo:
    repo = Repo()
    jobs_dir = root / "jobs"
    if jobs_dir.is_dir():
        for directory in sorted(p for p in jobs_dir.iterdir() if p.is_dir()):
            spec_path = directory / "job.yaml"
            if not spec_path.is_file():
                raise PromptRepoError(f"{directory.name} 缺少 job.yaml")
            spec = load_yaml(spec_path)
            if not isinstance(spec, dict):
                raise PromptRepoError(f"{spec_path} 不是映射")
            _require(spec, ("id", "version", "engine"), where=str(spec_path))
            if spec["id"] != directory.name:
                raise PromptRepoError(f"{spec_path} 的 id 与目录名不一致")
            row = job_to_public(spec, directory)
            _validate_job(row)
            if row["enabled"]:
                _smoke_render(row)
            digest = content_sha256(_entry_files(directory), root=directory)
            key = f"jobs/{row['id']}"
            row["prompt_sha"] = digest
            row["source_path"] = _rel(spec_path)
            repo.jobs[row["id"]] = row
            repo.fingerprints[("job", row["id"])] = (int(spec["version"]), digest)
            repo.paths[("job", row["id"])] = row["source_path"]

    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for path in sorted(skills_dir.glob("*.yaml")):
            spec = load_yaml(path)
            if not isinstance(spec, dict):
                raise PromptRepoError(f"{path} 不是映射")
            _require(spec, ("id", "version"), where=str(path))
            digest = content_sha256([path], root=skills_dir)
            key = f"skills/{spec['id']}"
            repo.skills[str(spec["id"])] = spec
            repo.fingerprints[("skill", str(spec["id"]))] = (int(spec["version"]), digest)
            repo.paths[("skill", str(spec["id"]))] = _rel(path)

    roles_dir = root / "roles"
    if roles_dir.is_dir():
        for path in sorted(roles_dir.glob("*.yaml")):
            spec = load_yaml(path)
            if not isinstance(spec, dict):
                raise PromptRepoError(f"{path} 不是映射")
            _require(spec, ("id", "version", "label"), where=str(path))
            digest = content_sha256([path], root=roles_dir)
            key = f"roles/{spec['id']}"
            spec = dict(spec)
            spec["_source_path"] = _rel(path)
            spec["_sha"] = digest
            repo.roles.append(spec)
            repo.fingerprints[("role", str(spec["id"]))] = (int(spec["version"]), digest)
            repo.paths[("role", str(spec["id"]))] = spec["_source_path"]
        repo.roles.sort(key=lambda row: (int(row.get("sort_order") or 0), str(row.get("id") or "")))

    stack_path = root / "stack" / "stack.yaml"
    if stack_path.is_file():
        spec = load_yaml(stack_path)
        if not isinstance(spec, dict):
            raise PromptRepoError("stack.yaml 不是映射")
        digest = content_sha256([stack_path], root=stack_path.parent)
        spec = dict(spec)
        spec["_source_path"] = _rel(stack_path)
        spec["_sha"] = digest
        repo.stack = spec
        repo.fingerprints[("stack", "stack")] = (int(spec.get("version") or 1), digest)
        repo.paths[("stack", "stack")] = spec["_source_path"]

    _scan_assistant(root, repo)
    _check_refs(repo)
    return repo


def _scan_assistant(root: Path, repo: Repo) -> None:
    base = root / "assistant"
    jobs = base / "jobs"
    if jobs.is_dir():
        for directory in sorted(p for p in jobs.iterdir() if p.is_dir()):
            spec_path = directory / "job.yaml"
            if not spec_path.is_file():
                continue
            spec = load_yaml(spec_path)
            row = job_to_public(spec, directory)
            _validate_job(row)
            if row["enabled"]:
                _smoke_render(row)
            digest = content_sha256(_entry_files(directory), root=directory)
            key = f"assistant/jobs/{row['id']}"
            row["prompt_sha"] = digest
            row["source_path"] = _rel(spec_path)
            repo.jobs[row["id"]] = row
            repo.fingerprints[("job", row["id"])] = (int(spec.get("version") or 1), digest)
            repo.paths[("job", row["id"])] = row["source_path"]
    for kind, folder in (("assistant_tool", base / "tools"), ("card", base / "cards")):
        if not folder.is_dir():
            continue
        bucket = repo.tools if kind == "assistant_tool" else repo.cards
        for path in sorted(folder.glob("*.yaml")):
            spec = load_yaml(path)
            if not isinstance(spec, dict):
                raise PromptRepoError(f"{path} 不是映射")
            _require(spec, ("id", "version"), where=str(path))
            digest = content_sha256([path], root=folder)
            key = f"assistant/{folder.name}/{spec['id']}"
            spec = dict(spec)
            spec["source_path"] = _rel(path)
            spec["sha256"] = digest
            bucket[str(spec["id"])] = spec
            repo.fingerprints[(kind, str(spec["id"]))] = (int(spec["version"]), digest)
            repo.paths[(kind, str(spec["id"]))] = spec["source_path"]


def _known_skills(repo: Repo) -> set[str]:
    names = {str(row.get("id") or "") for row in (repo.stack.get("skills") or []) if isinstance(row, dict)}
    names.update(repo.skills)
    return names


def _check_refs(repo: Repo) -> None:
    role_ids = {str(row.get("id") or "") for row in repo.roles}
    for spec in repo.skills.values():
        role = str(spec.get("role") or "")
        if role and role not in role_ids:
            raise PromptRepoError(f"技能 {spec.get('id')} 引用了不存在的角色 {role}")
    job_ids = set(repo.jobs)
    for row in repo.roles:
        workflow = str(row.get("job") or "")
        for jid in row.get("jobs") or []:
            if jid == workflow or jid in job_ids:
                continue
            raise PromptRepoError(f"角色 {row.get('id')} 引用了不存在的 job {jid}")
    known = _known_skills(repo)
    drivers = repo.stack.get("skill_drivers") or {}
    if isinstance(drivers, dict):
        for sid in drivers:
            if sid not in known:
                raise PromptRepoError(f"编排 skill_drivers 引用了不存在的技能 {sid}")
    role_skills = repo.stack.get("role_skills") or {}
    if isinstance(role_skills, dict):
        for _rid, ids in role_skills.items():
            for item in ids or []:
                if item not in known:
                    raise PromptRepoError(f"编排 role_skills 引用了不存在的技能 {item}")


def load_all() -> Repo:
    global _STATE, _MTIME
    root = prompts_dir()
    with _LOCK:
        repo = _scan(root)
        _STATE = repo
        _MTIME = _max_mtime(root)
        return repo


def _repo() -> Repo:
    global _STATE, _MTIME
    with _LOCK:
        root = prompts_dir()
        if _STATE is None:
            _STATE = _scan(root)
            _MTIME = _max_mtime(root)
            return _STATE
        if _reload_enabled():
            latest = _max_mtime(root)
            if latest != _MTIME:
                _STATE = _scan(root)
                _MTIME = latest
        return _STATE


def get_job(job_id: str) -> dict[str, Any] | None:
    jid = str(job_id or "").strip()
    if not jid:
        return None
    row = _repo().jobs.get(jid)
    return dict(row) if row else None


def list_jobs() -> list[dict[str, Any]]:
    rows = list(_repo().jobs.values())
    rows.sort(key=lambda row: (int(row.get("sort_order") or 0), str(row.get("id") or "")))
    return [dict(row) for row in rows]


def _skill_public(spec: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.ai.prompt_render import _render_side

    sid = str(spec.get("id") or "")
    role_id = str(spec.get("role") or "")
    role_label = str(spec.get("role_label") or role_id)
    view = dict(spec.get("view") or {})
    mapped = _ROLE_JOB_ALIASES.get(sid) or sid
    job = _repo().jobs.get(mapped) or _repo().jobs.get(sid)
    prompt = ""
    if job:
        prompt = _render_side(list(job.get("system_blocks") or []), {})
    version, digest = fingerprint("skill", sid)
    return {
        "id": sid,
        "label": str(spec.get("label") or sid),
        "summary": str(spec.get("summary") or ""),
        "category": str(spec.get("category") or "flow"),
        "role": {"id": role_id, "label": role_label},
        "role_id": role_id,
        "triggers": list(spec.get("triggers") or []),
        "engine": str(spec.get("engine") or "chat"),
        "system_prompt": prompt,
        "prompt_chars": len(prompt),
        "sop": dict(spec.get("sop") or {}),
        "input": dict(spec.get("input") or {}),
        "view": view,
        "view_id": str(view.get("id") or "job-timeline"),
        "enabled": spec.get("enabled", True) is not False,
        "builtin": False,
        "sort_order": int(spec.get("sort_order") or 0),
        "editable": False,
        "prompt_custom": False,
        "version": version,
        "sha": digest,
        "source_path": source_path("skill", sid),
    }


_ROLE_JOB_ALIASES: dict[str, str] = {
    "req-analyst": "analyze_req",
    "mindmap-writer": "draft_mindmap",
    "case-writer": "draft_cases",
    "test-engineer": "test-engineer-chat",
    "im-qa-assistant": "im-dialogue",
    "im-defect-assistant": "im-defect",
    "run-case": "agent-decide",
}


def get_skill(skill_id: str) -> dict[str, Any] | None:
    sid = str(skill_id or "").strip()
    if not sid:
        return None
    spec = _repo().skills.get(sid)
    if spec is None:
        return None
    return _skill_public(spec)


def list_skills() -> list[dict[str, Any]]:
    rows = [_skill_public(spec) for spec in _repo().skills.values()]
    rows.sort(key=lambda row: (int(row.get("sort_order") or 0), str(row.get("id") or "")))
    return rows


def role_specs() -> list[dict[str, Any]]:
    return [dict(row) for row in _repo().roles]


def stack_spec() -> dict[str, Any]:
    return dict(_repo().stack)


def list_assistant_tools() -> list[dict[str, Any]]:
    return [dict(row) for row in _repo().tools.values()]


def get_card_template(template_id: str) -> dict[str, Any] | None:
    row = _repo().cards.get(str(template_id or "").strip())
    return dict(row) if row else None


def fingerprint(kind: str, ident: str) -> tuple[int, str]:
    return _repo().fingerprints.get((kind, ident), (0, ""))


def source_path(kind: str, ident: str) -> str:
    return _repo().paths.get((kind, ident), "")
