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


def program_session_block_from_hierarchy(
    ctx,
    nodes: list[Any],
    *,
    case: dict[str, Any] | None = None,
) -> str:
    """hierarchy 能定论时跳过 inspect-session VLM，避免隐私弹窗被误判为系统权限页。"""
    if not nodes:
        return ""
    from mino_nexus.loop.ui_consent import find_consent_control

    if find_consent_control(list(nodes)) is not None:
        return (
            "session=guest identity=unknown seen=in_app_legal_consent "
            "next=accept_legal_consent "
            "reason=应用内隐私/用户协议弹窗（hierarchy），请 accept_legal_consent；"
            "非系统权限页，勿 recover_bring_target_app_foreground"
        )
    pkg = str(getattr(ctx, "target_package", "") or "").strip()
    from mino_nexus.services.nav_capture_store import run_guard_foreground

    fg = run_guard_foreground(
        list(nodes),
        target_package=pkg,
        platform=str(getattr(ctx, "platform", "") or ""),
        launch_confirmed=bool(getattr(ctx, "app_launch_confirmed", False)),
    )
    overlay = str(fg.get("system_overlay") or "").strip().lower()
    af = str(fg.get("app_foreground") or "").strip().lower()
    sk = str(fg.get("screen_kind") or "").strip().lower()
    if af == "no" or sk in ("foreign", "launcher") or overlay == "yes":
        # 前台非被测 App：送图已 withhold，勿再写 session_block 误导模型。
        return ""
    if af == "yes" and overlay != "yes":
        return (
            "session=guest identity=unknown seen=target_app_foreground "
            "next=observe "
            "reason=被测 App 已在前台（probe/hierarchy）；会话态待本步判定，"
            "优先 accept_legal_consent 或 tap，勿仅凭截图猜系统权限"
        )
    return ""


def _append_required_to_program_block(line: str, *, required: str = "any") -> str:
    req = str(required or "any").strip().lower()
    if req == "guest":
        return f"{line.strip()} required=guest"
    if req == "logged_in":
        return f"{line.strip()} required=logged_in"
    return line.strip()


def _clear_foreign_session_block(slot_sink: dict[str, str]) -> None:
    raw = str(slot_sink.get("session_block") or "")
    if not raw:
        return
    if "seen=foreign_screen" in raw or "return_to_target_app" in raw:
        slot_sink["session_block"] = ""
        return
    if _vlm_session_block_looks_foreign_pollution(raw):
        slot_sink["session_block"] = ""


def llm_session_block(ctx, block: str) -> str:
    """前台非被测 App 时不向 decide 传 session_block（与送图 withhold 一致）。"""
    try:
        from mino_nexus.loop.fuse.llm_screenshot_gate import should_withhold_llm_image

        if should_withhold_llm_image(ctx):
            return ""
    except Exception:
        pass
    return str(block or "").strip()


def _vlm_session_block_looks_foreign_pollution(block: str) -> bool:
    raw = str(block or "")
    if "seen=foreign_screen" in raw:
        return False
    markers = (
        "系统应用",
        "系统设置",
        "不属于",
        "无法判断",
        "存储页面",
        "应用信息",
    )
    if any(m in raw for m in markers) and "session=" in raw:
        return True
    return False


def hierarchy_nodes_from_nav(nav: Any, ctx: Any) -> list[Any]:
    nodes: list[Any] = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
    if nav is not None:
        snap = getattr(nav, "snapshot", None)
        if snap is not None:
            nodes = list(getattr(snap, "nodes", None) or [])
    return nodes


def reconcile_session_block_with_hierarchy(
    ctx,
    slot_sink: dict[str, str],
    nav: Any,
    case: dict[str, Any],
) -> None:
    """每 turn 用 hierarchy 结论覆盖过期的 inspect-session VLM 槽（尤其错包/设置页）。"""
    from mino_nexus.runtime.session_gate import ensure_case_scene, required_session as required_session_enum

    nodes = hierarchy_nodes_from_nav(nav, ctx)
    prog = program_session_block_from_hierarchy(ctx, nodes, case=case)
    af = str(getattr(ctx, "app_foreground", "") or "").strip().lower()
    if not prog and af == "no":
        _clear_foreign_session_block(slot_sink)
        return
    if not prog:
        existing = str(slot_sink.get("session_block") or "")
        if _vlm_session_block_looks_foreign_pollution(existing) and af == "no":
            _clear_foreign_session_block(slot_sink)
        return
    existing = str(slot_sink.get("session_block") or "")
    if "in_app_legal_consent" in prog:
        req = required_session_enum(scene=ensure_case_scene(case, getattr(ctx, "case_scene", None)))
        slot_sink["session_block"] = _append_required_to_program_block(prog, required=req)
        return
    if _session_block_is_conclusive(existing) and not _vlm_session_block_looks_foreign_pollution(existing):
        return
    if "target_app_foreground" in prog or _vlm_session_block_looks_foreign_pollution(existing):
        req = required_session_enum(scene=ensure_case_scene(case, getattr(ctx, "case_scene", None)))
        slot_sink["session_block"] = _append_required_to_program_block(prog, required=req)
    if str(getattr(ctx, "app_foreground", "") or "").strip().lower() == "no":
        _clear_foreign_session_block(slot_sink)


