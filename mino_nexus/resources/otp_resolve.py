"""接码 / 登录凭证：按 env_profile × env_surface 解析 effective secrets。"""
from __future__ import annotations

from typing import Any

import re

from mino_nexus.resources.project_env import (
    _norm_env_secrets,
    _norm_gmail_inbox,
    default_env_secrets,
    env_secrets,
    matching_login_policy,
)


def resolve_effective_secrets(
    env_doc: dict | None,
    *,
    env_profile: str = "test",
    env_surface: str = "",
    app_id: str = "",
) -> dict[str, Any]:
    """登录策略优先。未命中时再用 channel_secrets[surface][env]，最后是环境默认。"""
    doc = env_doc if isinstance(env_doc, dict) else {}
    env_key = str(env_profile or "test").strip() or "test"
    policy = matching_login_policy(doc, app_id)
    if isinstance(policy, dict):
        secrets = policy.get("secrets") if isinstance(policy.get("secrets"), dict) else {}
        slot = secrets.get(env_key)
        if isinstance(slot, dict):
            return _norm_env_secrets(slot)
    surface = str(env_surface or "").strip()
    ch_map = doc.get("channel_secrets")
    if surface and isinstance(ch_map, dict):
        row = ch_map.get(surface)
        if isinstance(row, dict):
            slot = row.get(env_key)
            if isinstance(slot, dict) and not bool(slot.get("inherit")):
                return _norm_env_secrets(slot)
    return env_secrets(doc, env_key)


def _inbox_from_scout(*, plugin_user_id: str = "", node_id: str = "", sn: str = "") -> str:
    """Only the authenticated owner's configured, live execution node is eligible."""
    from mino_nexus.platform.identity import require_user_id
    from mino_nexus.resources.node_registry import get_registry
    from mino_nexus.resources.execution_dependencies import plugin_fact

    uid = require_user_id(plugin_user_id or None)
    reg = get_registry()
    nid = str(node_id or "").strip()
    if sn:
        node, _ = reg.resolve(sn)
        if node is None or (nid and nid != node.node_id):
            return ""
        nid = node.node_id
    spec = {"kind": "plugin", "class": "mail", "plugin_id": "gmail"}
    if nid:
        spec["node_id"] = nid
    result = plugin_fact(reg.nodes(), spec, uid)
    if result["status"] != "ready":
        return ""
    node = reg.get_node(result["node_id"])
    if (node is None or node.owner_user_id != uid or not node.alive or node.send is None
            or node.node_connection_generation != result["node_connection_generation"]
            or node.scout_process_generation != result["scout_process_generation"]):
        return ""
    for row in node.plugins or []:
        if row.get("class") == "mail" and row.get("id") == "gmail" and row.get("configured") is True:
            values = row.get("values") if isinstance(row.get("values"), dict) else {}
            inbox = str(values.get("inbox_address") or "").strip()
            return inbox if "@" in inbox else ""
    return ""


def gmail_inbox_address(env_doc: dict | None, *, plugin_user_id: str = "", node_id: str = "", sn: str = "") -> str:
    del env_doc
    return _inbox_from_scout(plugin_user_id=plugin_user_id, node_id=node_id, sn=sn)


def plugin_user_id_from_ctx(ctx: Any) -> str:
    return str(getattr(ctx, "plugin_user_id", "") or getattr(ctx, "user_id", "") or "").strip()


def resolve_gmail_inbox(env_doc: dict | None, ctx: Any | None = None) -> str:
    uid = plugin_user_id_from_ctx(ctx) if ctx is not None else ""
    node_id = str(getattr(ctx, "node_id", "") or "") if ctx is not None else ""
    sn = str(getattr(ctx, "sn", "") or "") if ctx is not None else ""
    return gmail_inbox_address(env_doc, plugin_user_id=uid, node_id=node_id, sn=sn)


def login_kind_from_secrets(secrets: dict | None) -> str:
    return _norm_env_secrets(secrets)["login"]["kind"]


def account_row_matches_login_kind(row: dict | None, kind: str) -> bool:
    r = row if isinstance(row, dict) else {}
    k = str(kind or "phone").strip().lower()
    if k == "email":
        return bool(str(r.get("email") or "").strip())
    phone = re.sub(r"\s+", "", str(r.get("phone") or ""))
    return bool(phone)


def filter_accounts_for_login_kind(
    rows: list[dict],
    *,
    kind: str,
) -> list[dict]:
    k = str(kind or "phone").strip().lower()
    if k not in ("phone", "email"):
        return list(rows or [])
    matched = [r for r in (rows or []) if account_row_matches_login_kind(r, k)]
    # 禁止回退：email 登录不能租到仅手机号的行，否则程序链静默失败、模型狂点 Email。
    return matched


