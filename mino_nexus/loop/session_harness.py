"""Session Log Harness：Eval / Audit / Replay / Fork 投影与计划。"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.session_project import project_turns
from mino_nexus.services import session_store


def _events(session_id: str) -> list[dict[str, Any]]:
    return session_store.read_events(session_id, limit=2000)


def _cap_in_menus(turns: list[dict[str, Any]], cap: str) -> list[int]:
    hits: list[int] = []
    needle = str(cap or "").strip()
    if not needle:
        return hits
    for t in turns:
        flags = t.get("menu_flags") or {}
        caps = flags.get("cap_ids") or []
        if needle in caps:
            hits.append(int(t.get("turn") or 0))
    return hits


def _stuck_turn(turns: list[dict[str, Any]], overall: str) -> int | None:
    if overall in ("pass", "done"):
        return None
    for t in reversed(turns):
        te = t.get("turn_end") or {}
        st = str(te.get("decision_status") or t.get("outcome_status") or "").lower()
        if st in ("fail", "failed", "blocked", "declined", "give_up"):
            return int(t.get("turn") or 0)
        if t.get("decisions"):
            return int(t.get("turn") or 0)
        for tool in t.get("tools") or []:
            if tool.get("kind") == "result":
                rs = str(tool.get("status") or "").lower()
                if rs in ("fail", "failed", "declined", "blocked"):
                    return int(t.get("turn") or 0)
    if turns:
        return int(turns[-1].get("turn") or 0)
    return None


def _invalid_capability_count(events: list[dict[str, Any]]) -> int:
    n = 0
    for row in events:
        typ = str(row.get("type") or "")
        payload = dict(row.get("payload") or {})
        if typ == "tool/result":
            st = str(payload.get("status") or "").lower()
            err = str(payload.get("error") or payload.get("summary") or "").lower()
            if st == "declined" or "cap_not_in_catalog" in err or "not in catalog" in err:
                n += 1
        elif typ == "guard/block":
            n += 1
    return n


def project_eval(
    session_id: str,
    *,
    expect: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Eval Harness：从 session log 聚合 benchmark 指标。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    meta = session_store.get_meta(sid)
    turns_doc = project_turns(sid)
    if meta is None and not turns_doc:
        return None

    events = _events(sid)
    turns = (turns_doc or {}).get("turns") or []
    overall = str((turns_doc or {}).get("overall") or (meta or {}).get("status") or "")

    llm_rows = [e for e in events if e.get("type") == "llm/response"]
    tool_calls = [e for e in events if e.get("type") == "tool/call"]
    recovery = [e for e in events if e.get("type") == "recovery/match"]
    guards = [e for e in events if e.get("type") == "guard/block"]
    decisions = [e for e in events if str(e.get("type") or "").startswith("decision/")]

    tokens = sum(int((e.get("payload") or {}).get("total_tokens") or 0) for e in llm_rows)
    recovery_menu_turns = [
        int(t.get("turn") or 0)
        for t in turns
        if (t.get("menu_flags") or {}).get("recovery_in_menu")
    ]

    stuck = _stuck_turn(turns, overall)
    metrics: dict[str, Any] = {
        "session_id": sid,
        "task_success": overall == "pass",
        "status": overall,
        "step_count": len(tool_calls),
        "turn_count": len(turns),
        "llm_calls": len(llm_rows),
        "llm_tokens": tokens,
        "tool_calls": len(tool_calls),
        "invalid_tool_calls": _invalid_capability_count(events),
        "guard_blocks": len(guards),
        "recovery_hits": len(recovery),
        "recovery_in_menu_turns": recovery_menu_turns,
        "decision_events": len(decisions),
        "stuck_turn": stuck,
        "event_count": len(events),
        "menu_caps": {},
    }

    exp = dict(expect or {})
    caps_watch = exp.get("tools_must_include") or exp.get("menu_must_include") or []
    if isinstance(caps_watch, str):
        caps_watch = [caps_watch]
    for cap in caps_watch:
        c = str(cap or "").strip()
        if c:
            metrics["menu_caps"][c] = _cap_in_menus(turns, c)

    checks: dict[str, Any] = {}
    if exp:
        if "status" in exp:
            checks["status"] = overall == str(exp["status"])
        if "max_steps" in exp:
            try:
                checks["max_steps"] = len(tool_calls) <= int(exp["max_steps"])
            except (TypeError, ValueError):
                checks["max_steps"] = None
        if "max_llm_tokens" in exp:
            try:
                checks["max_llm_tokens"] = tokens <= int(exp["max_llm_tokens"])
            except (TypeError, ValueError):
                checks["max_llm_tokens"] = None
        for cap in caps_watch:
            c = str(cap or "").strip()
            if c:
                checks[f"menu_has_{c}"] = bool(metrics["menu_caps"].get(c))
        # §9 的门槛也能当断言用；指标为 None（没样本）时记 None 而不是 False —— 没测过不算不通过
        nav_metrics = metrics.get("nav") or {}
        for key, cmp_fn in (
            ("min_edge_success_rate", lambda got, want: got >= want),
            ("max_guard_fp", lambda got, want: got <= want),
            ("max_guard_fn", lambda got, want: got <= want),
        ):
            if key not in exp:
                continue
            field = {"min_edge_success_rate": "edge_success_rate",
                     "max_guard_fp": "guard_fp", "max_guard_fn": "guard_fn"}[key]
            got = nav_metrics.get(field)
            try:
                checks[key] = None if got is None else cmp_fn(float(got), float(exp[key]))
            except (TypeError, ValueError):
                checks[key] = None

    # NavFSM 指标（设计稿 §9）。没跑导航图的 session 这里全是 0 / None，不影响原有字段。
    from mino_nexus.services.nav_telemetry import aggregate_session

    metrics["nav"] = aggregate_session(sid, events=events)

    metrics["expect"] = exp
    metrics["checks"] = checks
    metrics["passed"] = all(v is True for v in checks.values()) if checks else None
    return metrics


def project_audit(session_id: str) -> dict[str, Any] | None:
    """审计证据链：回答「为何 blocked/fail」。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    meta = session_store.get_meta(sid)
    turns_doc = project_turns(sid)
    if meta is None and not turns_doc:
        return None

    events = _events(sid)
    turns = (turns_doc or {}).get("turns") or []
    overall = str((turns_doc or {}).get("overall") or (meta or {}).get("status") or "")
    summary = str((turns_doc or {}).get("summary") or (meta or {}).get("summary") or "")

    chain: list[dict[str, Any]] = []
    for row in events:
        typ = str(row.get("type") or "")
        if typ in (
            "session/start",
            "session/end",
            "inspection/done",
            "decision/ask_human",
            "decision/give_up",
            "guard/block",
            "recovery/match",
            "hitl/blocked",
            "turn/end",
            "tool/result",
        ):
            payload = dict(row.get("payload") or {})
            st = str(payload.get("status") or "").lower()
            if typ == "tool/result" and st not in ("fail", "failed", "declined", "blocked"):
                continue
            chain.append({
                "seq": row.get("seq"),
                "ts": row.get("ts"),
                "type": typ,
                "turn": row.get("turn"),
                "phase": row.get("phase"),
                "payload": payload,
            })

    findings: list[str] = []
    if overall == "blocked":
        for t in turns:
            for d in t.get("decisions") or []:
                if d.get("type") == "decision/ask_human":
                    findings.append(f"Turn {t.get('turn')}: 模型请求人工 — {d.get('thought') or '无说明'}")
            for g in t.get("guards") or []:
                findings.append(
                    f"Turn {t.get('turn')}: Guard 拦截 {g.get('capability_id') or '?'} — {g.get('reason') or ''}"
                )
    elif overall in ("fail", "failed"):
        stuck = _stuck_turn(turns, overall)
        if stuck is not None:
            t = next((x for x in turns if int(x.get("turn") or 0) == stuck), None)
            if t:
                te = t.get("turn_end") or {}
                if te.get("decision_cap"):
                    findings.append(f"Turn {stuck}: 决策 {te.get('decision_cap')} → {te.get('decision_status')}")
                for tool in t.get("tools") or []:
                    if tool.get("kind") == "result" and str(tool.get("status") or "").lower() in ("fail", "failed"):
                        findings.append(
                            f"Turn {stuck}: 工具 {tool.get('capability_id')} 失败 — {tool.get('summary') or tool.get('error') or ''}"
                        )
                if t.get("think", {}).get("thought"):
                    findings.append(f"Turn {stuck} 思考: {t['think']['thought'][:200]}")

    insp = next((e for e in events if e.get("type") == "inspection/done"), None)
    if insp:
        p = dict(insp.get("payload") or {})
        if p.get("session_block"):
            findings.append(f"巡检 session_block: {str(p.get('session_block'))[:240]}")

    return {
        "session_id": sid,
        "status": overall,
        "summary": summary,
        "meta": meta,
        "findings": findings,
        "evidence_chain": chain,
        "stuck_turn": _stuck_turn(turns, overall),
        "turns": turns,
    }


