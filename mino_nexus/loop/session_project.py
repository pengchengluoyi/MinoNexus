"""Session Event Log 投影：供 Studio / eval 读取。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services import session_store


def _turn_sidecars(
    events: list[dict[str, Any]],
) -> tuple[dict[int, str], dict[int, dict], dict[int, dict], list[dict[str, Any]]]:
    """按 turn 索引 dispatch_id、context/slots、context/menu、inspection 列表。"""
    dispatch: dict[int, str] = {}
    slots: dict[int, dict[str, Any]] = {}
    menus: dict[int, dict[str, Any]] = {}
    inspections: list[dict[str, Any]] = []
    for row in events:
        typ = str(row.get("type") or "")
        turn = int(row.get("turn") or 0)
        payload = dict(row.get("payload") or {})
        if typ == "llm/response" and turn:
            did = str(payload.get("dispatch_id") or "").strip()
            if did:
                dispatch[turn] = did
        elif typ == "context/slots" and turn:
            body = payload.get("slots") if isinstance(payload.get("slots"), dict) else payload
            slots[turn] = dict(body or {})
        elif typ == "context/menu" and turn:
            menus[turn] = dict(payload or {})
        elif typ == "inspection/done":
            inspections.append({
                "turn": turn,
                "seq": row.get("seq"),
                "ts": row.get("ts") or "",
                **payload,
            })
    return dispatch, slots, menus, inspections


def _enrich_ui_event(
    payload: dict[str, Any],
    *,
    turn: int,
    dispatch: dict[int, str],
    slots: dict[int, dict[str, Any]],
    menus: dict[int, dict[str, Any]],
    inspections: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(payload or {})
    if turn <= 0:
        return out
    did = dispatch.get(turn)
    if did and not out.get("dispatch_id"):
        out["dispatch_id"] = did
    slot_row = slots.get(turn)
    if slot_row and not out.get("context_slots"):
        out["context_slots"] = slot_row
    menu_row = menus.get(turn)
    if menu_row and not out.get("context_menu"):
        out["context_menu"] = menu_row
        out["menu_flags"] = _menu_flags(menu_row)
    turn_inspections = [x for x in inspections if int(x.get("turn") or 0) == turn]
    if turn_inspections and not out.get("inspections"):
        out["inspections"] = turn_inspections
    return out


def _menu_flags(menu: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(menu or {})
    cap_ids = [str(c) for c in (payload.get("cap_ids") or []) if c]
    tool_kinds = [str(k) for k in (payload.get("tool_kinds") or []) if k]
    recovery_caps = [c for c in cap_ids if c.startswith("recover_")]
    return {
        "cap_ids": cap_ids,
        "tool_kinds": tool_kinds,
        "menu_count": len(cap_ids),
        "recovery_in_menu": bool(recovery_caps) or "recovery" in tool_kinds,
        "recovery_caps": recovery_caps,
        "ask_human_in_menu": "signal_ask_human" in cap_ids,
    }


def _turn_bucket(turns: dict[int, dict[str, Any]], turn: int) -> dict[str, Any]:
    n = int(turn or 0)
    if n not in turns:
        turns[n] = {
            "turn": n,
            "phase": "",
            "ts_start": "",
            "menu": None,
            "menu_flags": None,
            "slots": None,
            "llm_calls": [],
            "decisions": [],
            "guards": [],
            "recovery": [],
            "inspections": [],
            "tools": [],
            "turn_end": None,
            "think": None,
            "stream": [],
        }
    return turns[n]


def project_turns(session_id: str) -> dict[str, Any] | None:
    """按 turn 聚合 debug 轨迹：menu / slots / LLM / 决策 / 工具链。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    meta = session_store.get_meta(sid)
    events = session_store.read_events(sid, limit=2000)
    if meta is None and not events:
        return None

    turns: dict[int, dict[str, Any]] = {}
    pending_llm: dict[int, dict[str, Any]] = {}

    for row in events:
        typ = str(row.get("type") or "")
        turn = int(row.get("turn") or 0)
        phase = str(row.get("phase") or "")
        payload = dict(row.get("payload") or {})
        ts = str(row.get("ts") or "")

        if typ == "inspection/done" and turn <= 0:
            bucket = _turn_bucket(turns, 0)
            bucket["inspections"].append({"ts": ts, **payload})
            continue

        if turn <= 0 and typ not in ("session/start", "session/end"):
            if typ in ("recovery/match", "tool/call", "tool/result"):
                bucket = _turn_bucket(turns, 0)
            else:
                continue
        else:
            bucket = _turn_bucket(turns, turn)

        if phase and not bucket.get("phase"):
            bucket["phase"] = phase
        if ts and not bucket.get("ts_start"):
            bucket["ts_start"] = ts

        if typ == "turn/start":
            bucket["history_lines"] = payload.get("history_lines") or []
            bucket["cursor_phase"] = payload.get("cursor_phase") or phase
        elif typ == "context/menu":
            bucket["menu"] = payload
            bucket["menu_flags"] = _menu_flags(payload)
        elif typ == "context/slots":
            body = payload.get("slots") if isinstance(payload.get("slots"), dict) else payload
            bucket["slots"] = dict(body or {})
        elif typ == "llm/request":
            pending_llm[turn] = {
                "job_id": payload.get("job_id") or "",
                "messages": payload.get("messages") or [],
                "ts": ts,
            }
        elif typ == "llm/response":
            item = {
                "job": payload.get("job") or "",
                "dispatch_id": payload.get("dispatch_id") or "",
                "model": payload.get("model") or "",
                "status": payload.get("status") or "",
                "elapsed_ms": payload.get("elapsed_ms") or 0,
                "total_tokens": payload.get("total_tokens") or 0,
                "error": payload.get("error") or "",
                "output_hash": payload.get("output_hash") or "",
                "ts": ts,
            }
            req = pending_llm.pop(turn, None)
            if req and req.get("job_id") == item.get("job"):
                item["request"] = req
            bucket["llm_calls"].append(item)
        elif typ in ("decision/ask_human", "decision/give_up"):
            bucket["decisions"].append({"type": typ, "ts": ts, **payload})
        elif typ == "guard/block":
            bucket["guards"].append({"ts": ts, **payload})
        elif typ == "recovery/match":
            bucket["recovery"].append({"ts": ts, **payload})
        elif typ == "inspection/done":
            bucket["inspections"].append({"ts": ts, **payload})
        elif typ == "turn/end":
            bucket["turn_end"] = {"ts": ts, **payload}
        elif typ == "tool/call":
            bucket["tools"].append({"kind": "call", "ts": ts, **payload})
        elif typ == "tool/result":
            bucket["tools"].append({"kind": "result", "ts": ts, **payload})
        elif typ == "stream/emit":
            phase_name = str(payload.get("phase") or "")
            slim = {
                "phase": phase_name,
                "ts": ts,
                "thought": payload.get("thought") or "",
                "status": payload.get("status") or "",
                "capability_id": payload.get("capability_id") or "",
                "dispatch_id": payload.get("dispatch_id") or "",
                "summary": payload.get("summary") or "",
            }
            bucket["stream"].append(slim)
            if phase_name == "think" and not bucket.get("think"):
                bucket["think"] = slim
        elif typ == "phase/change":
            bucket.setdefault("phase_changes", []).append({"ts": ts, **payload})

    ordered = [turns[k] for k in sorted(turns.keys())]
    for row in ordered:
        te = row.get("turn_end") or {}
        row["outcome_cap"] = te.get("decision_cap") or ""
        row["outcome_status"] = te.get("decision_status") or ""
        if not row.get("think") and row.get("stream"):
            for s in row["stream"]:
                if s.get("phase") == "think":
                    row["think"] = s
                    break

    start = next((e for e in events if e.get("type") == "session/start"), None)
    end = next((e for e in reversed(events) if e.get("type") == "session/end"), None)
    return {
        "session_id": sid,
        "meta": meta,
        "turns": ordered,
        "turn_count": len(ordered),
        "source": "session_log",
        "goal": str((start or {}).get("payload", {}).get("goal") or ""),
        "overall": (end or {}).get("payload", {}).get("status") or (meta or {}).get("status") or "",
        "summary": (end or {}).get("payload", {}).get("summary") or (meta or {}).get("summary") or "",
    }


