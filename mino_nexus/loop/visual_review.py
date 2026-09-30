"""视觉步当回合只记已派发。下一回合用节点证据回看，通过才记 pass。"""
from __future__ import annotations

from typing import Any

_REVIEW_CAPS = frozenset({"tap_element", "input_text"})
_INTERNAL_CAPS = frozenset({
    "lease_account",
    "get_otp",
    "get_phone",
    "clear_app_cache",
    "wait_ms",
    "wait_screen_ready",
    "confirm_login_state",
    "release_account",
    "relogin",
})


def _row_caps(row: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("device_cap", "hook_cap", "cap"):
        val = str(row.get(key) or "").strip()
        if val:
            out.add(val)
    return out


def defer_visual_pass(
    cursor: Any,
    *,
    capability_id: str,
    params: dict[str, Any] | None,
    summary: str,
    writer: Any = None,
    ctx: Any = None,
) -> bool:
    """看图的点击本回合即成功。看图的输入仍等下一回合读回框内文字。DOM 以本次节点命中为准。"""
    cap = str(capability_id or "").strip()
    if cap in _INTERNAL_CAPS or cap not in _REVIEW_CAPS:
        return False
    from mino_nexus.action_space.scheme import action_scheme

    scheme = action_scheme(ctx)
    if cap == "tap_element" or scheme == "dom":
        return False
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
    from mino_nexus.loop.milestones import read_state

    row = in_progress_milestone(read_state(cursor))
    if not isinstance(row, dict):
        return False
    declared = _row_caps(row)
    if declared and cap not in declared:
        return False
    mid = str(row.get("id") or "")
    kept = dict(params or {})
    pending = {
        "milestone_id": mid,
        "capability_id": cap,
        "params": kept,
        "summary": str(summary or "")[:400],
        "landed_labels": _landed_at_dispatch(ctx, cap, kept, str(summary or "")),
    }
    setattr(cursor, "pending_visual_review", pending)
    if writer:
        writer.append(
            "review/dispatched",
            {"milestone_id": mid, "capability_id": cap, "summary": pending["summary"]},
        )
    return True


def settle_pending_review(
    cursor: Any,
    ctx: Any,
    *,
    writer: Any = None,
    in_prep: bool = False,
    in_check: bool = False,
) -> str:
    """下一回合一开始回看。返回 stop / pass / fail / none。"""
    pending = getattr(cursor, "pending_visual_review", None)
    if not isinstance(pending, dict) or not pending.get("milestone_id"):
        return "none"
    mid = str(pending.get("milestone_id") or "")
    cap = str(pending.get("capability_id") or "")
    params = pending.get("params") if isinstance(pending.get("params"), dict) else {}
    summary = str(pending.get("summary") or "")
    ok, reason, label = _evidence(
        cursor,
        ctx,
        mid,
        cap,
        params,
        summary,
        pending.get("landed_labels") if isinstance(pending.get("landed_labels"), list) else [],
    )
    setattr(cursor, "pending_visual_review", None)
    if ok:
        _clear_streak(cursor, mid)
        setattr(cursor, "semantic_stop", "")
        from mino_nexus.loop.milestones import note_tool_pass_milestone

        noted = dict(params)
        if cap == "tap_element" and label and not str(noted.get("selector_text") or "").strip():
            noted["selector_text"] = label
        note_tool_pass_milestone(
            cursor,
            capability_id=cap,
            milestone_id=mid,
            writer=writer,
            params=noted,
            tool_summary=summary,
            ctx=ctx,
        )
        from mino_nexus.loop.step_phase_fsm import try_transition_after_tool_pass

        try_transition_after_tool_pass(
            cursor, ctx, writer=writer, in_prep=in_prep, in_check=in_check
        )
        if writer:
            writer.append(
                "review/pass",
                {"milestone_id": mid, "capability_id": cap, "reason": reason, "label": label},
            )
        return "pass"
    _rewind(cursor, mid)
    streak = _bump_streak(cursor, mid)
    hint = _hint(cursor, mid, label or reason)
    setattr(cursor, "correction_hint", hint)
    if writer:
        writer.append(
            "review/fail",
            {
                "milestone_id": mid,
                "capability_id": cap,
                "reason": reason,
                "label": label,
                "streak": streak,
            },
        )
        if reason in ("exclude", "unlisted"):
            writer.append(
                "semantic/reject",
                {"milestone_id": mid, "reason": reason, "label": label, "streak": streak},
            )
    if streak >= 2:
        setattr(cursor, "semantic_stop", f"{hint} 连续 {streak} 次回看未通过，已停止。")
        return "stop"
    return "fail"


def _landed_at_dispatch(
    ctx: Any,
    cap: str,
    params: dict[str, Any],
    summary: str,
) -> list[str]:
    from mino_nexus.semantic.intent_surface import landed_labels

    labels = landed_labels(params, summary)
    if cap != "tap_element" or ctx is None:
        return labels
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    hit = _node_at_milli(nodes, params.get("x"), params.get("y"), ctx)
    if hit is None:
        return labels
    for label in node_text(hit):
        if label and label not in labels:
            labels.append(label)
    return labels


def _evidence(
    cursor: Any,
    ctx: Any,
    mid: str,
    cap: str,
    params: dict[str, Any],
    summary: str,
    landed: list[str] | None = None,
) -> tuple[bool, str, str]:
    from mino_nexus.loop.milestones import read_state

    row = _row_by_id(read_state(cursor), mid)
    if row is None:
        return False, "missing_milestone", ""
    if cap == "input_text":
        return _input_evidence(ctx, row, params, summary)
    if cap == "tap_element":
        nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
        return _tap_evidence(row, params, summary, landed or [], nodes)
    return True, "not_visual", ""


def _input_evidence(
    ctx: Any,
    row: dict[str, Any],
    params: dict[str, Any],
    summary: str,
) -> tuple[bool, str, str]:
    from mino_nexus.loop.channel_observation import attach_input_readback
    from mino_nexus.loop.flow_block_exit import milestone_device_exit_ok

    checked = attach_input_readback(ctx, params)
    ok, status, reason = milestone_device_exit_ok(
        row,
        capability_id="input_text",
        field=str(checked.get("field") or ""),
        summary=summary,
        params=checked,
    )
    got = str(checked.get("_field_value") or "")
    if ok and status == "pass" and reason == "field_value_readback":
        return True, reason, got[:80]
    if not ok:
        return False, reason or status or "input_unread", got[:80]
    return False, "input_unverified", got[:80]


def _tap_evidence(
    row: dict[str, Any],
    params: dict[str, Any],
    summary: str,
    landed: list[str],
    nodes: list[dict[str, Any]] | None = None,
) -> tuple[bool, str, str]:
    from mino_nexus.semantic.intent_surface import classify_label, landed_labels, row_has_surface

    if not row_has_surface(row):
        return True, "no_surface", ""
    labels: list[str] = []
    for label in list(landed) + landed_labels(params, summary):
        if label and label not in labels:
            labels.append(label)
    excluded = ""
    matched = ""
    for label in labels:
        kind, phrase = classify_label(label, row, nodes)
        if kind == "exclude" and not excluded:
            excluded = phrase or label
        elif kind == "match" and not matched:
            matched = phrase or label
    if excluded:
        return False, "exclude", excluded
    if matched:
        return True, "landed", matched
    return False, "unlisted", labels[0] if labels else ""


def node_text(node: dict[str, Any]) -> list[str]:
    from mino_nexus.semantic.intent_surface import node_labels

    return node_labels(node)


def _node_at_milli(nodes: list[dict[str, Any]], x: Any, y: Any, ctx: Any) -> dict[str, Any] | None:
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return None
    wh = getattr(ctx, "viewport_wh", None) or (0, 0)
    width, height = int(wh[0] or 0), int(wh[1] or 0)
    if width <= 0 or height <= 0 or fx > 1000 or fy > 1000:
        px, py = fx, fy
    else:
        px, py = fx / 1000.0 * width, fy / 1000.0 * height
    from mino_nexus.loop.ui_consent import _bounds, _size

    best: dict[str, Any] | None = None
    best_area = 0
    for node in nodes:
        if node.get("clickable") is False:
            continue
        bounds = _bounds(node)
        if len(bounds) < 4:
            continue
        if not (bounds[0] <= px <= bounds[2] and bounds[1] <= py <= bounds[3]):
            continue
        w, h = _size(bounds)
        area = w * h
        if area <= 0:
            continue
        if best is None or area < best_area:
            best = node
            best_area = area
    return best


def _row_by_id(state: dict[str, Any], milestone_id: str) -> dict[str, Any] | None:
    for row in state.get("milestones") or []:
        if isinstance(row, dict) and str(row.get("id") or "") == milestone_id:
            return row
    return None


def _rewind(cursor: Any, milestone_id: str) -> None:
    from mino_nexus.loop.milestones import read_state, write_state

    state = read_state(cursor)
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    for row in ms:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "")
        status = str(row.get("status") or "pending").strip().lower()
        if status == "skipped":
            continue
        if rid == milestone_id:
            row["status"] = "in_progress"
        elif status == "in_progress":
            row["status"] = "pending"
    state["milestones"] = ms
    write_state(cursor, state)


def _streaks(cursor: Any) -> dict[str, int]:
    raw = getattr(cursor, "semantic_reject_streak", None)
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v or 0) for k, v in raw.items()}


def _bump_streak(cursor: Any, milestone_id: str) -> int:
    streaks = _streaks(cursor)
    streaks[milestone_id] = int(streaks.get(milestone_id) or 0) + 1
    setattr(cursor, "semantic_reject_streak", streaks)
    return streaks[milestone_id]


def _clear_streak(cursor: Any, milestone_id: str) -> None:
    streaks = _streaks(cursor)
    streaks.pop(milestone_id, None)
    setattr(cursor, "semantic_reject_streak", streaks)


def _hint(cursor: Any, milestone_id: str, label: str) -> str:
    from mino_nexus.loop.milestones import read_state

    row = _row_by_id(read_state(cursor), milestone_id) or {}
    allow = "、".join(str(x) for x in (row.get("match") or [])[:8])
    deny = "、".join(str(x) for x in (row.get("exclude") or [])[:8])
    if allow or deny:
        return f"{milestone_id} 只能点 {allow}。不要点 {deny}。这次是「{label or '未知'}」。"
    return f"{milestone_id} 回看未通过（{label or '没有独立证据'}）。"