def _history_at_turn(turns: list[dict[str, Any]], turn: int) -> list[str]:
    for t in turns:
        if int(t.get("turn") or 0) == turn:
            lines = t.get("history_lines")
            if isinstance(lines, list) and lines:
                return [str(x) for x in lines]
    nxt = turn + 1
    for t in turns:
        if int(t.get("turn") or 0) == nxt:
            lines = t.get("history_lines")
            if isinstance(lines, list):
                return [str(x) for x in lines]
    return []


def _cursor_hint_at_turn(turns: list[dict[str, Any]], turn: int) -> dict[str, Any]:
    for t in turns:
        if int(t.get("turn") or 0) == turn:
            slots = t.get("slots") if isinstance(t.get("slots"), dict) else {}
            return {
                "phase": t.get("phase") or t.get("cursor_phase") or slots.get("phase") or "do",
                "case_step": slots.get("case_step"),
                "goal": slots.get("goal") or "",
            }
    return {"phase": "do"}


def build_replay_plan(
    session_id: str,
    *,
    up_to_turn: int | None = None,
) -> dict[str, Any] | None:
    """Replay 计划：同样输入重跑（整案或验证到 turn N 的上下文）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    meta = session_store.get_meta(sid)
    turns_doc = project_turns(sid)
    if meta is None:
        return None

    events = _events(sid)
    start = next((e for e in events if e.get("type") == "session/start"), None)
    start_payload = dict((start or {}).get("payload") or {})
    turns = (turns_doc or {}).get("turns") or []

    cap = int(up_to_turn) if up_to_turn is not None else None
    frozen_turns = [t for t in turns if cap is None or int(t.get("turn") or 0) <= cap]

    return {
        "mode": "replay",
        "parent_session_id": sid,
        "run_id": meta.get("run_id") or "",
        "case_id": meta.get("case_id") or "",
        "app_id": meta.get("app_id") or "",
        "up_to_turn": cap,
        "session_start": start_payload,
        "frozen_turns": frozen_turns,
        "goal": (turns_doc or {}).get("goal") or start_payload.get("goal") or "",
        "note": "整案 replay 重新 run_case；up_to_turn 用于 A/B 对比该 turn 前的输入快照",
    }


def build_fork_plan(
    session_id: str,
    *,
    from_turn: int,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Fork 计划：从 turn/end 之后切开，可换 provider / 策略继续。"""
    sid = str(session_id or "").strip()
    ft = max(1, int(from_turn or 1))
    meta = session_store.get_meta(sid)
    turns_doc = project_turns(sid)
    if meta is None or not turns_doc:
        return None

    turns = turns_doc.get("turns") or []
    cut = next((t for t in turns if int(t.get("turn") or 0) == ft), None)
    if cut is None:
        return None

    events = _events(sid)
    ovr = dict(overrides or {})
    history = _history_at_turn(turns, ft)
    cursor_hint = _cursor_hint_at_turn(turns, ft)

    return {
        "mode": "fork",
        "parent_session_id": sid,
        "fork_from_turn": ft,
        "run_id": meta.get("run_id") or "",
        "case_id": meta.get("case_id") or "",
        "app_id": meta.get("app_id") or "",
        "history_lines": history,
        "cursor_hint": cursor_hint,
        "turn_end_at_cut": cut.get("turn_end"),
        "menu_at_cut": cut.get("menu"),
        "menu_flags_at_cut": cut.get("menu_flags"),
        "slots_at_cut": cut.get("slots"),
        "overrides": {
            "provider_id": str(ovr.get("provider_id") or start_provider(events) or ""),
            "model": str(ovr.get("model") or ""),
            "tool_kinds": ovr.get("tool_kinds"),
            "notes": str(ovr.get("notes") or ""),
        },
    }


