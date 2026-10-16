"""接码 / 登录凭证：按 env_profile × env_surface 解析 effective secrets。"""
from __future__ import annotations

from typing import Any

import re

from mino_nexus.services.project_env import (
    _norm_env_secrets,
    _norm_gmail_inbox,
    default_env_secrets,
    env_secrets,
)


def resolve_effective_secrets(
    env_doc: dict | None,
    *,
    env_profile: str = "test",
    env_surface: str = "",
) -> dict[str, Any]:
    """channel_secrets[surface][env] 优先，否则 environments[env].secrets。"""
    doc = env_doc if isinstance(env_doc, dict) else {}
    env_key = str(env_profile or "test").strip() or "test"
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
    from mino_nexus.core.execution_user import require_user_id
    from mino_nexus.services.node_registry import get_registry
    from mino_nexus.services.execution_dependencies import plugin_fact

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
    sec = secrets if isinstance(secrets, dict) else {}
    login = sec.get("login") if isinstance(sec.get("login"), dict) else {}
    kind = str(login.get("kind") or sec.get("login_kind") or "phone").strip().lower()
    return kind if kind in ("phone", "email") else "phone"


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
            "request_sms_code 发码，get_otp 取验证码（勿反复点 Email tab）。"
        )
    else:
        login_line = (
            "登录方式=手机号：input_text(field=phone) 填租号手机号，"
            "request_sms_code 发码，get_otp 后填验证码。"
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
