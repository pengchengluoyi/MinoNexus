"""登录态写入 RunContext，校验阶段可读（不依赖当轮是否重跑 inspect-session）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.loop.session_ensure import parse_session_value

_LOGIN_EXPECT_RE = re.compile(r"登录成功|已登录|logged.?in|用户名称|昵称", re.I)


def stamp_session_observation(ctx: Any, row: dict[str, Any] | None) -> None:
    """inspect-session 成功后写入 ctx.session_fact / task_session。"""
    if not isinstance(row, dict) or not row.get("ok"):
        return
    session = str(row.get("session") or "").strip().lower()
    if session not in ("logged_in", "guest", "logged_out", "unknown"):
        return
    identity = str(row.get("identity") or "").strip()
    seen = str(row.get("seen") or "").strip()
    fact = {
        "session": session,
        "identity": identity,
        "seen": seen,
        "next": str(row.get("next") or "").strip(),
        "reason": str(row.get("reason") or "").strip(),
        "source": "inspect_session",
    }
    ctx.session_fact = dict(fact)
    ctx.task_session = dict(fact)
    if session == "logged_in":
        ctx.session_dirty = False


def mark_session_dirty(ctx: Any, *, reason: str = "") -> None:
    ctx.session_dirty = True
    if reason:
        prev = dict(getattr(ctx, "session_fact", None) or {})
        prev["stale_reason"] = str(reason)[:200]
        ctx.session_fact = prev


def execution_context_session_line(ctx: Any) -> str:
    """供 decide/assert 的紧凑登录态字段（与 session_block 互补）。"""
    fact = dict(getattr(ctx, "session_fact", None) or {})
    scene = dict(getattr(ctx, "case_scene", None) or {})
    parts = [
        f"session_dirty={bool(getattr(ctx, 'session_dirty', False))}",
        f"required_session={str(scene.get('required_session') or 'any')}",
        f"task_session={str(fact.get('session') or 'unknown')}",
    ]
    stale = str(fact.get("stale_reason") or "").strip()
    if stale:
        parts.append(f"stale_reason={stale[:80]}")
    return "【执行上下文·会话】" + " ".join(parts)


def _strip_stacked_session_block(block: str) -> str:
    """历史 bug：run_context 行与 inspect 行用 | 叠在一起；保留 inspect 段。"""
    raw = str(block or "").strip()
    if " | " not in raw:
        return raw
    parts = [p.strip() for p in raw.split(" | ") if p.strip()]
    if len(parts) < 2:
        return raw
    for part in reversed(parts):
        if "source=run_context" in part:
            continue
        if part.startswith("session="):
            return part
    return parts[-1]


def effective_session_block(ctx: Any, slot_block: str = "") -> str:
    """单条 session 结论：优先当轮 inspect-session 槽，不与 run_context 叠句。"""
    block = _strip_stacked_session_block(str(slot_block or "").strip())
    if block and not block.startswith("（"):
        return block

    fact = dict(getattr(ctx, "session_fact", None) or {})
    session = str(fact.get("session") or "").strip().lower()
    if not session:
        return block
    line = (
        f"session={session} identity={fact.get('identity') or ''} "
        f"seen={fact.get('seen') or ''} source=run_context"
    ).strip()
    nxt = str(fact.get("next") or "").strip()
    rsn = str(fact.get("reason") or "").strip()
    if nxt:
        line = f"{line} next={nxt}"
    if rsn:
        line = f"{line} reason={rsn[:120]}"
    scene = dict(getattr(ctx, "case_scene", None) or {})
    req = str(scene.get("required_session") or "any").strip().lower()
    if req == "guest":
        line = f"{line} required=guest"
    elif req == "logged_in":
        line = f"{line} required=logged_in"
    return line


def assert_session_context(ctx: Any, *, instruction: str = "", expected: str = "") -> str:
    """校验步骤若涉及登录态，附上运行上下文里的 session 结论。"""
    blob = f"{instruction}\n{expected}"
    from mino_nexus.runtime.session_gate import required_session

    scene = getattr(ctx, "case_scene", None) or {}
    req = required_session(scene=scene if isinstance(scene, dict) else {})
    if req not in ("logged_in", "guest") and not _LOGIN_EXPECT_RE.search(blob):
        return ""
    block = effective_session_block(ctx, "")
    if not block:
        return ""
    extra = ""
    session = parse_session_value(block)
    picked = getattr(ctx, "picked_account", None) or {}
    if isinstance(picked, dict):
        phone = str(picked.get("phone") or picked.get("ident") or "").strip()
        if phone:
            tail = phone[-4:] if len(phone) >= 4 else phone
            extra += f" 本任务租号尾号 {tail}，判「登录成功」时须与屏上账号一致。"
    if session == "logged_out" and re.search(r"已登录|登录成功|logged.?in", blob, re.I):
        extra += (
            " 当前 session=logged_out 表示未登录（含登录页/登录弹层），"
            "屏上有手机号或登录按钮仍属未登录，不得判为已登录。"
        )
    if req == "guest" and session == "logged_in":
        extra += " 前置要求未登录(guest)，当前 session 已为 logged_in，点赞/半屏登录类步骤需先 guest 再操作。"
    return (
        f"【运行上下文·登录态】{block}。"
        "校验登录相关预期时必须结合此结论与当前截图，不要只看局部控件。"
        f"{extra}"
    )