def format_run_credential_hint(
    env_doc: dict | None,
    *,
    env_profile: str = "test",
    env_surface: str = "",
    plugin_user_id: str = "",
) -> str:
    """跑批注入 history：与 project_env 登录/接码配置一致，避免模型走错手机/邮箱分支。"""
    doc = env_doc if isinstance(env_doc, dict) else {}
    secrets = resolve_effective_secrets(doc, env_profile=env_profile, env_surface=env_surface)
    kind = login_kind_from_secrets(secrets)
    otp = secrets.get("otp") if isinstance(secrets.get("otp"), dict) else {}
    mode = str(otp.get("mode") or "auto").strip().lower()
    surface = str(env_surface or "").strip()
    env_key = str(env_profile or "test").strip() or "test"
    head = f"【登录凭证·环境】{env_key}"
    if surface:
        head += f" × {surface}"
    if kind == "email":
        login_line = (
            "登录方式=邮箱：选 Email 标签后 input_text(field=email) 填租号邮箱，"
            "用 tap_element 点击发送验证码控件，确认发码后 get_otp 取验证码（勿反复点 Email tab）。"
        )
    else:
        login_line = (
            "登录方式=手机号：input_text(field=phone) 填租号手机号，"
            "用 tap_element 点击发送验证码控件，确认发码后 get_otp 再填验证码。"
        )
    otp_bits: list[str] = []
    if mode == "fixed":
        otp_bits.append("接码=fixed（环境固定码）")
    elif mode == "gmail":
        inbox = gmail_inbox_address(doc, plugin_user_id=plugin_user_id)
        otp_bits.append(
            "接码=Gmail IMAP"
            + (f"（收件箱 {inbox}）" if inbox else "（未配收件箱：插件 Gmail 收信）")
        )
    elif mode == "hitl":
        otp_bits.append("接码=人工 hitl")
    else:
        otp_bits.append(f"接码=auto（mode={mode}）")
    tail = "；".join(otp_bits) if otp_bits else ""
    return f"{head}；{login_line}" + (f"；{tail}" if tail else "")


def otp_context(ctx: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(env_doc, effective_secrets, otp_slot) — 失败时返回空 dict。"""
    try:
        from mino_nexus.resources import project_store as ps
        from mino_nexus.resources.otp_resolve import resolve_effective_secrets

        app_id = str(getattr(ctx, "app_id", "") or "").strip() if ctx is not None else ""
        if not app_id:
            return {}, {}, {}
        app = ps.require_app(app_id)
        project_id = str(app.get("project_id") or "").strip()
        if not project_id:
            return {}, {}, {}
        env_doc = ps.project_env(project_id)
        env_key = str(getattr(ctx, "env_profile", "") or "test") if ctx is not None else "test"
        surface = str(getattr(ctx, "env_surface", "") or "").strip()
        secrets = resolve_effective_secrets(
            env_doc,
            env_profile=env_key,
            env_surface=surface,
            app_id=app_id,
        )
        otp = secrets.get("otp") if isinstance(secrets, dict) else {}
        if not isinstance(otp, dict):
            otp = {}
        return env_doc, secrets, otp
    except Exception:
        return {}, {}, {}


def resolve_otp(ctx: Any) -> tuple[str, str]:
    acc = getattr(ctx, "picked_account", None) if ctx is not None else None
    if isinstance(acc, dict):
        for key in ("otp", "sms_code", "code"):
            val = str(acc.get(key) or "").strip()
            if val:
                return val, "account"
    env_doc, _secrets, otp = otp_context(ctx)
    if not otp:
        return "", "missing"
    fixed = str(otp.get("fixed") or "").strip()
    mode = str(otp.get("mode") or "auto").strip().lower()
    if mode == "hitl":
        return "", "hitl"
    if fixed and mode in ("fixed", "auto"):
        return fixed, "env_fixed"
    if mode == "gmail":
        return "", "gmail"
    if mode == "auto":
        from mino_nexus.resources.otp_resolve import resolve_gmail_inbox

        if resolve_gmail_inbox(env_doc, ctx):
            return "", "gmail"
    if mode == "fixed" and not fixed:
        return "", "missing"
    return "", "missing"


def lease_email_for_otp(ctx: Any) -> str:
    acc = getattr(ctx, "picked_account", None) if ctx is not None else None
    if isinstance(acc, dict):
        for key in ("email", "login_email"):
            val = str(acc.get(key) or "").strip()
            if val:
                return val
    return ""


def login_kind_for_ctx(ctx: Any | None) -> str:
    if ctx is None:
        return "phone"
    try:
        _doc, secrets, _otp = otp_context(ctx)
        return login_kind_from_secrets(secrets)
    except Exception:
        return "phone"


def is_web_email_login(ctx: Any | None) -> bool:
    if ctx is None:
        return False
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

    return ui_channel_from_ctx(ctx) == UiChannel.WEB and login_kind_for_ctx(ctx) == "email"
