"""派单前按 UI 渠道补 EXECUTE params（仅 Web 改写；安卓原样返回）。"""
from __future__ import annotations

from typing import Any


def prepare_device_execute_params(
    cap_id: str,
    params: dict[str, Any] | None,
    ctx: Any,
) -> dict[str, Any]:
    out = dict(params or {})
    cid = str(cap_id or "").strip()
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

    if ui_channel_from_ctx(ctx) != UiChannel.WEB:
        return out
    if cid == "tap_element":
        return enrich_web_tap_params(out, ctx)
    if cid == "input_text":
        return enrich_web_input_text_params(out, ctx)
    if cid in ("long_press_element", "multi_tap"):
        out["web_coordinate_only"] = True
    return out


def _tap_node_pool(
    ctx: Any,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    for n in hierarchy_nodes or []:
        if isinstance(n, dict):
            pool.append(n)
    if ctx is not None:
        for n in getattr(ctx, "nav_hierarchy_nodes", None) or []:
            if isinstance(n, dict) and n not in pool:
                pool.append(n)
        vlm = getattr(ctx, "nav_vlm_hierarchy", None)
        if isinstance(vlm, dict):
            for n in vlm.get("nodes") or []:
                if isinstance(n, dict):
                    pool.append(n)
    return pool


def web_tap_params_for_selector(
    ctx: Any,
    label: str,
    *,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Web 点击：hierarchy/VLM 补坐标，并标记 Scout 优先走视觉坐标。"""
    lab = str(label or "").strip()
    from mino_nexus.loop.tap_enrich import enrich_tap_params

    base: dict[str, Any] = {"selector_text": lab, "text": lab}
    pool = _tap_node_pool(ctx, hierarchy_nodes)
    out = enrich_tap_params(base, pool, hint=lab)
    return enrich_web_tap_params(out, ctx)


def enrich_web_tap_params(params: dict[str, Any], ctx: Any | None = None) -> dict[str, Any]:
    """Web tap：看图缺坐标时不从层级补点。DOM 方案仍可按节点补。"""
    out = dict(params or {})
    out["web_coordinate_only"] = True
    out["tap_prefer_coordinates"] = True
    from mino_nexus.action_space.scheme import action_scheme

    if action_scheme(ctx) != "dom":
        return out
    if out.get("x") is None or out.get("y") is None:
        sel = str(out.get("selector_text") or out.get("text") or "").strip()
        if sel and ctx is not None:
            from mino_nexus.loop.tap_enrich import enrich_tap_params

            pool = _tap_node_pool(ctx, None)
            out = enrich_tap_params(out, pool, hint=sel)
    return out


def _coords_from_web_focus(ctx: Any) -> dict[str, Any]:
    from mino_nexus.loop.web.web_progress import normalize_web_focus
    from mino_nexus.loop.ui_consent import tap_params_for_control

    focus = normalize_web_focus(getattr(ctx, "web_focus", None) if ctx else None)
    if not focus.get("editable_ready"):
        return {}
    bb = list(focus.get("bounds") or [])
    if len(bb) < 4:
        return {}
    node = {"bounds": bb, "text": "", "clickable": True}
    pool = _tap_node_pool(ctx, None)
    return tap_params_for_control(node, pool if pool else [node])


def _dom_node_is_email_like(node: dict[str, Any]) -> bool:
    from mino_nexus.loop.ui_consent import _label_text

    lab = _label_text(node)
    if "@" in lab:
        return True
    low = lab.lower()
    return low in ("email", "e-mail") or "邮箱" in lab


def _dom_nodes_same_place(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    tol_px: float = 28.0,
) -> bool:
    from mino_nexus.loop.ui_consent import _bounds, _center

    ax, ay = _center(_bounds(a))
    bx, by = _center(_bounds(b))
    return abs(ax - bx) <= tol_px and abs(ay - by) <= tol_px


def resolve_web_sms_code_tap_params(
    ctx: Any,
    *,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Web 验证码框坐标。已有点击落点时只返回该千分比，不再用 DOM 另算一个点。"""
    cached = getattr(ctx, "web_sms_code_tap_milli", None) if ctx is not None else None
    if isinstance(cached, (list, tuple)) and len(cached) >= 2:
        try:
            return {"x": int(cached[0]), "y": int(cached[1])}
        except (TypeError, ValueError):
            pass
    pool = _tap_node_pool(ctx, hierarchy_nodes)
    from mino_nexus.loop.ui_consent import tap_params_for_control

    if pool:
        from mino_nexus.loop.ui_dom import (
            find_dom_email_field,
            find_dom_otp_field,
            find_dom_text_input_below,
        )

        email_n = find_dom_email_field(pool)
        otp_n = find_dom_otp_field(pool, email_field=email_n)
        if otp_n is None and email_n is not None:
            otp_n = find_dom_text_input_below(pool, email_n)
        if otp_n is None:
            otp_n = find_dom_text_input_below(pool, None)
        if otp_n is not None and email_n is not None:
            if (
                otp_n is email_n
                or _dom_nodes_same_place(otp_n, email_n)
                or _dom_node_is_email_like(otp_n)
            ):
                otp_n = find_dom_text_input_below(pool, email_n)
        if otp_n is not None and email_n is not None and _dom_nodes_same_place(otp_n, email_n):
            otp_n = None
        if otp_n is not None:
            return tap_params_for_control(otp_n, pool)
    return {}


def cache_web_sms_code_tap_from_ctx(
    ctx: Any,
    *,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> None:
    """点击落点已写入时保持不动。DOM 查找不能覆盖 visual_tap 的千分比。"""
    del hierarchy_nodes
    if ctx is None or _cached_sms_milli(ctx) is not None:
        return


def _coords_from_login_field(
    ctx: Any,
    field: str,
    *,
    pool: list[dict[str, Any]] | None = None,
    prior_otp_tap_only: bool = False,
) -> dict[str, Any]:
    nodes = pool if pool is not None else _tap_node_pool(ctx, None)
    from mino_nexus.loop.ui_channel import ui_channel_label, ui_channel_from_ctx
    from mino_nexus.loop.ui_consent import tap_params_for_control

    fld = str(field or "").strip().lower()
    if fld in ("email", "login_email") and ctx is not None:
        cached = getattr(ctx, "web_email_tap_milli", None)
        cached_xy: tuple[int, int] | None = None
        if isinstance(cached, (list, tuple)) and len(cached) >= 2:
            try:
                cached_xy = (int(cached[0]), int(cached[1]))
            except (TypeError, ValueError):
                cached_xy = None
        if nodes:
            from mino_nexus.loop.ui_dom import find_dom_email_field

            email_n = find_dom_email_field(nodes)
            if email_n is not None:
                ep = tap_params_for_control(email_n, nodes)
                if cached_xy is None or not _same_sent_point(
                    cached_xy[0], cached_xy[1], ep.get("x"), ep.get("y"), ctx, tol_px=48
                ):
                    return ep
        if cached_xy is not None:
            return {"x": cached_xy[0], "y": cached_xy[1]}
    if fld in ("sms_code", "验证码", "otp"):
        if prior_otp_tap_only:
            cached = getattr(ctx, "web_sms_code_tap_milli", None) if ctx is not None else None
            if isinstance(cached, (list, tuple)) and len(cached) >= 2:
                try:
                    return {"x": int(cached[0]), "y": int(cached[1])}
                except (TypeError, ValueError):
                    pass
            return {}
        email_n = None
        extra = resolve_web_sms_code_tap_params(ctx, hierarchy_nodes=nodes)
        if extra:
            return extra
        hit = _coords_from_web_focus(ctx)
        if hit:
            return hit
        return {}

    if not nodes:
        return {}
    from mino_nexus.loop.ui_sms_request import credential_field_for_login

    ch = ui_channel_label(ui_channel_from_ctx(ctx))
    from mino_nexus.services.account_credential_text import login_kind_from_ctx

    kind = login_kind_from_ctx(ctx)
    if fld in ("email", "login_email"):
        kind = "email"
    elif fld == "phone":
        kind = "phone"
    node = credential_field_for_login(nodes, login_kind=kind, channel=ch)
    if node is None:
        return {}
    return tap_params_for_control(node, nodes)


def _viewport_wh(ctx: Any) -> tuple[int, int]:
    raw = getattr(ctx, "viewport_wh", None) if ctx is not None else None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            w, h = int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            return 0, 0
        if w > 0 and h > 0:
            return w, h
    return 0, 0


def _cached_sms_milli(ctx: Any) -> tuple[int, int] | None:
    raw = getattr(ctx, "web_sms_code_tap_milli", None) if ctx is not None else None
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        return int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None


def _same_sent_point(ax: Any, ay: Any, bx: Any, by: Any, ctx: Any, *, tol_px: int = 40) -> bool:
    """两个派单坐标都按视口换像素后再比，避免千分比和像素直接相减。"""
    from mino_nexus.ai.coords import milli_to_viewport_px

    w, h = _viewport_wh(ctx)
    if w <= 0 or h <= 0:
        try:
            return abs(int(ax) - int(bx)) <= 20 and abs(int(ay) - int(by)) <= 20
        except (TypeError, ValueError):
            return False
    pa = milli_to_viewport_px(ax, ay, w, h)
    pb = milli_to_viewport_px(bx, by, w, h)
    if pa is None or pb is None:
        return False
    return abs(pa[0] - pb[0]) <= tol_px and abs(pa[1] - pb[1]) <= tol_px


_SMS_FIELDS = ("sms_code", "验证码", "otp")


def enrich_web_input_text_params(params: dict[str, Any], ctx: Any | None = None) -> dict[str, Any]:
    """Web 输入：补 field 元数据 + VLM/hierarchy 坐标（Scout 仅坐标点击后键入）。"""
    out = dict(params or {})
    field = str(out.get("field") or "").strip().lower()
    if out.pop("web_type_into_focus", None):
        cached = _cached_sms_milli(ctx) if field in _SMS_FIELDS else None
        if cached is not None:
            # 沿用刚点中的验证码框千分比。Scout 按视口换一次像素，和那次点击同一落点。
            out["x"], out["y"] = cached
            out["web_coordinate_only"] = True
            out["tap_prefer_coordinates"] = True
            out["otp_coords_from_prior_tap_only"] = True
        else:
            out["web_coordinate_only"] = False
            for k in ("x", "y", "tap_prefer_coordinates", "otp_coords_from_prior_tap_only"):
                out.pop(k, None)
            target = dict(out.get("target") or {}) if isinstance(out.get("target"), dict) else {}
            if field in _SMS_FIELDS:
                out.setdefault("selector_text", "验证码")
                target.setdefault("text", "验证码")
                out["login_field"] = "sms_code"
            if target:
                out["target"] = target
            return out
    out["web_coordinate_only"] = True
    if not field or field in ("text", "input"):
        if out.get("x") is None or out.get("y") is None:
            if ctx is not None:
                from mino_nexus.loop.tap_enrich import enrich_tap_params

                hint = str(out.get("selector_text") or out.get("text") or "")
                out = enrich_tap_params(out, _tap_node_pool(ctx, None), hint=hint)
        return out
    target = dict(out.get("target") or {}) if isinstance(out.get("target"), dict) else {}

    if field in ("email", "login_email"):
        out.setdefault("selector_text", "email")
        target.setdefault("text", "email")
        out["login_field"] = "email"
    elif field == "phone":
        out.setdefault("selector_text", "phone")
        target.setdefault("text", "phone")
        out["login_field"] = "phone"
    elif field in ("sms_code", "验证码", "otp"):
        out.setdefault("selector_text", "验证码")
        target.setdefault("text", "验证码")
        out["login_field"] = "sms_code"
    elif field in ("password", "密码"):
        out.setdefault("selector_text", "password")
        target.setdefault("text", "password")
        out["login_field"] = "password"

    if target:
        out["target"] = target
    prior_otp_tap_only = bool(out.get("otp_coords_from_prior_tap_only"))
    if ctx is not None:
        extra = _coords_from_login_field(
            ctx, field, prior_otp_tap_only=prior_otp_tap_only
        )
        if field in ("sms_code", "验证码", "otp") and extra.get("x") is not None:
            pool = _tap_node_pool(ctx, None)
            if pool:
                from mino_nexus.loop.ui_dom import find_dom_email_field
                from mino_nexus.loop.ui_consent import tap_params_for_control

                email_n = find_dom_email_field(pool)
                if email_n is not None:
                    ep = tap_params_for_control(email_n, pool)
                    ex, ey = int(ep.get("x") or -1), int(ep.get("y") or -1)
                    ox, oy = int(extra.get("x") or -1), int(extra.get("y") or -1)
                    if ex >= 0 and ox >= 0 and _same_sent_point(ex, ey, ox, oy, ctx):
                        extra = {}
                        setattr(ctx, "web_sms_code_tap_milli", None)
        locked = bool(out.get("otp_coords_from_prior_tap_only")) and out.get("x") is not None
        locked_email = (
            bool(out.get("email_coords_from_prior_tap_only")) and out.get("x") is not None
        )
        if (
            locked_email
            and field in ("email", "login_email")
            and extra.get("x") is not None
            and not _same_sent_point(
                out.get("x"), out.get("y"), extra.get("x"), extra.get("y"), ctx, tol_px=48
            )
        ):
            out["x"] = extra["x"]
            out["y"] = extra["y"]
            out.pop("email_coords_from_prior_tap_only", None)
            locked_email = False
        if (
            not locked
            and not locked_email
            and field in ("sms_code", "验证码", "otp")
            and extra.get("x") is not None
            and out.get("x") is not None
            and not _same_sent_point(out.get("x"), out.get("y"), extra.get("x"), extra.get("y"), ctx)
        ):
            extra = {k: v for k, v in extra.items() if k not in ("x", "y")}
        if (
            not locked
            and not locked_email
            and field in ("email", "login_email", "sms_code", "验证码", "otp")
            and extra.get("x") is not None
        ):
            out["x"] = extra["x"]
            out["y"] = extra["y"]
        elif out.get("x") is None or out.get("y") is None:
            for k, v in extra.items():
                out.setdefault(k, v)
    if (
        ctx is not None
        and field in ("sms_code", "验证码", "otp")
        and out.get("x") is not None
        and out.get("y") is not None
        and not out.get("otp_coords_from_prior_tap_only")
    ):
        setattr(ctx, "web_sms_code_tap_milli", (int(out["x"]), int(out["y"])))
    return out
