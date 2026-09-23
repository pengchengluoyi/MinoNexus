"""租号凭证填入 input_text：按 login.kind 与 field 只读号池 email/phone。

display_name / account_ident 仅供平台展示，不得作为登录凭据（与 Android/Web 渠道无关）。
"""
from __future__ import annotations

import re
from typing import Any


def login_kind_from_ctx(ctx: Any) -> str:
    try:
        from mino_nexus.loop.local_executors import _otp_context
        from mino_nexus.services.otp_resolve import login_kind_from_secrets

        _doc, secrets, _otp = _otp_context(ctx)
        return login_kind_from_secrets(secrets)
    except Exception:
        return "phone"


def lease_email_address(acc: dict[str, Any] | None) -> str:
    row = acc if isinstance(acc, dict) else {}
    for key in ("email", "login_email"):
        val = str(row.get(key) or "").strip()
        if val and "@" in val:
            return val
    return ""


def lease_phone_digits(acc: dict[str, Any] | None) -> str:
    """仅 acc.phone；不用 display_name / account_ident / 邮箱。"""
    row = acc if isinstance(acc, dict) else {}
    raw = str(row.get("phone") or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 11:
        return digits[-11:]
    if len(digits) >= 10:
        return digits
    return ""


def normalize_input_text_for_lease(
    params: dict[str, Any] | None,
    *,
    ctx: Any = None,
) -> dict[str, Any]:
    """按 login.kind 纠正 field/text，避免邮箱登录填成 phone+用户名。"""
    out = dict(params or {})
    acc = dict(getattr(ctx, "picked_account", None) or {}) if ctx is not None else {}
    kind = login_kind_from_ctx(ctx) if ctx is not None else "phone"
    field = str(out.get("field") or "text").strip().lower()

    if kind == "email":
        if field in ("phone", "username", "login_phone"):
            field = "email"
            out["field"] = "email"
        elif field == "text" and not str(out.get("text") or "").strip():
            field = "email"
            out["field"] = "email"
        email = lease_email_address(acc)
        if field in ("email", "login_email"):
            if email:
                out["text"] = email
            return out
        if email and field == "text" and "@" not in str(out.get("text") or ""):
            out["field"] = "email"
            out["text"] = email
            return out

    if kind == "phone":
        if field in ("email", "login_email", "username"):
            field = "phone"
            out["field"] = "phone"
        phone = lease_phone_digits(acc)
        if field == "phone":
            if phone:
                out["text"] = phone
            elif str(out.get("text") or "").strip():
                cur = re.sub(r"\D", "", str(out.get("text") or ""))
                if len(cur) < 10:
                    out["text"] = ""
            return out

    if field in ("email", "login_email"):
        email = lease_email_address(acc)
        if email:
            out["text"] = email
        return out

    if field == "phone":
        phone = lease_phone_digits(acc)
        if phone:
            out["text"] = phone
        elif str(out.get("text") or "").strip():
            # 勿保留模型/ident 填进来的非 11 位「假手机号」
            cur = re.sub(r"\D", "", str(out.get("text") or ""))
            if len(cur) < 10:
                out["text"] = ""
        return out

    return out