def start_provider(events: list[dict[str, Any]]) -> str:
    start = next((e for e in events if e.get("type") == "session/start"), None)
    return str((start or {}).get("payload", {}).get("provider_id") or "")


def harvest_sessions(
    *,
    app_id: str = "",
    limit: int = 20,
    expect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Benchmark harvest：批量 session → eval 指标。"""
    items, total = session_store.list_sessions(app_id=app_id, limit=max(1, min(100, limit)))
    rows: list[dict[str, Any]] = []
    for meta in items:
        sid = str(meta.get("session_id") or "")
        ev = project_eval(sid, expect=expect)
        if ev:
            rows.append(ev)
    passed = sum(1 for r in rows if r.get("passed") is True)
    return {
        "app_id": app_id,
        "total": total,
        "harvested": len(rows),
        "passed": passed,
        "failed_harvest": len(rows) - passed if expect else None,
        "items": rows,
    }


def fork_state_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """agent_loop 可用的 fork 注入状态。"""
    hint = dict(plan.get("cursor_hint") or {})
    return {
        "parent_session_id": str(plan.get("parent_session_id") or ""),
        "fork_from_turn": int(plan.get("fork_from_turn") or 1),
        "history": list(plan.get("history_lines") or []),
        "phase": str(hint.get("phase") or "do"),
        "provider_id": str((plan.get("overrides") or {}).get("provider_id") or ""),
    }
