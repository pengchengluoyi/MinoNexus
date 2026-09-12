"""llm_jobs 表：读写、保存校验、启动烟测。"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from mino_nexus.ai.prompt_render import JobRenderError, render_job


def _public(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "label": row.label or row.id,
        "summary": row.summary or "",
        "engine": row.engine or "text_chat",
        "role_id": row.role_id or "",
        "output_schema": row.output_schema or "",
        "slots": list(row.slots_json or []),
        "flags": list(row.flags_json or []),
        "system_blocks": list(row.system_blocks_json or []),
        "user_blocks": list(row.user_blocks_json or []),
        "image": dict(row.image_json or {}),
        "call": dict(row.call_json or {}),
        "enabled": bool(row.enabled),
        "builtin": bool(row.builtin),
        "sort_order": int(row.sort_order or 0),
        "seed_rev": str(row.seed_rev or ""),
        "prompt_version": int(row.prompt_version or 1),
        "overrides_json": dict(row.overrides_json or {}),
    }


def _to_row(spec: dict[str, Any]):
    from mino_nexus.models.llm_job import LlmJob

    return LlmJob(
        id=str(spec.get("id") or "").strip(),
        label=str(spec.get("label") or spec.get("id") or ""),
        summary=str(spec.get("summary") or ""),
        engine=str(spec.get("engine") or "text_chat"),
        role_id=str(spec.get("role_id") or ""),
        output_schema=str(spec.get("output_schema") or ""),
        slots_json=list(spec.get("slots") or []),
        flags_json=list(spec.get("flags") or []),
        system_blocks_json=list(spec.get("system_blocks") or []),
        user_blocks_json=list(spec.get("user_blocks") or []),
        image_json=dict(spec.get("image") or {}),
        call_json=dict(spec.get("call") or {}),
        enabled=spec.get("enabled", True) is not False,
        builtin=bool(spec.get("builtin", True)),
        sort_order=int(spec.get("sort_order") or 0),
        seed_rev="",
        prompt_version=int(spec.get("prompt_version") or 1),
        overrides_json=dict(spec.get("overrides_json") or {}),
    )


def list_jobs() -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.llm_job import LlmJob

    ensure_db()
    db = SessionLocal()
    try:
        rows = db.query(LlmJob).order_by(LlmJob.sort_order, LlmJob.id).all()
        return [_public(r) for r in rows]
    finally:
        db.close()


def get_job(job_id: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.llm_job import LlmJob

    jid = str(job_id or "").strip()
    if not jid:
        return None
    ensure_db()
    db = SessionLocal()
    try:
        row = db.query(LlmJob).filter(LlmJob.id == jid).first()
        if row is None:
            return None
        return _public(row)
    finally:
        db.close()


def _slot_names(row: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for s in row.get("slots") or []:
        if not isinstance(s, dict):
            continue
        key = str(s.get("name") or s.get("id") or "").strip()
        if key:
            out.add(key)
    return out


def _strip_when_blocks(row: dict[str, Any]) -> bool:
    """合并带 when 的 system 块为单块；去掉 when / flags。返回是否有改动。"""
    changed = False
    system = [dict(b) for b in (row.get("system_blocks") or []) if isinstance(b, dict)]
    if any(str(b.get("when") or "").strip() for b in system):
        parts: list[str] = []
        for block in system:
            if block.get("enabled") is False:
                continue
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
        row["system_blocks"] = [{"id": "main", "text": "\n\n".join(parts), "enabled": True}] if parts else []
        changed = True
    for side in ("system_blocks", "user_blocks"):
        for block in row.get(side) or []:
            if isinstance(block, dict) and block.pop("when", None):
                changed = True
    if row.get("flags"):
        row["flags"] = []
        changed = True
    return changed


def upgrade_jobs_strip_when() -> int:
    """启动迁移：去掉 llm_jobs 里的 when 条件块。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob

    n = 0
    with session_scope() as db:
        rows = db.query(LlmJob).all()
        for row in rows:
            spec = _public(row)
            if not _strip_when_blocks(spec):
                continue
            _validate_job(spec)
            _smoke_render(spec)
            row.system_blocks_json = list(spec.get("system_blocks") or [])
            row.user_blocks_json = list(spec.get("user_blocks") or [])
            row.flags_json = list(spec.get("flags") or [])
            n += 1
        db.flush()
    return n


