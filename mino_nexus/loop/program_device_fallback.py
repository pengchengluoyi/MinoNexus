"""程序链 Web 设备步 DOM 无坐标时，交给看图执行（vision exec）兜底。"""
from __future__ import annotations

from typing import Any


def vision_fallback_result(
    *,
    capability_id: str,
    params: dict[str, Any],
    reason: str,
    milestone_id: str = "",
    block_id: str = "",
    step_id: str = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "need_vision_fallback": True,
        "status": "defer",
        "capability_id": str(capability_id or "input_text"),
        "fallback_params": dict(params or {}),
        "fallback_reason": str(reason or "no_web_coords"),
        "milestone_id": milestone_id,
        "block_id": block_id,
        "step_id": step_id or milestone_id,
        "summary": "程序链未解析到 Web 坐标，转看图执行",
    }


def is_vision_fallback(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("need_vision_fallback"))


def _fallback_hint(payload: dict[str, Any]) -> str:
    cap = str(payload.get("capability_id") or "input_text")
    params = dict(payload.get("fallback_params") or {})
    field = str(params.get("field") or "").strip()
    fld = f"field={field}" if field else "带坐标"
    return (
        f"程序链未能从 DOM 解析点击坐标（{payload.get('fallback_reason') or 'no_web_coords'}）；"
        f"本轮须看图执行 {cap}({fld})，勿空转 get_otp/重复发码。"
    )


def note_vision_fallback_streak(cursor: Any, payload: dict[str, Any]) -> int:
    """同一里程碑连续拿不到坐标的次数。换了里程碑就从头计。"""
    mid = str((payload or {}).get("milestone_id") or (payload or {}).get("step_id") or "")
    cap = str((payload or {}).get("capability_id") or "input_text")
    key = f"{mid}:{cap}"
    prev = getattr(cursor, "vision_fallback_streak", None)
    if not isinstance(prev, dict) or prev.get("key") != key:
        prev = {"key": key, "n": 0}
    prev["n"] = int(prev.get("n") or 0) + 1
    setattr(cursor, "vision_fallback_streak", prev)
    return int(prev["n"])


def activate_program_vision_fallback(
    cursor: Any,
    writer: Any,
    payload: dict[str, Any],
) -> None:
    fb = {
        "capability_id": str(payload.get("capability_id") or "input_text"),
        "params": dict(payload.get("fallback_params") or {}),
        "reason": str(payload.get("fallback_reason") or "no_web_coords"),
        "milestone_id": str(payload.get("milestone_id") or payload.get("step_id") or ""),
    }
    setattr(cursor, "program_vision_fallback", fb)
    hint = _fallback_hint({**payload, "fallback_params": fb["params"]})
    prev = str(getattr(cursor, "correction_hint", "") or "").strip()
    cursor.correction_hint = f"{prev} {hint}".strip() if prev else hint
    if writer is not None:
        try:
            writer.append("program/vision_fallback", fb)
        except Exception:
            pass


def merge_program_vision_fallback_slots(cursor: Any, inspect_slots: dict[str, Any]) -> None:
    fb = getattr(cursor, "program_vision_fallback", None)
    if not isinstance(fb, dict) or not fb:
        return
    params = dict(fb.get("params") or {})
    cap = str(fb.get("capability_id") or "input_text")
    field = str(params.get("field") or "")
    text = str(params.get("text") or "")
    line = (
        "【程序链→看图兜底】须在本轮 vision 执行 "
        f"{cap}(field={field or '?'})"
        + (f"，text 已备好" if text else "")
        + "；params 必须含有效 x/y。"
    )
    assist = str(inspect_slots.get("nav_assist") or "").strip()
    inspect_slots["nav_assist"] = f"{assist}\n{line}".strip() if assist else line


def clear_program_vision_fallback(cursor: Any) -> None:
    if hasattr(cursor, "program_vision_fallback"):
        try:
            delattr(cursor, "program_vision_fallback")
        except Exception:
            setattr(cursor, "program_vision_fallback", None)


__all__ = [
    "activate_program_vision_fallback",
    "clear_program_vision_fallback",
    "is_vision_fallback",
    "merge_program_vision_fallback_slots",
    "note_vision_fallback_streak",
    "vision_fallback_result",
]
