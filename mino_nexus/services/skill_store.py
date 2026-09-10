"""技能表。builtin 灌种一次；读时坏行回退代码副本。"""
from __future__ import annotations

import copy
import re
from typing import Any

from mino_nexus.ai.skill_defs import (
    ALIASES,
    CATEGORIES,
    ENGINES,
    INPUT_TYPES,
    POINTERS,
    VIEWS,
    builtin_skills,
    enums,
)
from mino_nexus.catalog.exec_classes import ALL_KINDS

_SLUG = re.compile(r"[^a-z0-9_-]+")


def _now_builtin() -> dict[str, dict[str, Any]]:
    return {str(row["id"]): copy.deepcopy(row) for row in builtin_skills()}


def resolve_id(skill_id: str) -> str:
    raw = str(skill_id or "").strip()
    return ALIASES.get(raw, raw)


def _public(row) -> dict[str, Any]:
    role_id = str(getattr(row, "role_id", "") or "")
    role_label = str(getattr(row, "role_label", "") or "")
    sop = dict(row.sop_json or {}) if isinstance(row.sop_json, dict) else {}
    inp = dict(row.input_json or {}) if isinstance(row.input_json, dict) else {}
    view = dict(row.view_json or {}) if isinstance(row.view_json, dict) else {}
    return {
        "id": row.id,
        "label": row.label or row.id,
        "summary": row.summary or "",
        "category": row.category or "flow",
        "role": {"id": role_id, "label": role_label or role_id},
        "role_id": role_id,
        "triggers": list(row.triggers_json or []),
        "engine": row.engine or "chat",
        "system_prompt": row.system_prompt or "",
        "prompt_chars": len(str(row.system_prompt or "")),
        "sop": sop,
        "input": inp,
        "view": view,
        "view_id": str(view.get("id") or "job-timeline"),
        "enabled": bool(row.enabled),
        "builtin": bool(row.builtin),
        "sort_order": int(row.sort_order or 0),
        "editable": True,
        "prompt_custom": not bool(row.builtin) or bool(str(row.system_prompt or "").strip()),
    }


def _fallback(skill_id: str) -> dict[str, Any] | None:
    hit = _now_builtin().get(resolve_id(skill_id))
    if not hit:
        return None
    out = copy.deepcopy(hit)
    out["role_id"] = (out.get("role") or {}).get("id") or ""
    out["view_id"] = (out.get("view") or {}).get("id") or "job-timeline"
    out["prompt_chars"] = len(str(out.get("system_prompt") or ""))
    out["editable"] = True
    out["prompt_custom"] = False
    return out


def _hydrate(row) -> dict[str, Any]:
    pub = _public(row)
    base = _fallback(row.id)
    if base:
        if not str(pub.get("system_prompt") or "").strip():
            from mino_nexus.services.job_store import job_system_text, resolve_job_id

            prompt = job_system_text(resolve_job_id(row.id))
            pub["system_prompt"] = prompt
            pub["prompt_chars"] = len(prompt)
            pub["prompt_custom"] = False
        if not pub.get("sop"):
            pub["sop"] = base.get("sop") or {}
        if not pub.get("input"):
            pub["input"] = base.get("input") or {}
        if not (pub.get("view") or {}).get("id"):
            pub["view"] = base.get("view") or {"id": "job-timeline"}
            pub["view_id"] = pub["view"].get("id")
    return pub


def seed_skills() -> int:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.skill import Skill

    added = 0
    with session_scope() as db:
        for spec in builtin_skills():
            sid = spec["id"]
            if db.query(Skill).filter(Skill.id == sid).first():
                continue
            db.add(_to_row(spec))
            added += 1
    return added


def upgrade_run_case_sop_guards() -> int:
    """run-case 各阶段 guards / tool_kinds 与 builtin 对齐（旧库可能缺项）。"""
    import copy

    from mino_nexus.ai.skill_defs import DEFAULT_SOP
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.skill import Skill

    want_by_phase = {
        str(p.get("id") or "").strip().lower(): p
        for p in (DEFAULT_SOP.get("phases") or [])
        if str(p.get("id") or "").strip()
    }
    updated = 0
    with session_scope() as db:
        row = db.query(Skill).filter(Skill.id == "run-case").first()
        if not row:
            return 0
        sop = copy.deepcopy(dict(row.sop_json or {}))
        phases = list(sop.get("phases") or [])
        if not phases:
            return 0
        changed = False
        for phase in phases:
            pid = str(phase.get("id") or "").strip().lower()
            spec = want_by_phase.get(pid)
            if not spec:
                continue
            want_guards = [str(g).strip() for g in (spec.get("guards") or []) if str(g).strip()]
            guards = [str(g).strip() for g in (phase.get("guards") or []) if str(g).strip()]
            deprecated = {"skip_repeat_tap", "stuck_alternation"}
            guards = [g for g in guards if g not in deprecated]
            for g in want_guards:
                if g not in guards:
                    guards.append(g)
                    changed = True
            if guards != list(phase.get("guards") or []):
                phase["guards"] = guards
                changed = True
            want_kinds = [str(k).strip() for k in (spec.get("tool_kinds") or []) if str(k).strip()]
            kinds = [str(k).strip() for k in (phase.get("tool_kinds") or []) if str(k).strip()]
            for k in want_kinds:
                if k not in kinds:
                    kinds.append(k)
                    changed = True
            if kinds != list(phase.get("tool_kinds") or []):
                phase["tool_kinds"] = kinds
        if changed:
            sop["phases"] = phases
            row.sop_json = sop
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(row, "sop_json")
            updated = 1
    return updated