def project_trajectory(session_id: str) -> dict[str, Any] | None:
    """投影为 agent_stream 兼容形状；优先用于 Studio 回放。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    meta = session_store.get_meta(sid)
    events = session_store.read_events(sid, limit=2000)
    if meta is None and not events:
        return None

    dispatch, slots_by_turn, menus_by_turn, inspections = _turn_sidecars(events)
    stream_rows = [e for e in events if e.get("type") == "stream/emit"]
    start = next((e for e in events if e.get("type") == "session/start"), None)
    end = next((e for e in reversed(events) if e.get("type") == "session/end"), None)

    case_id = (meta or {}).get("case_id") or (start or {}).get("payload", {}).get("case_id") or ""
    goal = str((start or {}).get("payload", {}).get("goal") or "")
    skill_id = ""
    view_id = ""
    slots: dict[str, Any] = {}

    ui_events: list[dict[str, Any]] = []
    if stream_rows:
        for row in stream_rows:
            payload = dict(row.get("payload") or {})
            turn = int(payload.get("step") or row.get("turn") or 0)
            payload = _enrich_ui_event(
                payload,
                turn=turn,
                dispatch=dispatch,
                slots=slots_by_turn,
                menus=menus_by_turn,
                inspections=inspections,
            )
            if payload.get("goal") and not goal:
                goal = str(payload.get("goal") or "")
            if payload.get("skill_id"):
                skill_id = str(payload.get("skill_id") or "")
            if payload.get("view_id"):
                view_id = str(payload.get("view_id") or "")
            if isinstance(payload.get("slots"), dict):
                slots = payload.get("slots") or slots
            ui_events.append(payload)
    else:
        ui_events = _structured_to_ui(
            events,
            dispatch=dispatch,
            slots_by_turn=slots_by_turn,
            menus_by_turn=menus_by_turn,
            inspections=inspections,
        )

    status = (end or {}).get("payload", {}).get("status") or (meta or {}).get("status") or ""
    summary = (end or {}).get("payload", {}).get("summary") or (meta or {}).get("summary") or ""
    finished = status not in ("", "running")

    return {
        "run_id": sid,
        "session_id": sid,
        "case_id": case_id,
        "goal": goal,
        "events": ui_events,
        "overall": status,
        "summary": summary,
        "finished": finished,
        "skill_id": skill_id,
        "view_id": view_id,
        "slots": slots,
        "event_count": len(events),
        "source": "session_log",
        "llm_calls": project_llm_calls(sid),
        "inspections": [
            dict(row.get("payload") or {})
            for row in events
            if str(row.get("type") or "") == "inspection/done"
        ],
    }


def _structured_to_ui(
    events: list[dict[str, Any]],
    *,
    dispatch: dict[int, str] | None = None,
    slots_by_turn: dict[int, dict[str, Any]] | None = None,
    menus_by_turn: dict[int, dict[str, Any]] | None = None,
    inspections: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """无 stream/emit 时，用 structured event 拼简易 UI 时间线。"""
    dispatch = dispatch or {}
    slots_by_turn = slots_by_turn or {}
    menus_by_turn = menus_by_turn or {}
    inspections = inspections or []
    out: list[dict[str, Any]] = []
    for row in events:
        typ = str(row.get("type") or "")
        payload = dict(row.get("payload") or {})
        turn = int(row.get("turn") or 0)
        phase = str(row.get("phase") or "")
        base: dict[str, Any] | None = None
        if typ == "turn/start":
            base = {"phase": "think", "step": turn, "loop_phase": phase, "thought": "turn start"}
        elif typ == "observe/screen":
            base = {
                "phase": "observe",
                "step": turn,
                "loop_phase": phase,
                "thumb": payload.get("thumb") or payload.get("thumb_ref") or "",
            }
        elif typ == "tool/call":
            base = {
                "phase": "step",
                "step": turn,
                "loop_phase": phase,
                "capability_id": payload.get("capability_id") or "",
                "action": payload,
            }
        elif typ == "tool/result":
            base = {
                "phase": "result",
                "step": turn,
                "loop_phase": phase,
                "capability_id": payload.get("capability_id") or "",
                "status": payload.get("status") or "",
                "summary": payload.get("summary") or "",
            }
        elif typ == "llm/response":
            base = {
                "phase": "think",
                "step": turn,
                "loop_phase": phase,
                "thought": payload.get("tool_name") or payload.get("job") or "llm",
                "dispatch_id": payload.get("dispatch_id") or "",
            }
        if base is not None:
            out.append(_enrich_ui_event(
                base,
                turn=turn,
                dispatch=dispatch,
                slots=slots_by_turn,
                menus=menus_by_turn,
                inspections=inspections,
            ))
    if events and str(events[-1].get("type") or "") == "session/end":
        end_payload = dict(events[-1].get("payload") or {})
        out.append({
            "phase": "done",
            "status": end_payload.get("status") or "",
            "summary": end_payload.get("summary") or "",
        })
    return out


def project_llm_calls(session_id: str) -> list[dict[str, Any]]:
    sid = str(session_id or "").strip()
    if not sid:
        return []
    rows = session_store.read_events(sid, limit=2000)
    out: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    for row in rows:
        typ = str(row.get("type") or "")
        payload = dict(row.get("payload") or {})
        if typ == "llm/request":
            pending = {
                "turn": row.get("turn"),
                "phase": row.get("phase") or "",
                "job_id": payload.get("job_id") or "",
                "messages": payload.get("messages") or [],
                "ts": row.get("ts") or "",
            }
        elif typ == "llm/response":
            item = {
                "turn": row.get("turn"),
                "phase": row.get("phase") or "",
                "dispatch_id": payload.get("dispatch_id") or "",
                "job": payload.get("job") or "",
                "model": payload.get("model") or "",
                "status": payload.get("status") or "",
                "elapsed_ms": payload.get("elapsed_ms") or 0,
                "total_tokens": payload.get("total_tokens") or 0,
                "error": payload.get("error") or "",
                "ts": row.get("ts") or "",
            }
            if pending and pending.get("job_id") == item.get("job"):
                item["request"] = pending
            out.append(item)
            pending = None
    return out


def project_metrics(session_id: str) -> dict[str, Any]:
    sid = str(session_id or "").strip()
    meta = session_store.get_meta(sid) or {}
    events = session_store.read_events(sid, limit=2000)
    llm_rows = [e for e in events if e.get("type") == "llm/response"]
    tool_calls = [e for e in events if e.get("type") == "tool/call"]
    tool_results = [e for e in events if e.get("type") == "tool/result"]
    recovery = [e for e in events if e.get("type") == "recovery/match"]
    guards = [e for e in events if e.get("type") == "guard/block"]
    inspection_rows = [e for e in events if e.get("type") == "inspection/done"]
    tokens = sum(int((e.get("payload") or {}).get("total_tokens") or 0) for e in llm_rows)
    failed_tools = sum(
        1
        for e in tool_results
        if str((e.get("payload") or {}).get("status") or "").lower() in {"fail", "failed", "declined", "blocked"}
    )
    end = next((e for e in reversed(events) if e.get("type") == "session/end"), None)
    task_status = (end or {}).get("payload", {}).get("status") or meta.get("status") or ""
    return {
        "session_id": sid,
        "task_success": task_status == "pass",
        "status": task_status,
        "step_count": len(tool_calls),
        "llm_calls": len(llm_rows),
        "llm_tokens": tokens,
        "tool_calls": len(tool_calls),
        "tool_failures": failed_tools,
        "recovery_hits": len(recovery),
        "guard_blocks": len(guards),
        "inspection_count": len(inspection_rows),
        "event_count": len(events),
    }
