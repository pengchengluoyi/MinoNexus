"""渠道观测：页面身份、进展、输入读回。

循环只调用这三个读数。安卓用控件树和布局角色；Web 用 URL、对话框结构和输入值。
Web 节点不进入安卓布局分类。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse

from mino_nexus.loop.hierarchy_slots import dom_structure_fingerprint
from mino_nexus.loop.step_pointer import screen_fingerprint
from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx
from mino_nexus.loop.web.web_progress import normalize_web_focus

_WEB_DIALOG_RE = re.compile(r"\b(dialog|alertdialog|modal)\b", re.I)
_WEB_FORM_RE = re.compile(r"^(input|textarea|select)\b|textbox|searchbox", re.I)
_DOM_INPUT_RE = re.compile(
    r"^(input|textarea|select)\b|htmlinput|htmltextarea|contenteditable|textbox",
    re.I,
)


def _sha(text: str, n: int = 12) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:n]


def _nodes(nodes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [n for n in (nodes or []) if isinstance(n, dict)]


def _page_url(ctx: Any, nodes: list[dict[str, Any]]) -> str:
    for node in nodes:
        for key in ("page_url", "url", "location"):
            val = str(node.get(key) or "").strip()
            if val.startswith("http://") or val.startswith("https://"):
                return val
    for attr in ("page_url", "target_package"):
        val = str(getattr(ctx, attr, "") or "").strip()
        if val.startswith("http://") or val.startswith("https://"):
            return val
    return ""


def _web_surface(nodes: list[dict[str, Any]]) -> tuple[bool, bool]:
    dialog = False
    form = False
    for node in nodes:
        blob = " ".join(
            str(node.get(k) or "")
            for k in ("class", "role", "resource_id")
        )
        if _WEB_DIALOG_RE.search(blob):
            dialog = True
        if _WEB_FORM_RE.search(blob):
            form = True
        if dialog and form:
            break
    return dialog, form


def web_page_key(ctx: Any, nodes: list[dict[str, Any]] | None) -> str:
    rows = _nodes(nodes)
    raw = _page_url(ctx, rows)
    parsed = urlparse(raw) if raw else None
    origin = ""
    path = ""
    if parsed is not None and parsed.scheme:
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        path = parsed.path or "/"
    dialog, form = _web_surface(rows)
    return f"web|{origin}|{path}|dialog={int(dialog)}|form={int(form)}"


def android_page_key(nodes: list[dict[str, Any]] | None) -> str:
    rows = _nodes(nodes)
    if not rows:
        return "android|empty"
    from mino_nexus.services.nav_layout import (
        detect_layout_framework,
        framework_fingerprint,
        stable_chrome_texts,
    )
    from mino_nexus.services.nav_screen_layout import infer_content_bands

    bands = infer_content_bands(rows)
    y_tab = int(bands.get("content_bottom_px") or 0) or 9999
    sample = {"nodes": rows}
    fw = detect_layout_framework(sample, y_tab_max=y_tab, exclude=set())
    chrome = stable_chrome_texts(sample, y_tab_max=y_tab, exclude=set())
    fp, chrome_key = framework_fingerprint(fw, chrome)
    ck = ",".join(str(x) for x in (chrome_key or ()) if x)
    return f"android|{fp}|{ck}"


def page_key(ctx: Any, nodes: list[dict[str, Any]] | None = None) -> str:
    rows = _nodes(nodes if nodes is not None else getattr(ctx, "nav_hierarchy_nodes", None))
    if ui_channel_from_ctx(ctx) == UiChannel.WEB:
        return web_page_key(ctx, rows)
    return android_page_key(rows)


def progress_key(
    ctx: Any,
    *,
    nodes: list[dict[str, Any]] | None = None,
    hierarchy_text: str = "",
    image_base64: str = "",
    width: int = 0,
    height: int = 0,
) -> str:
    """安卓沿用控件树文本指纹。Web 用 URL、对话框/表单、DOM 结构和焦点值。"""
    rows = _nodes(nodes if nodes is not None else getattr(ctx, "nav_hierarchy_nodes", None))
    if ctx is not None and ui_channel_from_ctx(ctx) == UiChannel.WEB:
        focus = normalize_web_focus(getattr(ctx, "web_focus", None))
        value = str(focus.get("value") or "")
        value_fp = _sha(value, 8) if value else f"len={int(focus.get('value_len') or 0)}"
        dialog, form = _web_surface(rows)
        blob = "|".join(
            [
                web_page_key(ctx, rows),
                f"dialog={int(dialog)}",
                f"form={int(form)}",
                f"dom={dom_structure_fingerprint(rows)}",
                f"focus={focus.get('tag')}|{focus.get('type')}|{focus.get('id')}|{focus.get('name')}|{value_fp}",
            ]
        )
        return _sha(blob, 16)
    return screen_fingerprint(
        hierarchy_text=hierarchy_text,
        image_base64=image_base64,
        width=width,
        height=height,
    )


def _node_value(node: dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("value", "text"):
        val = str(node.get(key) or "").strip()
        if val:
            return val
    return ""


def read_field(
    ctx: Any,
    nodes: list[dict[str, Any]] | None,
    field: str,
) -> str:
    """读输入框当前值。Web 优先 activeElement.value，其次 DOM 节点。"""
    fld = str(field or "").strip().lower()
    rows = _nodes(nodes if nodes is not None else getattr(ctx, "nav_hierarchy_nodes", None))
    focus = normalize_web_focus(getattr(ctx, "web_focus", None) if ctx is not None else None)
    focus_val = str(focus.get("value") or "").strip()
    channel = ui_channel_from_ctx(ctx)

    if channel == UiChannel.WEB:
        from mino_nexus.loop.ui_dom import find_dom_email_field, find_dom_phone_field

        hit = None
        if fld in ("email", "login_email"):
            hit = find_dom_email_field(rows)
        elif fld in ("phone", "tel"):
            hit = find_dom_phone_field(rows)
        elif fld in ("sms_code", "otp", "验证码"):
            hit = _focused_input(rows)
        node_val = _node_value(hit)
        if node_val:
            return node_val
        if focus_val:
            return focus_val
        return ""

    from mino_nexus.loop.hierarchy_slots import node_flag

    for node in rows:
        cls = str(node.get("class") or "")
        if "EditText" not in cls and not node_flag(node, "focused"):
            continue
        if fld in ("sms_code", "otp", "验证码") and not node_flag(node, "focused"):
            continue
        val = _node_value(node)
        if val:
            return val
    return ""


def _focused_input(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    from mino_nexus.loop.hierarchy_slots import node_flag

    for node in nodes:
        blob = f"{node.get('class') or ''} {node.get('role') or ''}"
        if _DOM_INPUT_RE.search(blob) and node_flag(node, "focused"):
            return node
    return None


def capture_decision_frame(proxy: Any, ctx: Any, *, hierarchy: bool = True) -> tuple[Any, str]:
    """先采层级，再截屏。截屏不早于这次层级，卡片和判断用同一时刻。纯视觉只截屏。"""
    from mino_nexus.loop.observe.agent_stream import make_thumb
    from mino_nexus.loop.hierarchy_slots import capture

    snap = capture(proxy, turn_id=0) if hierarchy else None
    if snap is not None and getattr(snap, "ok", False):
        setattr(
            ctx,
            "nav_hierarchy_nodes",
            [n for n in (getattr(snap, "nodes", None) or []) if isinstance(n, dict)],
        )
        focus = getattr(snap, "web_focus", None)
        if isinstance(focus, dict) and focus:
            setattr(ctx, "web_focus", dict(focus))
    shot = None
    try:
        shot = proxy.observe("screenshot", force_fresh=True)
    except Exception:
        shot = None
    if shot is not None and int(getattr(shot, "width", 0) or 0) > 0:
        setattr(ctx, "viewport_wh", (int(shot.width), int(shot.height)))
    thumb = ""
    if shot is not None and getattr(shot, "has_image", lambda: False)():
        thumb = make_thumb(shot.image_base64) or ""
    return shot, thumb


def login_credential_form_open(nodes: list[dict[str, Any]] | None) -> bool:
    """邮箱输入框和验证码输入框同时还在，说明提交后没有离开登录表单。"""
    from mino_nexus.loop.ui_dom import find_dom_email_field, find_dom_otp_field

    rows = [n for n in (nodes or []) if isinstance(n, dict)]
    email = find_dom_email_field(rows)
    if email is None:
        return False
    return find_dom_otp_field(rows, email_field=email) is not None


def read_field_after_input(ctx: Any, proxy: Any, field: str) -> str | None:
    """动作后读回。纯视觉没有层级，返回 None：这次没读，不是框里是空的。"""
    scheme = str(
        getattr(proxy, "action_scheme", "") or getattr(ctx, "action_scheme", "") or ""
    ).strip().lower()
    if scheme == "visual":
        return None
    bind_fresh_observation(ctx, proxy)
    return read_field(ctx, getattr(ctx, "nav_hierarchy_nodes", None), field)


def bind_fresh_observation(ctx: Any, proxy: Any) -> None:
    """动作后再取一帧 hierarchy，把节点和 web_focus 写回 ctx。"""
    if ctx is None or proxy is None:
        return
    from mino_nexus.loop.hierarchy_slots import capture

    snap = capture(proxy, turn_id=0)
    if snap.ok:
        setattr(ctx, "nav_hierarchy_nodes", list(snap.nodes or []))
    if isinstance(snap.web_focus, dict) and snap.web_focus:
        setattr(ctx, "web_focus", normalize_web_focus(snap.web_focus))


def attach_input_readback(ctx: Any, params: dict[str, Any] | None) -> dict[str, Any]:
    """输入回看只认节点里的字。不改派给设备的原参数，也不用焦点回显。"""
    out = dict(params or {})
    if ctx is None:
        return out
    field = str(out.get("field") or "")
    rows = _nodes(getattr(ctx, "nav_hierarchy_nodes", None))
    fld = field.strip().lower()
    hit = None
    if ui_channel_from_ctx(ctx) == UiChannel.WEB:
        if fld in ("email", "login_email"):
            from mino_nexus.loop.ui_dom import find_dom_email_field

            hit = find_dom_email_field(rows)
        elif fld in ("phone", "tel"):
            from mino_nexus.loop.ui_dom import find_dom_phone_field

            hit = find_dom_phone_field(rows)
        elif fld in ("sms_code", "otp", "验证码"):
            from mino_nexus.loop.ui_dom import find_dom_otp_field

            hit = find_dom_otp_field(rows) or _focused_input(rows)
    else:
        if fld in ("email", "login_email"):
            from mino_nexus.loop.ui_sms_request import find_android_email_field

            hit = find_android_email_field(rows)
        elif fld in ("phone", "tel"):
            from mino_nexus.loop.ui_sms_request import find_phone_field

            hit = find_phone_field(rows)
        else:
            hit = _focused_input(rows)
    out["_field_value"] = _node_value(hit)
    out["_field_value_read"] = True
    return out
