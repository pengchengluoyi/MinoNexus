"""视觉载荷：只留坐标。不写 target，Scout 不得按文案改点，也不得吸附。"""
from __future__ import annotations

from typing import Any

_LABEL_KEYS = (
    "target",
    "selector_text",
    "description",
    "label",
    "content_desc",
    "anchor_between",
    "fallback_xy",
)


def apply_visual_point(params: dict[str, Any], *, keep_text: bool = False) -> dict[str, Any]:
    out = dict(params or {})
    for key in _LABEL_KEYS:
        out.pop(key, None)
    if not keep_text and "field" not in out:
        out.pop("text", None)
    out["point_policy"] = "coordinate"
    return out


def apply_focus_input(params: dict[str, Any]) -> dict[str, Any]:
    """看图输入只写入当前焦点。点击负责定位，输入不再带坐标、不再次点击。"""
    out = dict(params or {})
    for key in _LABEL_KEYS + (
        "x",
        "y",
        "point_policy",
        "web_coordinate_only",
        "tap_prefer_coordinates",
        "email_coords_from_prior_tap_only",
        "otp_coords_from_prior_tap_only",
    ):
        out.pop(key, None)
    mode = str(out.get("mode") or "replace").strip().lower()
    out["target_mode"] = "current_focus"
    out["mode"] = mode if mode in ("replace", "append") else "replace"
    out.pop("focus_ref", None)
    return out


def bind_focus_ref(params: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """previous_focus_action 只是别名。派发前必须换成这次会话里的具体点击。"""
    out = dict(params or {})
    if str(out.get("target_mode") or "") != "current_focus":
        return out
    ref = str(getattr(ctx, "focus_action_ref", "") or "").strip()
    run_id = str(getattr(ctx, "scout_run_id", "") or "")
    field = str(out.get("field") or "").strip().lower()
    focused_field = str(getattr(ctx, "focus_action_field", "") or "").strip().lower()
    aliases = {"email": "login_email", "sms_code": "otp"}
    mismatched = bool(field and focused_field and field != focused_field
                      and aliases.get(field) != focused_field and aliases.get(focused_field) != field)
    if not ref or not run_id or not ref.startswith(f"focus:{run_id}:") or mismatched:
        out["focus_ref"] = ""
        out["_focus_unresolved"] = True
        return out
    out["focus_ref"] = ref
    out.pop("_focus_unresolved", None)
    return out