def _to_row(spec: dict[str, Any], *, builtin: bool | None = None):
    from mino_nexus.models.skill import Skill

    role = spec.get("role") if isinstance(spec.get("role"), dict) else {}
    view = spec.get("view") if isinstance(spec.get("view"), dict) else {"id": spec.get("view_id") or "job-timeline"}
    return Skill(
        id=str(spec.get("id") or "").strip(),
        label=str(spec.get("label") or spec.get("id") or ""),
        summary=str(spec.get("summary") or ""),
        category=str(spec.get("category") or "flow"),
        role_id=str(spec.get("role_id") or role.get("id") or ""),
        role_label=str(spec.get("role_label") or role.get("label") or ""),
        engine=str(spec.get("engine") or "chat"),
        system_prompt=str(spec.get("system_prompt") or ""),
        sop_json=dict(spec.get("sop") or {}),
        input_json=dict(spec.get("input") or {}),
        view_json=dict(view or {"id": "job-timeline"}),
        triggers_json=list(spec.get("triggers") or []),
        enabled=spec.get("enabled", True) is not False,
        builtin=bool(spec.get("builtin") if builtin is None else builtin),
        sort_order=int(spec.get("sort_order") or 0),
    )


def list_skills() -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.skill import Skill

    ensure_db()
    seed_skills()
    db = SessionLocal()
    try:
        rows = db.query(Skill).order_by(Skill.sort_order, Skill.id).all()
        out = [_hydrate(r) for r in rows]
        seen = {r["id"] for r in out}
        for spec in builtin_skills():
            if spec["id"] not in seen:
                fb = _fallback(spec["id"])
                if fb:
                    out.append(fb)
        out.sort(key=lambda r: (int(r.get("sort_order") or 0), str(r.get("id") or "")))
        return out
    finally:
        db.close()


