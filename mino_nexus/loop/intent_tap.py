"""当前里程碑的 match 在树上只有一个整词命中时，直接点它，不打看图。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.ai.schemas import AgentAction, AgentDecision
from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
from mino_nexus.loop.milestones import read_state
from mino_nexus.semantic.intent_surface import classify_node, row_has_surface


def tap_surface_blocks_pass(
    cursor: Any,
    row: dict[str, Any],
    *,
    params: dict[str, Any] | None,
    summary: str,
    writer: Any = None,
    ctx: Any = None,
) -> bool:
    """DOM 落点落在排除表时不记 pass。看图点下去即成功，不查这张表。"""
    from mino_nexus.action_space.scheme import action_scheme

    if action_scheme(ctx) != "dom":
        return False
    if not row_has_surface(row):
        return False
    from mino_nexus.semantic.intent_surface import classify_label, landed_labels

    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    labels = landed_labels(params, summary)
    excluded = ""
    matched = ""
    for label in labels:
        kind, phrase = classify_label(label, row, nodes)
        if kind == "exclude" and not excluded:
            excluded = phrase or label
        elif kind == "match" and not matched:
            matched = phrase or label
    if matched and not excluded:
        streaks = dict(getattr(cursor, "semantic_reject_streak", None) or {})
        streaks.pop(str(row.get("id") or ""), None)
        setattr(cursor, "semantic_reject_streak", streaks)
        return False
    mid = str(row.get("id") or "")
    reason = "exclude" if excluded else "unlisted"
    label = excluded or (labels[0] if labels else "")
    streaks = dict(getattr(cursor, "semantic_reject_streak", None) or {})
    streaks[mid] = int(streaks.get(mid) or 0) + 1
    setattr(cursor, "semantic_reject_streak", streaks)
    allow = "、".join(str(x) for x in (row.get("match") or [])[:8])
    deny = "、".join(str(x) for x in (row.get("exclude") or [])[:8])
    hint = f"{mid} 只能点 {allow}。不要点 {deny}。这次是「{label or '未知'}」。"
    if cursor is not None:
        cursor.correction_hint = hint
    if writer:
        writer.append(
            "semantic/reject",
            {
                "milestone_id": mid,
                "reason": reason,
                "label": label,
                "streak": streaks[mid],
            },
        )
    if streaks[mid] >= 2 and cursor is not None:
        setattr(cursor, "semantic_stop", f"{hint} 连续 {streaks[mid]} 次，已停止。")
    return True


def dom_intent_tap_decision(cursor: Any, ctx: Any, writer: Any = None) -> Optional[AgentDecision]:
    row = in_progress_milestone(read_state(cursor))
    if not isinstance(row, dict) or not row_has_surface(row):
        return None
    kind = str(row.get("kind") or "")
    device = str(row.get("device_cap") or row.get("cap") or "")
    if kind not in ("visual_tap", "visual_action") and device != "tap_element":
        return None
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    hits: list[tuple[dict[str, Any], str]] = []
    for node in nodes:
        if node.get("clickable") is False:
            continue
        kind, phrase = classify_node(node, row, nodes)
        if kind == "match":
            hits.append((node, phrase))
    mid = str(row.get("id") or "")
    if len(hits) != 1:
        if writer and hits:
            writer.append(
                "semantic/ambiguous",
                {"milestone_id": mid, "count": len(hits), "labels": [h[1] for h in hits]},
            )
        return None
    node, phrase = hits[0]
    from mino_nexus.loop.ui_consent import tap_params_for_control

    params = tap_params_for_control(node, nodes)
    params["selector_text"] = phrase
    params["text"] = phrase
    params["target"] = {"text": phrase, "content_desc": phrase}
    params["intent_dom"] = True
    if writer:
        writer.append(
            "semantic/dom",
            {"milestone_id": mid, "label": phrase, "x": params.get("x"), "y": params.get("y")},
        )
    return AgentDecision(
        status="continue",
        thought=f"树上只有一个「{phrase}」，按 {mid} 的可点表直接点",
        action=AgentAction(capability_id="tap_element", params=params),
    )


def dom_checkbox_tap_decision(
    cursor: Any, ctx: Any, writer: Any = None
) -> tuple[bool, Optional[AgentDecision]]:
    """勾选只在 DOM 路线按勾选框角色 + 文案唯一命中。不是勾选步时 applicable 为假。"""
    row = in_progress_milestone(read_state(cursor))
    if not isinstance(row, dict):
        return False, None
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    element = params.get("element") if isinstance(params.get("element"), dict) else {}
    rules = params.get("rules") if isinstance(params.get("rules"), dict) else {}
    if str(element.get("role") or "") != "checkbox":
        return False, None
    label = str(rules.get("label") or "").strip()
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    hits: list[dict[str, Any]] = []
    for node in nodes:
        cls = str(node.get("class") or "").lower()
        role = str(node.get("role") or "").lower()
        checkable = bool(node.get("checkable")) or "checkbox" in cls or role == "checkbox"
        if not checkable:
            continue
        blob = f"{node.get('text') or ''} {node.get('content_desc') or ''}"
        if label and label not in blob:
            continue
        hits.append(node)
    mid = str(row.get("id") or "")
    if len(hits) != 1:
        if writer:
            writer.append("semantic/miss", {"milestone_id": mid, "role": "checkbox", "count": len(hits)})
        return True, None
    node = hits[0]
    from mino_nexus.loop.ui_consent import tap_params_for_control

    tap = tap_params_for_control(node, nodes)
    phrase = label or str(node.get("text") or node.get("content_desc") or "")
    tap["selector_text"] = phrase
    tap["text"] = phrase
    tap["target"] = {"text": phrase, "content_desc": phrase}
    tap["intent_dom"] = True
    if writer:
        writer.append("semantic/dom", {"milestone_id": mid, "label": phrase, "role": "checkbox"})
    return True, AgentDecision(
        status="continue",
        thought=f"树上只有一个勾选框「{phrase}」",
        action=AgentAction(capability_id="tap_element", params=tap),
    )
