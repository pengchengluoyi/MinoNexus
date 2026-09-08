"""截图可读性：黑屏 / 无图检测（pillow 只做采样，不做识别）。"""
from __future__ import annotations

import base64
import io
from typing import Any, Optional

from mino_nexus.core.schemas import CapturedScreen

# 均值亮度低于此阈值视为黑屏（0–255）
_BLACK_MEAN_THRESHOLD = 12
_SAMPLE_MAX_SIDE = 64


def _yes_no(cond: Optional[bool]) -> str:
    if cond is None:
        return "unknown"
    return "yes" if cond else "no"


def analyze_shot(shot: Any) -> dict[str, str]:
    """返回 capture_ok / capture_black（yes|no|unknown）。"""
    if shot is None:
        return {"capture_ok": "no", "capture_black": "unknown"}
    has = bool(getattr(shot, "has_image", lambda: False)())
    if not has:
        err = str(getattr(shot, "error", "") or "").strip()
        return {
            "capture_ok": "no",
            "capture_black": "unknown",
            "capture_error": err[:120] if err else "",
        }
    try:
        w = int(getattr(shot, "width", 0) or 0)
        h = int(getattr(shot, "height", 0) or 0)
    except (TypeError, ValueError):
        w, h = 0, 0
    if w <= 0 or h <= 0:
        w, h = 1080, 1920
    raw = str(getattr(shot, "image_base64", "") or "").strip()
    if not raw:
        return {"capture_ok": "no", "capture_black": "unknown"}
    try:
        from PIL import Image

        data = base64.b64decode(raw, validate=False)
        img = Image.open(io.BytesIO(data)).convert("L")
        img.thumbnail((_SAMPLE_MAX_SIDE, _SAMPLE_MAX_SIDE))
        pixels = list(img.getdata())
        mean = sum(pixels) / max(len(pixels), 1)
        black = mean < _BLACK_MEAN_THRESHOLD
        return {"capture_ok": "yes", "capture_black": _yes_no(black)}
    except Exception:
        return {"capture_ok": "yes", "capture_black": "unknown"}


def shot_usable(shot: Any) -> bool:
    facts = analyze_shot(shot)
    return facts.get("capture_ok") == "yes" and facts.get("capture_black") != "yes"


def merge_shot_evidence(evidence: Any, shot: Any) -> None:
    """把截图信号写入 Evidence（原地）。"""
    facts = analyze_shot(shot)
    for key in ("capture_ok", "capture_black"):
        val = facts.get(key)
        if val in ("yes", "no"):
            setattr(evidence, key, val)
    if facts.get("capture_ok") == "no" and evidence.screen_blocked == "unknown":
        evidence.screen_blocked = "yes"


def capture_brief(shot: Any) -> str:
    f = analyze_shot(shot)
    return f"capture_ok={f.get('capture_ok')} capture_black={f.get('capture_black')}"
