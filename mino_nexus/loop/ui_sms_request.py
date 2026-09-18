"""短信验证码请求按钮：只认布局，不猜 App 固定文案。

常见形态：宽手机号 EditText，右侧同一行一个可点的短文案控件。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from mino_nexus.loop.ui_consent import (
    _bounds,
    _center,
    _label_text,
    _same_row,
    _screen_wh,
    _size,
    is_long_label,
    is_small_control,
    tap_params_for_control,
)

_PHONE_DIGITS_RE = re.compile(r"^1\d{10}$")
_ACTION_TEXT_MAX = 16
_ACTION_TEXT_MIN = 1
_MIN_PHONE_FIELD_WIDTH_RATIO = 0.28
_ROW_RIGHT_SLACK_PX = 32


def _is_edittext(node: dict[str, Any]) -> bool:
    cls = str(node.get("class") or "").lower()
    return "edittext" in cls or bool(node.get("editable"))


def _phone_digits_in_node(node: dict[str, Any]) -> bool:
    raw = _label_text(node).replace(" ", "").replace("-", "")
    return bool(_PHONE_DIGITS_RE.match(raw))


def find_phone_field(nodes: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """当前屏最像手机号输入框的 EditText（宽、可含 11 位数字）。"""
    pool = [n for n in (nodes or []) if isinstance(n, dict)]
    if not pool:
        return None
    sw, sh = _screen_wh(pool)
    min_w = int(sw * _MIN_PHONE_FIELD_WIDTH_RATIO) if sw > 0 else 200
    ranked: list[tuple[int, dict[str, Any]]] = []
    for node in pool:
        if not _is_edittext(node):
            continue
        b = _bounds(node)
        w, h = _size(b)
        if w < min_w or h <= 0:
            continue
        score = min(40, w // max(1, min_w // 4))
        if _phone_digits_in_node(node):
            score += 80
        from mino_nexus.loop.hierarchy_slots import node_flag

        if node_flag(node, "focused"):
            score += 25
        cy = _center(b)[1]
        if sh > 0 and cy < sh * 0.72:
            score += 10
        ranked.append((score, node))
    if not ranked:
        return None
    ranked.sort(key=lambda row: row[0], reverse=True)
    return ranked[0][1]


def _is_send_action_chip(node: dict[str, Any], *, sw: int, sh: int) -> bool:
    if not node.get("clickable"):
        return False
    text = _label_text(node)
    if len(text) < _ACTION_TEXT_MIN or len(text) > _ACTION_TEXT_MAX:
        return False
    if is_long_label(node):
        return False
    if is_small_control(node, sw=sw, sh=sh):
        from mino_nexus.loop.hierarchy_slots import node_flag

        if node_flag(node, "checkable"):
            return False
    b = _bounds(node)
    _, h = _size(b)
    if sh > 0 and h > max(96, int(sh * 0.14)):
        return False
    return True


def _action_right_of_phone(action: dict[str, Any], phone: dict[str, Any]) -> bool:
    pb = _bounds(phone)
    ab = _bounds(action)
    if ab[0] >= pb[2] - _ROW_RIGHT_SLACK_PX:
        return True
    pcx, _ = _center(pb)
    acx, _ = _center(ab)
    return acx > pcx + (pb[2] - pb[0]) * 0.15


def find_send_code_button(
    nodes: list[dict[str, Any]],
    phone: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """手机号输入框同行、偏右的可点短文案控件。"""
    pool = [n for n in (nodes or []) if isinstance(n, dict)]
    if not pool or phone is None:
        return None
    sw, sh = _screen_wh(pool)
    best: tuple[int, dict[str, Any]] | None = None
    for node in pool:
        if node is phone or not _is_send_action_chip(node, sw=sw, sh=sh):
            continue
        if not _same_row(node, phone):
            continue
        if not _action_right_of_phone(node, phone):
            continue
        ab = _bounds(node)
        acx, _ = _center(ab)
        pb = _bounds(phone)
        score = 50 + int(acx - pb[2])
        if _label_text(node):
            score += 10
        if best is None or score > best[0]:
            best = (score, node)
    if best is not None:
        return best[1]
    return _find_send_button_below_or_inline(pool, phone, sw=sw, sh=sh)


def _find_send_button_below_or_inline(
    pool: list[dict[str, Any]],
    phone: dict[str, Any],
    *,
    sw: int,
    sh: int,
) -> Optional[dict[str, Any]]:
    """同行几何未命中：发送钮在输入框下方一行或嵌在同一容器右侧。"""
    pb = _bounds(phone)
    pcx, pcy = _center(pb)
    best: tuple[int, dict[str, Any]] | None = None
    max_below = max(140, int(sh * 0.08)) if sh > 0 else 140
    for node in pool:
        if node is phone or not _is_send_action_chip(node, sw=sw, sh=sh):
            continue
        ab = _bounds(node)
        acx, acy = _center(ab)
        if acy < pcy - 20:
            continue
        if acy > pb[3] + max_below:
            continue
        if acx < pcx - (pb[2] - pb[0]) * 0.2:
            continue
        score = 40
        if _same_row(node, phone):
            score += 30
        if acx >= pb[2] - _ROW_RIGHT_SLACK_PX:
            score += 25
        if acy > pb[3] and acy < pb[3] + max_below:
            score += 20
        if best is None or score > best[0]:
            best = (score, node)
    return None if best is None else best[1]


def phone_field_filled(nodes: list[dict[str, Any]]) -> bool:
    phone = find_phone_field(nodes)
    return phone is not None and _phone_digits_in_node(phone)


def tap_params_for_send_button(node: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return tap_params_for_control(node, nodes)