def _validate_job(row: dict[str, Any]) -> None:
    slots = _slot_names(row)
    for side in ("system_blocks", "user_blocks"):
        for block in row.get(side) or []:
            if not isinstance(block, dict):
                continue
            if str(block.get("when") or "").strip():
                raise ValueError("不再支持 when 条件块，请合并为单一 system 块")
            slot = block.get("slot")
            if slot and str(slot) not in slots:
                raise ValueError(f"块引用了未声明槽：{slot}")
            try:
                cap = int(block.get("max_chars") or 0)
            except (TypeError, ValueError):
                cap = 0
            if cap and not (1 <= cap <= 20000):
                raise ValueError("max_chars 须在 1..20000")
    if not any(str(b.get("text") or b.get("slot") or "").strip() for b in (row.get("system_blocks") or [])):
        raise ValueError("system 块不能全空")


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
    render_job(row, _fake_slots(row))


def _blocks_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "system_blocks": copy.deepcopy(row.get("system_blocks") or []),
        "user_blocks": copy.deepcopy(row.get("user_blocks") or []),
    }


def _blocks_changed(current: dict[str, Any], merged: dict[str, Any]) -> bool:
    return _blocks_snapshot(current) != _blocks_snapshot(merged)


def _revision_list(row: dict[str, Any]) -> list[dict[str, Any]]:
    bag = row.get("overrides_json") if isinstance(row.get("overrides_json"), dict) else {}
    return [dict(x) for x in (bag.get("revisions") or []) if isinstance(x, dict)]


def _set_revisions(row: dict[str, Any], revisions: list[dict[str, Any]]) -> None:
    bag = dict(row.get("overrides_json") or {})
    bag["revisions"] = revisions[-20:]
    row["overrides_json"] = bag


