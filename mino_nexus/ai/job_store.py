"""job 读取的薄封装。正文在 prompts/，由 prompt_repo 加载。"""
from __future__ import annotations

import copy
from typing import Any

from mino_nexus.platform.llm.render import JobRenderError, render_job
from mino_nexus.platform.llm.repo import get_job, list_jobs

ROLE_JOB_ALIASES: dict[str, str] = {
    "req-analyst": "analyze_req",
    "mindmap-writer": "draft_mindmap",
    "case-writer": "draft_cases",
    "test-engineer": "test-engineer-chat",
    "im-qa-assistant": "im-dialogue",
    "im-defect-assistant": "im-defect",
    "run-case": "agent-decide",
}


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


def preview_job(
    job_id: str,
    slots: dict[str, str] | None = None,
    flags: dict[str, bool] | None = None,
    *,
    system_blocks: list[dict[str, Any]] | None = None,
    user_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    del flags  # 条件块已去掉，预览不再按 flag 裁剪
    row = get_job(job_id)
    if not row:
        raise ValueError(f"prompts 里没有 {job_id}")
    draft = copy.deepcopy(row)
    if system_blocks is not None:
        draft["system_blocks"] = copy.deepcopy(system_blocks)
    if user_blocks is not None:
        draft["user_blocks"] = copy.deepcopy(user_blocks)
    use_slots = dict(slots or _fake_slots(draft))
    messages, meta = render_job(draft, use_slots)
    return {"messages": messages, "meta": meta}


def startup_health() -> dict[str, Any]:
    from mino_nexus.platform.llm.repo import load_all

    try:
        load_all()
    except Exception as exc:
        return {"ok": 0, "broken": [{"id": "*", "error": str(exc)}]}
    ok: list[str] = []
    broken: list[dict[str, str]] = []
    for row in list_jobs():
        if not row.get("enabled", True):
            continue
        try:
            render_job(row, _fake_slots(row))
            ok.append(str(row.get("id") or ""))
        except (JobRenderError, ValueError) as exc:
            broken.append({"id": str(row.get("id") or ""), "error": str(exc)})
    return {"ok": len(ok), "broken": broken}


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
    from mino_nexus.platform.llm.render import _render_side

    del explain_mode
    jid = resolve_job_id(job_id)
    row = get_job(jid)
    if not row:
        return ""
    return _render_side(list(row.get("system_blocks") or []), {})
