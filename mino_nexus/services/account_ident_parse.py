"""从前置 / 参数里提取账号标识（手机、邮箱），用于租号硬约束。"""
from __future__ import annotations

import re

_PHONE_RE = re.compile(r"(?<!\d)(1\d{10})(?!\d)")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_ACCOUNT_LINE_RE = re.compile(
    r"(?:账号|手机|手机号|登录号|测试号|帐号)\s*[:：]\s*([^\s,，;；]+)",
    re.I,
)


def _norm_phone(raw: str) -> str:
    return re.sub(r"\D+", "", str(raw or ""))


def extract_account_ident_hints(text: str) -> list[str]:
    """去重后的标识片段（优先手机号、邮箱、账号：行）。"""
    pre = str(text or "")
    if not pre.strip():
        return []
    seen: set[str] = set()
    out: list[str] = []

    def add(token: str) -> None:
        t = str(token or "").strip()
        if not t:
            return
        key = t.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(t)

    for m in _PHONE_RE.finditer(pre):
        add(m.group(1))
    for m in _EMAIL_RE.finditer(pre):
        add(m.group(0))
    for m in _ACCOUNT_LINE_RE.finditer(pre):
        chunk = str(m.group(1) or "").strip()
        if _PHONE_RE.fullmatch(_norm_phone(chunk)) or "@" in chunk:
            add(chunk)
        elif len(chunk) >= 4:
            add(chunk)
    return out


def primary_ident_query(text: str) -> str:
    hints = extract_account_ident_hints(text)
    return hints[0] if hints else ""


def account_row_matches_hints(row: dict, hints: list[str]) -> bool:
    if not hints:
        return True
    from mino_nexus.services.project_env import account_ident

    extra = " ".join(
        str(row.get(k) or "")
        for k in ("phone", "email", "username", "display_name", "account_id", "id")
    )
    blob = f"{account_ident(row)} {extra}".lower()
    compact = re.sub(r"\s+", "", blob)
    for h in hints:
        raw = str(h or "").strip()
        if not raw:
            continue
        low = raw.lower()
        if low in blob or low in compact:
            return True
        digits = _norm_phone(raw)
        if len(digits) >= 8 and digits in re.sub(r"\D+", "", compact):
            return True
    return False
