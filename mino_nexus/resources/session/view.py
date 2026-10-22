"""会话在 prompt 里的呈现：登录态、账号线索、紧凑字段。

纯读 `ctx` 属性 + 本域解析函数，因此 `ai` 与 `loop` 都能直接用它构造上下文。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.resources.accounts.session_match import normalize_device_session
from mino_nexus.resources.project_env import account_ident
from mino_nexus.resources.session.ensure import parse_session_value
from mino_nexus.resources.otp_resolve import is_web_email_login  # noqa: F401

def build_session_json(ctx: Any, slot_block: str = "") -> dict[str, Any]:

    fact = dict(getattr(ctx, "session_fact", None) or {})
    scene = dict(getattr(ctx, "case_scene", None) or {})
    block = str(slot_block or "").strip()
    parsed = parse_session_value(block) if block and not block.startswith("（") else ""

    session = normalize_device_session(fact.get("session") or parsed or "")
    return {
        "session": session,
        "identity": str(fact.get("identity") or "").strip(),
        "seen": str(fact.get("seen") or "").strip(),
        "source": str(fact.get("source") or ("inspect_slot" if block else "run_context")).strip(),
        "reason": str(fact.get("reason") or "").strip()[:200],
        "dirty": bool(getattr(ctx, "session_dirty", False)),
        "required_session": str(scene.get("required_session") or "any").strip().lower(),
        "prep_clear_done": bool(getattr(ctx, "prep_clear_done", False)),
        "execution_line": execution_context_session_line(ctx),
        "slot_line": block[:400] if block else "",
    }


def build_accounts_json(ctx: Any) -> dict[str, Any]:
    brief = str(getattr(ctx, "accounts_brief", "") or "").strip()
    picked = dict(getattr(ctx, "picked_account", None) or {})
    leased = bool(picked) or bool(getattr(ctx, "prep_lease_account_done", False))
    out: dict[str, Any] = {
        "leased": leased,
        "brief_legacy": brief[:500] if brief else "",
        "account_id": str(picked.get("account_id") or picked.get("id") or "").strip(),
        "env": str(picked.get("env") or "").strip(),
    }
    if picked:
        ident = account_ident(picked) or ""
        if ident:
            out["identity_hint"] = ident[:120]
    try:
        out["login_channel"] = "email_web" if is_web_email_login(ctx) else ""
    except Exception:
        out["login_channel"] = ""
    return out


def execution_context_session_line(ctx: Any) -> str:
    """供 decide/assert 的紧凑登录态字段（与 session_block 互补）。"""
    from mino_nexus.resources.accounts.session_match import normalize_device_session

    fact = dict(getattr(ctx, "session_fact", None) or {})
    scene = dict(getattr(ctx, "case_scene", None) or {})
    parts = [
        f"session_dirty={bool(getattr(ctx, 'session_dirty', False))}",
        f"required_session={str(scene.get('required_session') or 'any')}",
        f"task_session={normalize_device_session(fact.get('session') or '')}",
    ]
    stale = str(fact.get("stale_reason") or "").strip()
    if stale:
        parts.append(f"stale_reason={stale[:80]}")
    return "【执行上下文·会话】" + " ".join(parts)