def upgrade_jobs_prompt_version() -> int:
    """旧库补 prompt_version，修订记录补 version 字段。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob

    n = 0
    with session_scope() as db:
        rows = db.query(LlmJob).all()
        for row in rows:
            spec = _public(row)
            changed = False
            if not int(row.prompt_version or 0):
                row.prompt_version = 1
                spec["prompt_version"] = 1
                changed = True
            revs = _revision_list(spec)
            for i, rev in enumerate(revs):
                if rev.get("version") is None:
                    rev["version"] = max(1, int(spec.get("prompt_version") or 1) - len(revs) + i)
                    changed = True
            if changed:
                _set_revisions(spec, revs)
                row.overrides_json = dict(spec.get("overrides_json") or {})
                n += 1
        db.flush()
    return n


def save_job(job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob

    jid = str(job_id or body.get("id") or "").strip()
    if not jid:
        raise ValueError("缺少 job id")
    current = get_job(jid)
    if not current:
        raise ValueError(f"未知 job：{jid}")

    merged = copy.deepcopy(current)
    for key in (
        "label", "summary", "engine", "role_id", "output_schema",
        "slots", "flags", "system_blocks", "user_blocks", "image", "call", "enabled", "sort_order",
    ):
        if key in body and body[key] is not None:
            merged[key] = body[key]
    if "system_blocks" in body and body["system_blocks"] is not None:
        _strip_when_blocks(merged)

    activate_version = body.get("activate_version")
    if activate_version is not None:
        target = int(activate_version)
        hit = next((r for r in reversed(_revision_list(current)) if int(r.get("version") or 0) == target), None)
        if hit is None:
            raise ValueError(f"找不到版本 v{target}")
        merged["system_blocks"] = copy.deepcopy(hit.get("system_blocks") or [])
        merged["user_blocks"] = copy.deepcopy(hit.get("user_blocks") or [])
        revisions = _revision_list(current)
        revisions.append({
            "version": int(current.get("prompt_version") or 1),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **_blocks_snapshot(current),
        })
        merged["prompt_version"] = int(current.get("prompt_version") or 1) + 1
        _set_revisions(merged, revisions)
    elif body.get("reset"):
        revisions = _revision_list(current)
        if not revisions:
            raise ValueError("无法恢复：没有历史修订")
        baseline = revisions.pop()
        merged["system_blocks"] = copy.deepcopy(baseline.get("system_blocks") or [])
        merged["user_blocks"] = copy.deepcopy(baseline.get("user_blocks") or [])
        merged["prompt_version"] = int(baseline.get("version") or max(1, int(current.get("prompt_version") or 1) - 1))
        _set_revisions(merged, revisions)
    elif _blocks_changed(current, merged):
        revisions = _revision_list(current)
        revisions.append({
            "version": int(current.get("prompt_version") or 1),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **_blocks_snapshot(current),
        })
        merged["prompt_version"] = int(current.get("prompt_version") or 1) + 1
        _set_revisions(merged, revisions)

    _validate_job(merged)
    _smoke_render(merged)

    with session_scope() as db:
        row = db.query(LlmJob).filter(LlmJob.id == jid).first()
        if row is None:
            row = _to_row({**merged, "id": jid, "builtin": True})
            db.add(row)
        else:
            row.label = str(merged.get("label") or jid)
            row.summary = str(merged.get("summary") or "")
            row.engine = str(merged.get("engine") or row.engine)
            row.role_id = str(merged.get("role_id") or row.role_id)
            row.output_schema = str(merged.get("output_schema") or row.output_schema)
            row.slots_json = list(merged.get("slots") or [])
            row.flags_json = list(merged.get("flags") or [])
            row.system_blocks_json = list(merged.get("system_blocks") or [])
            row.user_blocks_json = list(merged.get("user_blocks") or [])
            row.image_json = dict(merged.get("image") or {})
            row.call_json = dict(merged.get("call") or {})
            if "enabled" in merged:
                row.enabled = bool(merged.get("enabled"))
            if "sort_order" in merged:
                row.sort_order = int(merged.get("sort_order") or 0)
            row.prompt_version = int(merged.get("prompt_version") or row.prompt_version or 1)
            row.overrides_json = dict(merged.get("overrides_json") or {})
        db.flush()

    hit = get_job(jid)
    if not hit:
        raise ValueError("保存后读不到 job")
    _validate_job(hit)
    return hit


def preview_job(
    job_id: str,
    slots: dict[str, str] | None = None,
    flags: dict[str, bool] | None = None,
    *,
    system_blocks: list[dict[str, Any]] | None = None,
    user_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    row = get_job(job_id)
    if not row:
        raise ValueError(f"llm_jobs 里没有 {job_id}")
    draft = copy.deepcopy(row)
    if system_blocks is not None:
        draft["system_blocks"] = copy.deepcopy(system_blocks)
    if user_blocks is not None:
        draft["user_blocks"] = copy.deepcopy(user_blocks)
    if system_blocks is not None:
        _strip_when_blocks(draft)
    use_slots = dict(slots or _fake_slots(draft))
    messages, meta = render_job(draft, use_slots)
    return {"messages": messages, "meta": meta}


def startup_health() -> dict[str, Any]:
    ok: list[str] = []
    broken: list[dict[str, str]] = []
    for row in list_jobs():
        if not row.get("enabled", True):
            continue
        try:
            _smoke_render(row)
            ok.append(str(row.get("id") or ""))
        except (JobRenderError, ValueError) as e:
            broken.append({"id": str(row.get("id") or ""), "error": str(e)})
    return {"ok": len(ok), "broken": broken}


ROLE_JOB_ALIASES: dict[str, str] = {
    "req-analyst": "analyze_req",
    "mindmap-writer": "draft_mindmap",
    "case-writer": "draft_cases",
    "test-engineer": "test-engineer-chat",
    "im-qa-assistant": "im-dialogue",
    "im-defect-assistant": "im-defect",
    "run-case": "agent-decide",
}


def resolve_job_id(key: str) -> str:
    raw = str(key or "").strip()
    if not raw:
        return ""
    if get_job(raw):
        return raw
    alias = ROLE_JOB_ALIASES.get(raw)
    if alias and get_job(alias):
        return alias
    return raw


def job_system_text(job_id: str, *, explain_mode: bool = False) -> str:
    from mino_nexus.ai.prompt_render import _render_side

    jid = resolve_job_id(job_id)
    row = get_job(jid)
    if not row:
        return ""
    return _render_side(list(row.get("system_blocks") or []), {})
