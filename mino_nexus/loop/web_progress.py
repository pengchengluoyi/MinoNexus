"""Web 进展信号：不依赖「弹键盘式」的整屏指纹变化，以 activeElement 为主。"""
from __future__ import annotations

import hashlib
from typing import Any


def normalize_web_focus(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {
        "editable_ready": bool(raw.get("editable_ready")),
        "tag": str(raw.get("tag") or "").strip().lower(),
        "type": str(raw.get("type") or "").strip().lower(),
        "id": str(raw.get("id") or "").strip()[:64],
        "name": str(raw.get("name") or "").strip()[:64],
        "value_len": int(raw.get("value_len") or 0),
        "value": str(raw.get("value") or "")[:120],
        "reason": str(raw.get("reason") or "").strip()[:32],
    }
    bb = raw.get("bounds")
    if isinstance(bb, (list, tuple)) and len(bb) >= 4:
        try:
            out["bounds"] = [int(bb[0]), int(bb[1]), int(bb[2]), int(bb[3])]
        except (TypeError, ValueError):
            pass
    return out


def web_editable_focus_ready(ctx: Any | None) -> bool:
    focus = normalize_web_focus(getattr(ctx, "web_focus", None) if ctx is not None else None)
    return bool(focus.get("editable_ready"))


def web_focus_fingerprint(focus: Any) -> str:
    f = normalize_web_focus(focus)
    if not f.get("editable_ready"):
        return f"idle:{f.get('reason') or 'none'}"
    value = str(f.get("value") or "")
    value_fp = (
        hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:8]
        if value
        else f"len={f.get('value_len')}"
    )
    blob = (
        f"{f.get('tag')}|{f.get('type')}|{f.get('id')}|{f.get('name')}|{value_fp}"
    )
    return hashlib.sha1(blob.encode("utf-8", errors="ignore")).hexdigest()[:12]


def refresh_web_focus(ctx: Any, proxy: Any) -> dict[str, Any]:
    """拉一帧 hierarchy，把 Scout 上报的 web_focus 写入 ctx。"""
    out: dict[str, Any] = {}
    if proxy is None or ctx is None:
        return out
    try:
        shot = proxy.observe("hierarchy", force_fresh=True)
        detail = dict(getattr(shot, "remote_detail", None) or {})
        raw = detail.get("web_focus")
        if isinstance(raw, dict):
            out = normalize_web_focus(raw)
            setattr(ctx, "web_focus", out)
    except Exception:
        pass
    return out


def web_tap_coarse_key(cap_id: str, params: dict[str, Any] | None) -> str:
    """Web 熔断用：同文案/选择器连点，忽略坐标抖动。"""
    cid = str(cap_id or "").strip()
    p = dict(params or {})
    if cid == "tap_element":
        sel = str(p.get("selector_text") or p.get("text") or p.get("content_desc") or "").strip()[:48]
        return f"tap|{sel or 'coord'}"
    if cid == "input_text":
        field = str(p.get("field") or "text").strip().lower()
        return f"input|{field}"
    return cid
