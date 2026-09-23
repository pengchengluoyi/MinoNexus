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


_SCALAR_MATCH_KEYS = (
    "phone",
    "email",
    "username",
    "account_id",
    "id",
    "note",
    "profile_id",
    "env",
)


def account_lease_match_blob(row: dict | None) -> str:
    """前置/租号标识命中用的拼接文本：含手机邮箱、facet、tags 等；**不含 display_name**。"""
    r = row if isinstance(row, dict) else {}
    parts: list[str] = []
    for k in _SCALAR_MATCH_KEYS:
        v = str(r.get(k) or "").strip()
        if v:
            parts.append(v)
    for t in r.get("tags") or []:
        s = str(t).strip()
        if s:
            parts.append(s)
    from mino_nexus.services.resource_pool import account_facets

    for v in account_facets(r).values():
        s = str(v).strip()
        if s:
            parts.append(s)
    return " ".join(parts).lower()


def account_lease_match_compact(row: dict | None) -> str:
    return re.sub(r"\s+", "", account_lease_match_blob(row))


def account_row_matches_hints(row: dict, hints: list[str]) -> bool:
    if not hints:
        return True
    blob = account_lease_match_blob(row)
    compact = account_lease_match_compact(row)
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
