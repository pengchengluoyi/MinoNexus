"""llm_jobs 表：读写、保存校验、启动烟测。"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from mino_nexus.ai.prompt_render import JobRenderError, render_job
from mino_nexus.loop.registry import PREDICATES


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
    return {str(s.get("name") or "") for s in (row.get("slots") or []) if isinstance(s, dict)}


def _validate_job(row: dict[str, Any]) -> None:
    slots = _slot_names(row)
    for side in ("system_blocks", "user_blocks"):
        for block in row.get(side) or []:
            if not isinstance(block, dict):
                continue
            when = str(block.get("when") or "").strip()
            if when and when not in PREDICATES:
                raise ValueError(f"未知谓词：{when}")
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
        name = str(spec.get("name") or "")
        kind = str(spec.get("kind") or "text")
        if kind == "json":
            out[name] = "{}"
        elif kind == "image":
            out[name] = "aaaa"
        else:
            out[name] = "X"
    return out


def _smoke_render(row: dict[str, Any]) -> None:
    slots = _fake_slots(row)
    flags_all = {str(f): True for f in (row.get("flags") or [])}
    flags_none = {str(f): False for f in (row.get("flags") or [])}
    for flags in (flags_all, flags_none):
        render_job(row, slots, flags)


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

    if body.get("reset"):
        prev_overrides = dict(current.get("overrides_json") or {})
        revisions = list(prev_overrides.get("revisions") or [])
        if not revisions:
            raise ValueError("无法恢复：没有历史修订")
        baseline = revisions.pop()
        merged["system_blocks"] = copy.deepcopy(baseline.get("system_blocks") or [])
        merged["user_blocks"] = copy.deepcopy(baseline.get("user_blocks") or [])
        merged["overrides_json"] = {"revisions": revisions}
    else:
        prev_overrides = dict(current.get("overrides_json") or {})
        revisions = list(prev_overrides.get("revisions") or [])
        revisions.append({
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "system_blocks": copy.deepcopy(current.get("system_blocks") or []),
            "user_blocks": copy.deepcopy(current.get("user_blocks") or []),
        })
        merged["overrides_json"] = {"revisions": revisions[-5:]}

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
            row.overrides_json = dict(merged.get("overrides_json") or {})
        db.flush()

    hit = get_job(jid)
    if not hit:
        raise ValueError("保存后读不到 job")
    _validate_job(hit)
    return hit


def preview_job(job_id: str, slots: dict[str, str] | None = None, flags: dict[str, bool] | None = None) -> dict[str, Any]:
    row = get_job(job_id)
    if not row:
        raise ValueError(f"llm_jobs 里没有 {job_id}")
    use_slots = dict(slots or _fake_slots(row))
    use_flags = dict(flags or {str(f): True for f in (row.get("flags") or [])})
    messages, meta = render_job(row, use_slots, use_flags)
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
    flag_keys = [str(x) for x in (row.get("flags") or [])]
    flags = {k: (bool(explain_mode) if k == "explain_mode" else False) for k in flag_keys}
    return _render_side(list(row.get("system_blocks") or []), {}, flags)