def refresh_session_block(
    *,
    shot,
    ctx,
    case: dict[str, Any],
    provider_id: str = "",
    slot_sink: dict[str, str],
    force: bool = False,
    nav: Any = None,
    turn_id: int = 0,
) -> dict[str, Any]:
    """跑 inspect-session 并写入 session_block（含 guest/logout 钳制）。

    已有明确结论时默认不重跑（登录用例每 turn 再调一次 LLM 会把步数预算烧光）。
    """
    from mino_nexus.runtime.session_gate import (
        ensure_case_scene,
        format_required_session_brief,
        reconcile_inspect_session,
        required_session as required_session_enum,
    )

    if not shot or not getattr(shot, "has_image", lambda: False)():
        slot_sink["session_block"] = "（无截图，跳过会话观察）"
        return {"ok": False, "reason": "无截图"}
    existing = str(slot_sink.get("session_block") or "")
    if not force and _session_block_is_conclusive(existing):
        return {"ok": True, "skipped": True, "reason": "already_observed"}
    nodes: list[Any] = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
    if nav is not None:
        snap = getattr(nav, "snapshot", None)
        if snap is not None:
            nodes = list(getattr(snap, "nodes", None) or [])
    prog = program_session_block_from_hierarchy(ctx, nodes, case=case)
    scene = ensure_case_scene(case, getattr(ctx, "case_scene", None))
    ctx.case_scene = scene
    req_enum = required_session_enum(scene=scene)
    if prog and (not force or "in_app_legal_consent" in prog):
        slot_sink["session_block"] = _append_required_to_program_block(prog, required=req_enum)
        return {"ok": True, "program": True, "reason": "hierarchy_session"}
    af_ctx = str(getattr(ctx, "app_foreground", "") or "").strip().lower()
    if af_ctx == "no":
        _clear_foreign_session_block(slot_sink)
        return {"ok": True, "program": True, "reason": "probe_foreign_skip"}
    if af_ctx not in ("yes", "") and not nodes:
        slot_sink["session_block"] = ""
        return {"ok": True, "program": True, "reason": "foreground_unknown_skip"}
    row = inspect_session(
        required_session=format_required_session_brief(scene),
        knowledge_hint=str(slot_sink.get("knowledge_hint") or ""),
        accounts_brief=str(getattr(ctx, "accounts_brief", "") or ""),
        image_base64=str(getattr(shot, "image_base64", "") or ""),
        image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
        screen_w=int(getattr(shot, "width", 0) or 0),
        screen_h=int(getattr(shot, "height", 0) or 0),
        provider_id=provider_id or None,
    )
    row = reconcile_inspect_session(row, required=req_enum)
    slot_sink["session_block"] = format_session_block(row, required=req_enum)
    from mino_nexus.loop.observe.session_persist import stamp_session_observation

    stamp_session_observation(ctx, row)
    layout = row.get("screen_layout") if isinstance(row.get("screen_layout"), dict) else {}
    if nav is not None and layout:
        nav.attach_turn_layout(int(turn_id or 0), layout)
    vlm_h = row.get("vlm_hierarchy") if isinstance(row.get("vlm_hierarchy"), dict) else {}
    if vlm_h.get("nodes") and nav is not None:
        nav.attach_turn_vlm_hierarchy(int(turn_id or 0), vlm_h)
        setattr(ctx, "nav_vlm_hierarchy", vlm_h)
    return row


def _session_block_is_conclusive(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw or raw.startswith("（"):
        return False
    if "session=" not in raw:
        return False
    session = ""
    for bit in raw.split():
        if bit.startswith("session="):
            session = bit.split("=", 1)[-1].strip().lower()
            break
    return session in ("logged_in", "guest")


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
            continue
        # 其它 observe job 后续按同一模式扩展


def _log_inspection(
    *,
    mark: str,
    job_id: str,
    result: dict[str, Any] | None = None,
    session_block: str = "",
) -> None:
    try:
        from mino_nexus.loop.observe.session_log import active_writer

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
    from mino_nexus.services.doc_match import match_step_docs as _match

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


def match_stuck_docs(
    *,
    ctx,
    case: dict[str, Any],
    cursor,
    history: list[str],
    steps: list[dict[str, Any]],
    hierarchy_text: str = "",
) -> str:
    """遇阻文档检索：屏文案权重更高。"""
    from mino_nexus.ai.knowledge_hint import build_case_intent, build_query, build_step_focus
    from mino_nexus.services.doc_match import match_stuck_docs as _stuck

    app_id, _ = resolve_knowledge_scope(ctx)
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
        history="\n".join(history[-4:]) if history else "",
        screen="",
    )
    query_vec = getattr(ctx, "doc_query_vec", None)
    if not isinstance(query_vec, list):
        query_vec = None
    try:
        block = _stuck(
            query,
            app_id=app_id,
            screen_text=hierarchy_text,
            query_vec=query_vec,
        )
    except Exception as exc:
        _log_doc_match(error=f"stuck:{type(exc).__name__}: {exc}", app_id=app_id)
        return ""
    if block:
        _log_doc_match(app_id=app_id, query=f"stuck:{query[:200]}")
    return block


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
        from mino_nexus.loop.observe.session_log import active_writer

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
        from mino_nexus.loop.observe.session_log import active_writer

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
