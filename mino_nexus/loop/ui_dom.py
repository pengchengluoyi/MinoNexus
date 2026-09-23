"""Web DOM 节点（`accessibility_json` 形态，`class` 为 HTML tag / role）。

与 `ui_sms_request` 的安卓 EditText 几何分离；共用 `ui_consent` 的 bounds/同行判定。
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
)

_PHONE_DIGITS_RE = re.compile(r"^1\d{10}$")
_EMAIL_AT_RE = re.compile(r"@")
_MIN_FIELD_WIDTH_RATIO = 0.22
_DOM_INPUT_RE = re.compile(
    r"^(input|textarea|select)\b|htmlinput|htmltextarea|contenteditable|type:email|type:text|type=tel",
    re.I,
)


def _is_dom_text_input(node: dict[str, Any]) -> bool:
    cls = str(node.get("class") or "").lower()
    if _DOM_INPUT_RE.search(cls):
        return True
    if bool(node.get("editable")):
        return True
    role = str(node.get("role") or node.get("content_desc") or "").lower()
    return role in ("textbox", "searchbox", "combobox")


def _node_input_type_email(node: dict[str, Any]) -> bool:
    cls = str(node.get("class") or "").lower()
    blob = f"{cls} {_label_text(node)}".lower()
    return "type:email" in cls or "type=email" in cls or "email" in blob or "邮箱" in blob


def find_dom_phone_field(nodes: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    pool = [n for n in (nodes or []) if isinstance(n, dict)]
    if not pool:
        return None
    sw, sh = _screen_wh(pool)
    min_w = int(sw * _MIN_FIELD_WIDTH_RATIO) if sw > 0 else 160
    ranked: list[tuple[int, dict[str, Any]]] = []
    for node in pool:
        if not _is_dom_text_input(node):
            continue
        b = _bounds(node)
        w, h = _size(b)
        if w < min_w or h <= 0:
            continue
        raw = _label_text(node).replace(" ", "").replace("-", "")
        score = min(40, w // max(1, min_w // 4))
        if _PHONE_DIGITS_RE.match(raw):
            score += 90
        if "tel" in str(node.get("class") or "").lower():
            score += 30
        from mino_nexus.loop.hierarchy_slots import node_flag

        if node_flag(node, "focused"):
            score += 20
        ranked.append((score, node))
    if not ranked:
        return None
    ranked.sort(key=lambda row: row[0], reverse=True)
    return ranked[0][1]


def find_dom_email_field(nodes: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    pool = [n for n in (nodes or []) if isinstance(n, dict)]
    if not pool:
        return None
    sw, sh = _screen_wh(pool)
    min_w = int(sw * _MIN_FIELD_WIDTH_RATIO) if sw > 0 else 160
    ranked: list[tuple[int, dict[str, Any]]] = []
    for node in pool:
        if not _is_dom_text_input(node):
            continue
        b = _bounds(node)
        w, h = _size(b)
        if w < min_w or h <= 0:
            continue
        label = _label_text(node)
        score = min(40, w // max(1, min_w // 4))
        if _EMAIL_AT_RE.search(label):
            score += 100
        if _node_input_type_email(node):
            score += 60
        low = label.lower()
        if "email" in low or "mail" in low:
            score += 40
        from mino_nexus.loop.hierarchy_slots import node_flag

        if node_flag(node, "focused"):
            score += 20
        ranked.append((score, node))
    if not ranked:
        return None
    ranked.sort(key=lambda row: row[0], reverse=True)
    best_score, best = ranked[0]
    if best_score < 25:
        return None
    return best


def dom_phone_field_filled(nodes: list[dict[str, Any]]) -> bool:
    field = find_dom_phone_field(nodes)
    if field is None:
        return False
    raw = _label_text(field).replace(" ", "").replace("-", "")
    return bool(_PHONE_DIGITS_RE.match(raw))


def dom_email_field_filled(nodes: list[dict[str, Any]]) -> bool:
    field = find_dom_email_field(nodes)
    if field is None:
        return False
    return bool(_EMAIL_AT_RE.search(_label_text(field)))


_OTP_HINT_RE = re.compile(r"验证码|verification|one.?time|otp|code", re.I)
_OTP_DIGITS_RE = re.compile(r"^\d{0,8}$")


def find_dom_otp_field(
    nodes: list[dict[str, Any]],
    *,
    email_field: dict[str, Any] | None = None,
) -> Optional[dict[str, Any]]:
    """Web 验证码/OTP 输入框（常与邮箱框同弹窗、在其下方）。"""
    pool = [n for n in (nodes or []) if isinstance(n, dict)]
    if not pool:
        return None
    email = email_field if email_field is not None else find_dom_email_field(pool)
    sw, sh = _screen_wh(pool)
    min_w = int(sw * _MIN_FIELD_WIDTH_RATIO) if sw > 0 else 160
    ranked: list[tuple[int, dict[str, Any]]] = []
    for node in pool:
        if not _is_dom_text_input(node):
            continue
        if email is not None and node is email:
            continue
        b = _bounds(node)
        w, h = _size(b)
        if w < min_w or h <= 0:
            continue
        label = _label_text(node)
        if _EMAIL_AT_RE.search(label):
            continue
        cls = str(node.get("class") or "").lower()
        blob = f"{cls} {label}".lower()
        score = min(30, w // max(1, min_w // 4))
        if "one-time" in cls or "otp" in blob or _OTP_HINT_RE.search(label):
            score += 90
        if str(node.get("type") or "").lower() in ("tel", "number", "text"):
            score += 15
        if _OTP_DIGITS_RE.match(label.replace(" ", "")):
            score += 25
        from mino_nexus.loop.hierarchy_slots import node_flag

        if node_flag(node, "focused"):
            score += 40
        if email is not None:
            eb = _bounds(email)
            _, ecy = _center(eb)
            _, ncy = _center(b)
            if ncy > ecy + 8:
                score += 45
            if _same_row(node, email):
                score -= 20
        ranked.append((score, node))
    if not ranked:
        return None
    ranked.sort(key=lambda row: row[0], reverse=True)
    best_score, best = ranked[0]
    if best_score < 35:
        return None
    return best


def find_dom_text_input_below(
    nodes: list[dict[str, Any]],
    anchor: dict[str, Any] | None,
) -> Optional[dict[str, Any]]:
    """锚点（常为邮箱框）下方最近的 DOM 文本输入，用于 OTP 等第二输入框。"""
    pool = [n for n in (nodes or []) if isinstance(n, dict)]
    if not pool:
        return None
    below: list[tuple[int, dict[str, Any]]] = []
    acy = _center(_bounds(anchor))[1] if anchor is not None else -1
    sw, sh = _screen_wh(pool)
    min_w = int(sw * _MIN_FIELD_WIDTH_RATIO) if sw > 0 else 160
    for node in pool:
        if not _is_dom_text_input(node):
            continue
        if anchor is not None and node is anchor:
            continue
        b = _bounds(node)
        if _size(b)[0] < min_w:
            continue
        _, ncy = _center(b)
        if anchor is not None and ncy <= acy + 6:
            continue
        below.append((ncy, node))
    if not below:
        return None
    below.sort(key=lambda row: row[0])
    return below[0][1]
