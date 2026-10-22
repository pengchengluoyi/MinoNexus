"""步级知识/文档检索：给计划器准备上下文块，不派设备动作。"""
from __future__ import annotations

from typing import Any


def resolve_knowledge_scope(ctx) -> tuple[str, str]:
    """跑批 scope：app_id + 所属 project_id（知识 app_ids 可写任一侧）。"""
    app_id = str(getattr(ctx, "app_id", "") or "").strip()
    project_id = str(getattr(ctx, "project_id", "") or "").strip()
    if not project_id and app_id:
        try:
            from mino_nexus.resources import project_store as ps

            app = ps.find_app(app_id)
            if app:
                project_id = str(app.get("project_id") or "").strip()
        except Exception:
            pass
    return app_id, project_id


def match_step_knowledge(
    *,
    ctx,
    case: dict[str, Any],
    cursor,
    history: list[str],
    steps: list[dict[str, Any]],
    hierarchy_text: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    """本步知识检索。返回带 score / used 的命中行；scope 为空返回空。"""
    from mino_nexus.ai.knowledge_hint import (
        build_case_intent,
        build_knowledge_scene,
        build_query,
        build_step_focus,
        rank_for_intent,
    )
    from mino_nexus.knowledge.knowledge_match import match_knowledge

    app_id, project_id = resolve_knowledge_scope(ctx)
    if not app_id and not project_id:
        return []
    intent = build_case_intent(
        case_name=str(case.get("name") or ""),
        goal=cursor.decide_goal(),
        steps_text=cursor.prompt_block(),
        precondition=str(case.get("precondition") or ""),
        success_criteria=cursor.decide_success(),
    )
    last = ""
    if steps:
        tail = steps[-1] or {}
        last = " ".join(str(x) for x in (
            tail.get("capability_id") or "",
            tail.get("thought") or "",
            tail.get("summary") or "",
        ) if x)
    step_focus = build_step_focus(cursor)
    scene = build_knowledge_scene(
        ctx=ctx,
        cursor=cursor,
        case=case,
        hierarchy_text=hierarchy_text,
    )
    query = build_query(
        case_intent=intent,
        step_focus=step_focus,
        last_action=last,
        history="\n".join(history[-6:]) if history else "",
        screen=str(hierarchy_text or "")[:1600],
    )
    try:
        hits = match_knowledge(
            query,
            app_id=app_id,
            project_id=project_id,
            scene=scene,
            limit=limit,
        )
    except Exception as exc:  # 检索失败不该拖垮跑批
        _log_knowledge(error=f"{type(exc).__name__}: {exc}")
        return []
    rows = rank_for_intent(hits, case_intent=intent, limit=limit)
    _log_knowledge(
        rows=rows,
        app_id=app_id,
        project_id=project_id,
        step_focus=step_focus,
        scene=scene,
    )
    return rows


def match_step_docs(
    *,
    ctx,
    case: dict[str, Any],
    cursor,
    history: list[str],
    steps: list[dict[str, Any]],
    hierarchy_text: str = "",
    limit: int = 3,
) -> str:
    """本步文档库 FTS 检索 → doc_context 块。无 app 或无文档时返回空串。"""
    from mino_nexus.ai.knowledge_hint import (
        build_case_intent,
        build_query,
        build_step_focus,
    )
    from mino_nexus.knowledge.doc_match import match_step_docs as _match

    app_id, _project_id = resolve_knowledge_scope(ctx)
    if not app_id:
        return ""
    intent = build_case_intent(
        case_name=str(case.get("name") or ""),
        goal=cursor.decide_goal(),
        steps_text=cursor.prompt_block(),
        precondition=str(case.get("precondition") or ""),
        success_criteria=cursor.decide_success(),
    )
    last = ""
    if steps:
        tail = steps[-1] or {}
        last = " ".join(str(x) for x in (
            tail.get("capability_id") or "",
            tail.get("thought") or "",
            tail.get("summary") or "",
        ) if x)
    query = build_query(
        case_intent=intent,
        step_focus=build_step_focus(cursor),
        last_action=last,
        history="\n".join(history[-6:]) if history else "",
        screen=str(hierarchy_text or "")[:1600],
    )
    query_vec = getattr(ctx, "doc_query_vec", None)
    if not isinstance(query_vec, list):
        query_vec = None
    try:
        hits, block = _match(query, app_id=app_id, limit=limit, query_vec=query_vec)
    except Exception as exc:
        _log_doc_match(error=f"{type(exc).__name__}: {exc}", app_id=app_id)
        return ""
    _log_doc_match(hits=hits, app_id=app_id, query=query[:400])
    return block


def _log_knowledge(
    *,
    rows: list[dict[str, Any]] | None = None,
    named: list[str] | None = None,
    error: str = "",
    app_id: str = "",
    project_id: str = "",
    step_focus: str = "",
    scene: dict[str, Any] | None = None,
) -> None:
    try:
        from mino_nexus.records.eventlog.writer import active_writer

        writer = active_writer()
        if writer is None:
            return
        writer.append(
            "knowledge/match",
            {
                "app_id": str(app_id or ""),
                "project_id": str(project_id or ""),
                "step_focus": str(step_focus or "")[:600],
                "scene": dict(scene or {}),
                "hits": [
                    {
                        "id": str(r.get("id") or ""),
                        "title": str(r.get("title") or "")[:80],
                        "category": str(r.get("category") or ""),
                        "score": int(r.get("score") or 0),
                        "sit_score": int(r.get("sit_score") or 0),
                        "bind_hit": bool(r.get("bind_hit")),
                        "match_pct": int(r.get("match_pct") or 0),
                        "used": bool(r.get("used")),
                        "used_via": str(r.get("used_via") or ""),
                    }
                    for r in (rows or [])
                ],
                "named": list(named or []),
                "error": error,
            },
        )
    except Exception:
        pass

def _log_doc_match(
    *,
    hits: list[dict[str, Any]] | None = None,
    error: str = "",
    app_id: str = "",
    query: str = "",
) -> None:
    try:
        from mino_nexus.records.eventlog.writer import active_writer

        writer = active_writer()
        if writer is None:
            return
        writer.append(
            "doc/match",
            {
                "app_id": str(app_id or ""),
                "query": str(query or "")[:400],
                "hits": [
                    {
                        "chunk_id": str(h.get("chunk_id") or ""),
                        "source_id": str(h.get("source_id") or ""),
                        "title": str(h.get("title") or "")[:80],
                        "heading": str(h.get("heading") or "")[:80],
                    }
                    for h in (hits or [])
                ],
                "error": error,
            },
        )
    except Exception:
        pass
