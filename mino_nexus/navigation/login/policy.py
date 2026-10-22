"""登录成功口径（纯策略）：只读 `ctx` / 普通 dict，不写库、不发事件、不碰设备。

归属：登录是**页面事实**，所以策略归 `navigation/`。原先它躺在 `services/`，与
`login_evidence`（证据落库）互相 import 形成环；拆开后本模块只依赖标准库。

不在这里的：`confirm_account_fill` / `note_submit_result` / `submission_current` 等
需要读账号凭据、写 session 事件的函数，留在 `services/login_evidence_policy.py`，
待 `records/eventlog` 落地后迁到 `records/evidence/`。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_POLICY = {"mode": "ui_identity", "env_profiles": [], "env_surfaces": [],
                  "authenticated_content_hint": "", "logout_navigation_hint": ""}

MODES = frozenset({"ui_identity", "submitted_account"})


def normalize_policy(value: Any) -> dict:
    """校验并归一；字段集合封闭，未知字段直接拒绝，避免拼写错误被静默忽略。"""
    if value is None:
        return deepcopy(DEFAULT_POLICY)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError("登录校验配置字段无效")
    mode = value.get("mode", "ui_identity")
    if mode not in MODES:
        raise ValueError("未知登录校验模式")
    result = {"mode": mode}
    for key, label in (("authenticated_content_hint", "登录专属页面说明"),
                       ("logout_navigation_hint", "退出登录导航说明")):
        hint = value.get(key, "")
        if not isinstance(hint, str) or len(hint) > 1200:
            raise ValueError(f"{label}必须是1200字以内的文字")
        result[key] = hint.strip()
    for key in ("env_profiles", "env_surfaces"):
        raw = value.get(key, [])
        if (not isinstance(raw, list) or len(raw) > 32
                or any(not isinstance(x, str) or not x.strip() or len(x) > 80 for x in raw)):
            raise ValueError("登录校验环境与渠道必须是有效字符串列表")
        result[key] = sorted(set(x.strip() for x in raw))
    if mode == "submitted_account" and not all(result[k] for k in ("env_profiles", "env_surfaces")):
        raise ValueError("提交账号绑定必须明确指定生效环境和渠道")
    return result


def policy_for_context(ctx: Any) -> dict:
    try:
        return normalize_policy((getattr(ctx, "playbook", None) or {}).get("login_evidence_policy"))
    except (ValueError, TypeError, AttributeError):
        return deepcopy(DEFAULT_POLICY)


def submitted_account_enabled(ctx: Any) -> bool:
    policy = policy_for_context(ctx)
    return (policy["mode"] == "submitted_account"
            and str(getattr(ctx, "env_profile", "") or "") in policy["env_profiles"]
            and str(getattr(ctx, "env_surface", "") or "") in policy["env_surfaces"])


def clear_submission(ctx: Any) -> None:
    ctx.login_account_fill_proof = {}
    ctx.login_submission_proof = {}


def configured_session_knowledge(ctx: Any) -> str:
    """App facts belong to the scoped persisted configuration, never source code."""
    import json

    policy = policy_for_context(ctx)
    if (str(getattr(ctx, "env_profile", "") or "") not in policy["env_profiles"]
            or str(getattr(ctx, "env_surface", "") or "") not in policy["env_surfaces"]):
        return ""
    return json.dumps({"app_login_evidence_config": {
        "account_binding_source": policy["mode"],
        "ui_identity_observability": "unavailable" if policy["mode"] == "submitted_account" else "visible_identity_required",
        "authenticated_content_hint": policy["authenticated_content_hint"],
        "logout_control_location_hint": policy["logout_navigation_hint"],
    }}, ensure_ascii=False)
