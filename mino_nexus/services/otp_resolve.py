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


def gmail_inbox_address(env_doc: dict | None) -> str:
    doc = env_doc if isinstance(env_doc, dict) else {}
    return str(_norm_gmail_inbox(doc.get("gmail_inbox")).get("address") or "").strip()


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
    return matched if matched else list(rows or [])


def format_run_credential_hint(
    env_doc: dict | None,
    *,
    env_profile: str = "test",
    env_surface: str = "",
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
        inbox = gmail_inbox_address(doc)
        otp_bits.append(
            "接码=Gmail IMAP"
            + (f"（收件箱 {inbox}）" if inbox else "（未配收件箱地址）")
        )
    elif mode == "hitl":
        otp_bits.append("接码=人工 hitl")
    else:
        otp_bits.append(f"接码=auto（mode={mode}）")
    tail = "；".join(otp_bits) if otp_bits else ""
    return f"{head}；{login_line}" + (f"；{tail}" if tail else "")
