"""阶段边界巡检：只产文本槽，不派设备动作。"""
from __future__ import annotations

from typing import Any

from mino_nexus.ai.planner import inspect_session


def resolve_knowledge_scope(ctx) -> tuple[str, str]:
    """跑批 scope：app_id + 所属 project_id（知识 app_ids 可写任一侧）。"""
    app_id = str(getattr(ctx, "app_id", "") or "").strip()
    project_id = str(getattr(ctx, "project_id", "") or "").strip()
    if not project_id and app_id:
        try:
            from mino_nexus.services import project_store as ps

            app = ps.find_app(app_id)
            if app:
                project_id = str(app.get("project_id") or "").strip()
        except Exception:
            pass
    return app_id, project_id


def format_session_block(result: dict[str, Any], *, required: str = "any") -> str:
    if not result.get("ok"):
        reason = str(result.get("reason") or "未观察").strip()
        return f"（会话观察未成功：{reason}）"
    bits = [
        f"session={result.get('session')}",
        f"identity={result.get('identity')}",
        f"seen={result.get('seen')}",
        f"next={result.get('next')}",
        f"reason={result.get('reason')}",
    ]
    if str(result.get("next") or "") == "logout":
        bits.append("action=先退出至登录页再执行登录步骤")
    req = str(required or "any").strip().lower()
    if req == "guest":
        bits.append("required=guest")
    elif req == "logged_in":
        bits.append("required=logged_in")
    return " ".join(str(x) for x in bits if x).strip()


def refresh_session_block(
    *,
    shot,
    ctx,
    case: dict[str, Any],
    provider_id: str = "",
    slot_sink: dict[str, str],
) -> dict[str, Any]:
    """跑 inspect-session 并写入 session_block（含 guest/logout 钳制）。"""
    from mino_nexus.runtime.session_gate import (
        ensure_case_scene,
        format_required_session_brief,
        reconcile_inspect_session,
        required_session as required_session_enum,
    )

    if not shot or not getattr(shot, "has_image", lambda: False)():
        slot_sink["session_block"] = "（无截图，跳过会话观察）"
        return {"ok": False, "reason": "无截图"}
    scene = ensure_case_scene(case, getattr(ctx, "case_scene", None))
    ctx.case_scene = scene
    req_enum = required_session_enum(scene=scene)
    row = inspect_session(
        required_session=format_required_session_brief(scene),
        knowledge_hint=str(slot_sink.get("knowledge_hint") or ""),
        accounts_brief=str(getattr(ctx, "accounts_brief", "") or ""),
        image_base64=str(getattr(shot, "image_base64", "") or ""),
        image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
        provider_id=provider_id or None,
    )
    row = reconcile_inspect_session(row, required=req_enum)
    slot_sink["session_block"] = format_session_block(row, required=req_enum)
    return row


def run_inspections(
    specs: list[dict[str, Any]],
    *,
    at: str,
    shot,
    ctx,
    provider_id: str = "",
    slot_sink: dict[str, str],
    case: dict[str, Any] | None = None,
) -> None:
    """在指定时机跑声明的巡检 job，结果写入 slot_sink。"""
    mark = str(at or "").strip()
    if not mark:
        return
    for spec in specs or []:
        if str(spec.get("at") or "") != mark:
            continue
        job_id = str(spec.get("job") or "").strip()
        if job_id == "inspect-session":
            row = refresh_session_block(
                shot=shot,
                ctx=ctx,
                case=case or {},
                provider_id=provider_id,
                slot_sink=slot_sink,
            )
            _log_inspection(mark=mark, job_id=job_id, result=row, session_block=slot_sink["session_block"])
        # 其它 observe job 后续按同一模式扩展


def _log_inspection(
    *,
    mark: str,
    job_id: str,
    result: dict[str, Any] | None = None,
    session_block: str = "",
) -> None:
    try:
        from mino_nexus.loop.session_log import active_writer

        writer = active_writer()
        if writer is None:
            return
        row = dict(result or {})
        writer.append(
            "inspection/done",
            {
                "at": mark,
                "job_id": job_id,
                "ok": bool(row.get("ok")),
                "session_block": str(session_block or "")[:1200],
                "session": str(row.get("session") or ""),
                "identity": str(row.get("identity") or ""),
                "next": str(row.get("next") or ""),
                "reason": str(row.get("reason") or "")[:400],
            },
        )
    except Exception:
        pass


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
    from mino_nexus.services.knowledge_match import match_knowledge

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


def expand_named_knowledge(
    decision,
    rows: list[dict[str, Any]],
    *,
    ctx,
    max_chars: int = 800,
) -> tuple[str, list[str]]:
    """模型点名后取正文。返回 (正文块, 命中的 id)。"""
    from mino_nexus.ai.knowledge_hint import build_body_text, named_ids
    from mino_nexus.services.knowledge_match import items_by_ids

    app_id, project_id = resolve_knowledge_scope(ctx)
    if not app_id and not project_id:
        return "", []
    ids = named_ids(decision, rows)
    if not ids:
        return "", []
    try:
        items = items_by_ids(ids, app_id=app_id, project_id=project_id)
    except Exception as exc:
        _log_knowledge(error=f"{type(exc).__name__}: {exc}")
        return "", []
    body = build_body_text(items, max_chars=max_chars)
    used = [str(it.get("id") or "") for it in items[:1] if it.get("id")]
    _log_knowledge(named=used)
    return body, used


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
        from mino_nexus.loop.session_log import active_writer

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
