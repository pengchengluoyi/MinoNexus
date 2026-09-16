"""Atlas 多态 VLM 判定（M4）：same_page_morph | split_page，仅 pin/split，不改 state_id 自动合并。"""
from __future__ import annotations

import base64
import json
from typing import Any

from mino_nexus.services import nav_capture_store as capture


def _wireframe_brief(wf: dict[str, Any] | None) -> str:
    if not isinstance(wf, dict):
        return "{}"
    regions = wf.get("regions") or []
    keys = []
    for r in regions[:24]:
        if not isinstance(r, dict):
            continue
        rect = r.get("rect") or {}
        keys.append(
            {
                "role": str(r.get("role") or r.get("source") or "")[:24],
                "label": str(r.get("label") or "")[:32],
                "rect": {k: round(float(rect.get(k) or 0), 3) for k in ("x", "y", "w", "h")},
            }
        )
    return json.dumps({"region_count": len(regions), "sample": keys}, ensure_ascii=False)


def _screen_b64(app_id: str, session_id: str, turn_id: int) -> tuple[str, str]:
    path = capture.screen_path(app_id, session_id, int(turn_id))
    if not path.is_file():
        return "", ""
    raw = path.read_bytes()
    if not raw:
        return "", ""
    return base64.b64encode(raw).decode("ascii"), "image/jpeg"


def judge_atlas_morph(
    *,
    app_id: str,
    session_id: str,
    turn_id: int,
    peer_turn_id: int,
    state_id: str = "",
    display_name: str = "",
    provider_id: str | None = None,
    timeout_sec: int = 45,
) -> dict[str, Any]:
    """返回 {ok, verdict, confidence, reason, apply_hint}。"""
    from mino_nexus.ai.llm_client import call_chat_text, resolve_regression_provider
    from mino_nexus.ai.prompt_render import JobRenderError, render

    empty: dict[str, Any] = {
        "ok": False,
        "verdict": "",
        "confidence": 0.0,
        "reason": "",
        "apply_hint": "",
    }
    turn_b = capture.read_turn(app_id, session_id, int(turn_id))
    turn_a = capture.read_turn(app_id, session_id, int(peer_turn_id))
    if not turn_b or not turn_a:
        empty["reason"] = "采集 turn 不存在"
        return empty
    img_b, mime = _screen_b64(app_id, session_id, int(turn_id))
    if not img_b:
        empty["reason"] = "缺少截图"
        return empty
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        empty["reason"] = f"未启用 AI：{gate.get('reason')}"
        return empty
    ctx = {
        "state_id": str(state_id or ""),
        "display_name": str(display_name or ""),
        "turn_a": int(peer_turn_id),
        "turn_b": int(turn_id),
        "wireframe_a": _wireframe_brief(turn_a.get("layout_wireframe")),
        "wireframe_b": _wireframe_brief(turn_b.get("layout_wireframe")),
        "localized_a": json.dumps(turn_a.get("localized") or {}, ensure_ascii=False)[:800],
        "localized_b": json.dumps(turn_b.get("localized") or {}, ensure_ascii=False)[:800],
    }
    slots = {
        "context_json": json.dumps(ctx, ensure_ascii=False, indent=2),
        "image_base64": img_b,
        "image_mime": mime,
    }
    try:
        messages, job_meta = render("nav-atlas-morph", slots)
    except JobRenderError as exc:
        empty["reason"] = str(exc)
        return empty
    try:
        text = call_chat_text(
            messages,
            provider=provider,
            timeout_sec=timeout_sec,
            json_mode=True,
            **dict(job_meta.get("call") or {}),
        )
    except Exception as exc:  # noqa: BLE001
        empty["reason"] = f"VLM 调用失败：{exc}"
        return empty
    try:
        raw = json.loads(text or "{}")
    except json.JSONDecodeError:
        empty["reason"] = "模型输出非 JSON"
        return empty
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in ("same_page_morph", "split_page"):
        empty["reason"] = f"无效 verdict={verdict!r}"
        return empty
    conf = float(raw.get("confidence") or 0.0)
    reason = str(raw.get("reason") or "").strip()[:500]
    hint = "pin" if verdict == "same_page_morph" else "split"
    return {
        "ok": True,
        "verdict": verdict,
        "confidence": max(0.0, min(1.0, conf)),
        "reason": reason,
        "apply_hint": hint,
        "peer_turn_id": int(peer_turn_id),
        "turn_id": int(turn_id),
    }


def apply_atlas_morph_verdict(
    app_id: str,
    *,
    session_id: str,
    turn_id: int,
    state_id: str,
    verdict: str,
    project_id: str = "",
    updated_by: str = "",
    display_name: str = "",
) -> dict[str, Any]:
    from mino_nexus.services import nav_screen_registry as reg

    v = str(verdict or "").strip().lower()
    if v == "same_page_morph":
        return reg.save_atlas_capture_pin(
            app_id,
            session_id=session_id,
            turn_id=int(turn_id),
            state_id=state_id,
            project_id=project_id,
            updated_by=updated_by,
        )
    if v == "split_page":
        return reg.save_atlas_capture_split(
            app_id,
            session_id=session_id,
            turn_id=int(turn_id),
            display_name=display_name,
            project_id=project_id,
            updated_by=updated_by,
        )
    return {"ok": False, "reason": f"未知 verdict={verdict}"}
