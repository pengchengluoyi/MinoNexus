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
    """Web tap：从 VLM/hierarchy 补坐标；Scout 仅按坐标点击。"""
    out = dict(params or {})
    out["web_coordinate_only"] = True
    out["tap_prefer_coordinates"] = True
    if out.get("x") is None or out.get("y") is None:
        sel = str(out.get("selector_text") or out.get("text") or "").strip()
        if sel and ctx is not None:
            from mino_nexus.loop.tap_enrich import enrich_tap_params

            pool = _tap_node_pool(ctx, None)
            out = enrich_tap_params(out, pool, hint=sel)
    return out


def _coords_from_web_focus(ctx: Any) -> dict[str, Any]:
    from mino_nexus.loop.web_progress import normalize_web_focus
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


def resolve_web_sms_code_tap_params(
    ctx: Any,
    *,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Web 验证码框点击坐标（hierarchy/VLM + 邮箱下方第二输入框）。"""
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
        if otp_n is not None:
            return tap_params_for_control(otp_n, pool)
    cached = getattr(ctx, "web_sms_code_tap_milli", None) if ctx is not None else None
    if isinstance(cached, (list, tuple)) and len(cached) >= 2:
        try:
            return {"x": int(cached[0]), "y": int(cached[1])}
        except (TypeError, ValueError):
            pass
    return {}


def cache_web_sms_code_tap_from_ctx(
    ctx: Any,
    *,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> None:
    if ctx is None:
        return
    p = resolve_web_sms_code_tap_params(ctx, hierarchy_nodes=hierarchy_nodes)
    if p.get("x") is not None and p.get("y") is not None:
        setattr(ctx, "web_sms_code_tap_milli", (int(p["x"]), int(p["y"])))


def _coords_from_login_field(
    ctx: Any,
    field: str,
    *,
    pool: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    nodes = pool if pool is not None else _tap_node_pool(ctx, None)
    from mino_nexus.loop.ui_channel import ui_channel_label, ui_channel_from_ctx
    from mino_nexus.loop.ui_consent import tap_params_for_control

    fld = str(field or "").strip().lower()
    if fld in ("sms_code", "验证码", "otp"):
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


def enrich_web_input_text_params(params: dict[str, Any], ctx: Any | None = None) -> dict[str, Any]:
    """Web 输入：补 field 元数据 + VLM/hierarchy 坐标（Scout 仅坐标点击后键入）。"""
    out = dict(params or {})
    out["web_coordinate_only"] = True
    field = str(out.get("field") or "").strip().lower()
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
    if ctx is not None and (out.get("x") is None or out.get("y") is None):
        extra = _coords_from_login_field(ctx, field)
        for k, v in extra.items():
            out.setdefault(k, v)
    if (
        ctx is not None
        and field in ("sms_code", "验证码", "otp")
        and out.get("x") is not None
        and out.get("y") is not None
    ):
        setattr(ctx, "web_sms_code_tap_milli", (int(out["x"]), int(out["y"])))
    return out
