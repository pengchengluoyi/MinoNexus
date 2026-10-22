"""按已租账号/OTP 修复 input_text 的 params：执行期的参数补全。"""
from __future__ import annotations

from typing import Any


def fill_input_text_from_ctx(
    params: dict[str, Any] | None,
    *,
    cap_id: str,
    ctx: Any = None,
) -> dict[str, Any]:
    """租号/OTP 网关补 params.text；邮箱登录强制 email 字段与库内邮箱，phone 仅用 acc.phone。"""
    out = dict(params or {})
    if str(cap_id or "").strip() != "input_text":
        return out
    from mino_nexus.resources.account_credential_text import normalize_input_text_for_lease

    out = normalize_input_text_for_lease(out, ctx=ctx)
    if out.get("_credential_unresolved"):
        return out
    acc = dict(getattr(ctx, "picked_account", None) or {}) if ctx is not None else {}
    field = str(out.get("field") or "text").strip().lower()
    if field in ("email", "login_email"):
        return out
    if field == "phone" and str(out.get("text") or "").strip():
        return out
    if field == "password":
        if acc.get("password"):
            out["text"] = str(acc["password"])
        return out
    if field in ("sms_code", "验证码", "otp"):
        out["field"] = "sms_code"
        code = str(acc.get("otp") or acc.get("sms_code") or "").strip()
        if not code and ctx is not None:
            from mino_nexus.resources.otp_resolve import resolve_otp as _resolve_otp

            code, _ = _resolve_otp(ctx)
        if code:
            out["text"] = code
    return out
