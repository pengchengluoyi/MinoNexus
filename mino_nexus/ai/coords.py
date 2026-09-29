# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Agent 决策坐标：出站 EXECUTE 保持 0–1000 千分比，由 Scout 按分辨率换像素。

Nexus 在派单前不得把千分比换成像素，否则 Scout 会再换算一次（Web 1280×800 上必偏）。
"""
from __future__ import annotations

from typing import Any, Optional

_MILLI = 1000.0
_PAIRS = (("x", "y"), ("from_x", "from_y"), ("to_x", "to_y"))


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clamp_px(fv: float, dim: int) -> int:
    return max(0, min(dim, int(round(fv))))


def _one_to_px(v: Any, dim: int) -> Any:
    fv = _as_float(v)
    if fv is None or dim <= 0:
        return v
    px = fv if fv > _MILLI else (fv / _MILLI * dim)
    return _clamp_px(px, dim)


def _clamp_milli(v: Any) -> Any:
    fv = _as_float(v)
    if fv is None:
        return v
    return max(0, min(int(_MILLI), int(round(fv))))


def _px_to_milli(v: Any, dim: int) -> Any:
    fv = _as_float(v)
    if fv is None or dim <= 0:
        return v
    return max(0, min(int(_MILLI), int(round(fv / dim * _MILLI))))


def prepare_xy_params_for_execute(params: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """规范化 agent 决策坐标，供 EXECUTE 发给 Scout（协议要求 0–1000 千分比）。"""
    if not isinstance(params, dict):
        return params
    for kx, ky in _PAIRS:
        if kx not in params and ky not in params:
            continue
        xv = _as_float(params.get(kx)) if kx in params else None
        yv = _as_float(params.get(ky)) if ky in params else None
        pair_is_px = (xv is not None and xv > _MILLI) or (yv is not None and yv > _MILLI)
        if pair_is_px:
            if kx in params:
                params[kx] = _px_to_milli(params[kx], width)
            if ky in params:
                params[ky] = _px_to_milli(params[ky], height)
        else:
            if kx in params:
                params[kx] = _clamp_milli(params[kx])
            if ky in params:
                params[ky] = _clamp_milli(params[ky])
    return params


def milli_to_viewport_px(x: Any, y: Any, width: int, height: int) -> Optional[tuple[int, int]]:
    """与 Scout `to_viewport_xy` 同一规则。

    两边都在 0–1000 且视口宽 > 1000 时，按千分比换成像素；否则视为已经是像素。
    设备回执里的坐标是换算后的像素，不要再套一次本函数。
    """
    xv, yv = _as_float(x), _as_float(y)
    if xv is None or yv is None or width <= 0 or height <= 0:
        return None
    xi, yi = int(round(xv)), int(round(yv))
    if 0 <= xi <= int(_MILLI) and 0 <= yi <= int(_MILLI) and width > int(_MILLI):
        xi = int(round(xi / _MILLI * width))
        yi = int(round(yi / _MILLI * height))
    return max(0, min(width - 1, xi)), max(0, min(height - 1, yi))


def landing_matches_sent(
    sent_x: Any,
    sent_y: Any,
    pixel_x: Any,
    pixel_y: Any,
    *,
    width: int,
    height: int,
    tol_px: int = 28,
) -> bool:
    """派单千分比换成视口像素后，和设备回执像素比。回执不再换算。"""
    sent = milli_to_viewport_px(sent_x, sent_y, width, height)
    rx, ry = _as_float(pixel_x), _as_float(pixel_y)
    if sent is None or rx is None or ry is None:
        return False
    tol = max(0, int(tol_px))
    return abs(sent[0] - int(round(rx))) <= tol and abs(sent[1] - int(round(ry))) <= tol


def apply_xy_params(params: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """千分比 → 截图像素。仅用于 Nexus 本地消费；**不要**在 EXECUTE 出站前调用。"""
    if not isinstance(params, dict):
        return params
    for kx, ky in _PAIRS:
        if kx not in params and ky not in params:
            continue
        xv = _as_float(params.get(kx)) if kx in params else None
        yv = _as_float(params.get(ky)) if ky in params else None
        pair_is_px = (xv is not None and xv > _MILLI) or (yv is not None and yv > _MILLI)
        if kx in params and width > 0:
            fv = _as_float(params[kx])
            params[kx] = _clamp_px(fv, width) if pair_is_px and fv is not None else _one_to_px(params[kx], width)
        if ky in params and height > 0:
            fv = _as_float(params[ky])
            params[ky] = _clamp_px(fv, height) if pair_is_px and fv is not None else _one_to_px(params[ky], height)
    return params


def lift_selector_target(params: dict[str, Any]) -> dict[str, Any]:
    """selector_text / description 写入 params.target，Scout 才能用层级点到控件。"""
    if not isinstance(params, dict):
        return params
    target = dict(params.get("target") or {}) if isinstance(params.get("target"), dict) else {}
    if any(str(target.get(k) or "").strip() for k in ("text", "content_desc", "resource_id", "id")):
        return params
    label = ""
    for key in ("selector_text", "description", "label", "text"):
        s = str(params.get(key) or "").strip().strip("「」\"'")
        if s:
            label = s
            break
    if not label:
        return params
    target.setdefault("text", label)
    target.setdefault("content_desc", label)
    params["target"] = target
    return params