def get_skill(skill_id: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.skill import Skill

    sid = resolve_id(skill_id)
    if not sid:
        return None
    ensure_db()
    db = SessionLocal()
    try:
        row = db.query(Skill).filter(Skill.id == sid).first()
        if row is not None:
            return _hydrate(row)
    finally:
        db.close()
    return _fallback(sid)


def skill_prompt(skill_id: str) -> str:
    from mino_nexus.services.job_store import job_system_text, resolve_job_id

    sid = resolve_id(skill_id)
    return job_system_text(resolve_job_id(sid))


def _validate_sop(raw: Any) -> dict[str, Any]:
    from mino_nexus.loop.sop_runtime import normalize_inspections, normalize_phases, sop_union_tool_kinds

    src = raw if isinstance(raw, dict) else {}
    phases = normalize_phases(src.get("phases"))
    inspections = normalize_inspections(src.get("inspections") if "inspections" in src else None)
    pointer = str(src.get("pointer") or "none").strip()
    if pointer not in POINTERS:
        pointer = "none"
    kinds = [str(x) for x in (src.get("tool_kinds") or sop_union_tool_kinds(phases)) if str(x) in ALL_KINDS]
    if not kinds:
        kinds = sop_union_tool_kinds(phases)
    try:
        max_steps = max(1, min(80, int(src.get("max_steps") or 24)))
    except (TypeError, ValueError):
        max_steps = 24
    return {
        "phases": phases,
        "inspections": inspections,
        "pointer": pointer,
        "tool_kinds": kinds,
        "max_steps": max_steps,
    }


def _clean_sop(raw: Any) -> dict[str, Any]:
    try:
        return _validate_sop(raw)
    except ValueError:
        src = raw if isinstance(raw, dict) else {}
        phases = [str(x) for x in (src.get("phases") or ["prep", "do", "check"]) if str(x).strip()]
        pointer = str(src.get("pointer") or "none").strip()
        if pointer not in POINTERS:
            pointer = "none"
        kinds = [str(x) for x in (src.get("tool_kinds") or list(ALL_KINDS)) if str(x) in ALL_KINDS]
        if not kinds:
            kinds = list(ALL_KINDS)
        try:
            max_steps = max(1, min(80, int(src.get("max_steps") or 24)))
        except (TypeError, ValueError):
            max_steps = 24
        return {"phases": phases or ["prep", "do", "check"], "pointer": pointer, "tool_kinds": kinds, "max_steps": max_steps}


def _clean_input(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    typ = str(src.get("type") or "none").strip()
    if typ not in INPUT_TYPES:
        typ = "none"
    mapping = src.get("map") if isinstance(src.get("map"), dict) else {}
    return {"type": typ, "map": {str(k): str(v) for k, v in mapping.items() if str(k).strip()}}


def _clean_view(raw: Any, view_id: str = "") -> dict[str, str]:
    if isinstance(raw, dict) and raw.get("id"):
        vid = str(raw.get("id") or "").strip()
    else:
        vid = str(view_id or "").strip()
    if vid not in VIEWS:
        vid = "job-timeline"
    return {"id": vid}


def _validate_engine(engine: str) -> str:
    e = str(engine or "chat").strip()
    if e not in ENGINES:
        raise ValueError(f"不支持的引擎：{engine}（可选 {', '.join(ENGINES)}）")
    return e


def _slug(raw: str) -> str:
    text = str(raw or "").strip().lower().replace(" ", "-")
    text = _SLUG.sub("-", text).strip("-")
    return text[:40]


def save_skill(skill_id: str, body: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.skill import Skill

    sid = resolve_id(skill_id) or _slug(body.get("id") or body.get("label") or "")
    if not sid:
        raise ValueError("缺少技能 id")
    if create and _row_exists(sid):
        raise ValueError(f"技能已存在：{sid}")
    engine = _validate_engine(body.get("engine") or (get_skill(sid) or {}).get("engine") or "chat")
    category = str(body.get("category") or "flow").strip()
    if category not in CATEGORIES:
        category = "flow"
    role = body.get("role") if isinstance(body.get("role"), dict) else {}
    with session_scope() as db:
        row = db.query(Skill).filter(Skill.id == sid).first()
        if row is None:
            spec = _fallback(sid) or {
                "id": sid,
                "label": body.get("label") or sid,
                "engine": engine,
                "builtin": False,
            }
            row = _to_row({**spec, "id": sid, "builtin": False}, builtin=False)
            db.add(row)
            db.flush()
        if "label" in body:
            row.label = str(body.get("label") or row.label or sid)
        if "summary" in body:
            row.summary = str(body.get("summary") or "")
        if "category" in body:
            row.category = category
        if "engine" in body:
            row.engine = engine
        if "system_prompt" in body:
            text = str(body.get("system_prompt") or "").strip()
            if not text:
                raise ValueError("prompt 不能为空")
            row.system_prompt = text
        if "sop" in body:
            row.sop_json = _clean_sop(body.get("sop"))
        if "input" in body:
            row.input_json = _clean_input(body.get("input"))
        if "view" in body or "view_id" in body:
            row.view_json = _clean_view(body.get("view"), str(body.get("view_id") or ""))
        if "triggers" in body and isinstance(body.get("triggers"), list):
            row.triggers_json = [str(x) for x in body.get("triggers") or [] if str(x).strip()]
        if "role" in body or "role_id" in body or "role_label" in body:
            row.role_id = str(body.get("role_id") or role.get("id") or row.role_id or "")
            row.role_label = str(body.get("role_label") or role.get("label") or row.role_label or "")
        if "enabled" in body:
            row.enabled = bool(body.get("enabled"))
        if "sort_order" in body:
            row.sort_order = int(body.get("sort_order") or 0)
        db.flush()
        sid = row.id
    hit = get_skill(sid)
    if not hit:
        raise ValueError("保存后读不到技能")
    return hit


def reset_skill_prompt(skill_id: str) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.skill import Skill

    sid = resolve_id(skill_id)
    with session_scope() as db:
        row = db.query(Skill).filter(Skill.id == sid).first()
        if row is None:
            raise ValueError(f"未知技能：{skill_id}")
        row.system_prompt = ""
        db.flush()
    hit = get_skill(sid)
    if not hit:
        raise ValueError(f"重置后读不到技能：{skill_id}")
    return hit


def _row_exists(skill_id: str) -> bool:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.skill import Skill

    ensure_db()
    db = SessionLocal()
    try:
        return db.query(Skill).filter(Skill.id == skill_id).first() is not None
    finally:
        db.close()


def catalog_payload() -> dict[str, Any]:
    rows = list_skills()
    return {
        "skills": rows,
        "enums": enums(),
        "counts": {"skills": len(rows)},
    }
