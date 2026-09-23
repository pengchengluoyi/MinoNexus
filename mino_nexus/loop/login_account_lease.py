"""进入登录界面后再租号（不在 prep 强制 lease_account）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.core.log import SLog

TAG = "LoginAccountLease"

# 登录弹窗已出现时再租；略长于开跑时 fail-fast 的 0ms，避免号池短暂占用
LOGIN_SURFACE_LEASE_WAIT_MS = 8_000

_LOGIN_TAP_RE = re.compile(
    r"log\s*in|sign\s*in|登录|register|注册|\bEmail\b|邮箱|手机|验证码|password|密码",
    re.I,
)


def picked_account_ready_for_login(ctx: Any) -> bool:
    """已租号且具备当前 login.kind 所需凭据（email 或 phone）。"""
    lease = getattr(ctx, "resource_lease", None) or {}
    picked = getattr(ctx, "picked_account", None) or {}
    if not isinstance(picked, dict):
        picked = {}
    aid = str(picked.get("id") or picked.get("account_id") or "").strip()
    if not aid:
        aid = str(lease.get("account_id") or "").strip()
    if not aid:
        return False
    from mino_nexus.services.account_credential_text import (
        lease_email_address,
        lease_phone_digits,
        login_kind_from_ctx,
    )

    kind = login_kind_from_ctx(ctx)
    if kind == "email":
        return bool(lease_email_address(picked))
    return bool(lease_phone_digits(picked))


def login_surface_detected(
    ctx: Any,
    nodes: list[dict[str, Any]] | None = None,
    *,
    tap_hint: str = "",
) -> bool:
    """登录表单/弹窗已出现，或本步刚点了登录入口类控件。"""
    if str(tap_hint or "").strip() and _LOGIN_TAP_RE.search(str(tap_hint)):
        return True
    row = [n for n in (nodes or getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    if not row:
        return False
    from mino_nexus.loop.email_login_auto import _login_surface_visible

    return _login_surface_visible(ctx, row)


def try_lease_on_login_surface(
    ctx: Any,
    case: dict[str, Any] | None = None,
    *,
    wait_ms: int = LOGIN_SURFACE_LEASE_WAIT_MS,
) -> tuple[bool, str, str]:
    """登录界面触发租号。返回 (成功, 错误文案, 成功摘要)。"""
    if picked_account_ready_for_login(ctx):
        brief = str(getattr(ctx, "accounts_brief", "") or "").strip()
        return True, "", brief

    from mino_nexus.loop.session_ensure import account_need_from_case
    from mino_nexus.services.account_lease import format_accounts_brief, lease_for_context

    case_row = case if isinstance(case, dict) else getattr(ctx, "case", None)
    scene = getattr(ctx, "case_scene", None) if ctx is not None else None
    need = account_need_from_case(
        case_row if isinstance(case_row, dict) else {},
        scene if isinstance(scene, dict) else {},
    )
    if not need.get("need_account"):
        return True, "", ""

    params: dict[str, Any] = {}
    prompt = str(need.get("prompt") or "").strip()
    if prompt:
        params["precondition"] = prompt

    row, err = lease_for_context(
        ctx,
        params,
        ai_reasoning=prompt,
        need_facets=need,
        wait_ms=max(0, int(wait_ms or 0)),
    )
    if row:
        brief = format_accounts_brief(row)
        SLog.i(TAG, f"leased on login surface: {brief[:120]}")
        return True, "", brief
    msg = str(err or "租号失败").strip()
    from mino_nexus.services.account_credential_text import login_kind_from_ctx

    if login_kind_from_ctx(ctx) == "email" and "email" not in msg.lower():
        msg = f"{msg}；当前环境 login.kind=email，号池须至少有一个账号填写 email（仅手机号不够）"
    SLog.w(TAG, f"login-surface lease failed: {msg}")
    return False, msg, ""
