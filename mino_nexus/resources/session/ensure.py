"""会话编排：按 CaseScene 租号 / 核对登录态 / 只走图上 logout 边。

不扫被测 App 文案。退出只认 NavFSM `action_type=logout`。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from mino_nexus.core.log import SLog
from mino_nexus.resources.session.scene_gate import (
    ensure_case_scene,
    required_session,
)
from mino_nexus.resources.accounts.pool_templates import infer_template_id_from_text
from mino_nexus.resources.accounts.requirement_compile import (
    TITLE_ACCOUNT_LOGIN,
    classify_precondition_title,
)

TAG = "SessionEnsure"
_SESSION_RE = re.compile(r"session=(\w+)", re.I)
_LINE_NUM = re.compile(r"^\s*\d+[.)、．]\s*")


def _account_session_hint_from_pre(pre: str) -> str:
    """只认「账号登录态」行；设备登录态不作为号池 session 提示。"""
    for line in re.split(r"[\n\r]+", str(pre or "")):
        chunk = _LINE_NUM.sub("", line.strip())
        if not chunk or ("：" not in chunk and ":" not in chunk):
            continue
        sep = "：" if "：" in chunk else ":"
        title, val = chunk.split(sep, 1)
        if classify_precondition_title(title) != TITLE_ACCOUNT_LOGIN:
            continue
        v = val.strip()
        if re.search(r"未登录|游客", v):
            return "guest"
        if re.search(r"已登录", v):
            return "logged_in"
    return ""


def account_need_from_case(
    case: dict[str, Any] | None,
    scene: dict[str, Any] | None,
) -> dict[str, Any]:
    row = ensure_case_scene(case or {}, scene if isinstance(scene, dict) else None)
    pre = str(
        row.get("precondition")
        or (scene or {}).get("precondition")
        or (case or {}).get("precondition")
        or (case or {}).get("precondition_raw")
        or ""
    ).strip()
    session = _account_session_hint_from_pre(pre)
    rk = (case or {}).get("resource_key") if isinstance(case, dict) else None
    if not session and isinstance(rk, dict):
        acc = rk.get("account") if isinstance(rk.get("account"), dict) else {}
        raw = str(acc.get("required_session") or "").strip().lower()
        if raw in ("logged_in", "logged_out", "guest"):
            session = "logged_in" if raw == "logged_in" else "guest"
    blob = pre
    profile = bool(re.search(r"资料|昵称|头像|profile", blob, re.I))
    address = bool(re.search(r"地址|收货|address", blob, re.I))
    template_id = str(
        row.get("account_template_id")
        or row.get("template_id")
        or (case or {}).get("account_template_id")
        or (case or {}).get("template_id")
        or ""
    ).strip()
    if not template_id:
        template_id = infer_template_id_from_text(pre)
    lease_requirements = row.get("lease_requirements")
    if not isinstance(lease_requirements, dict):
        lease_requirements = (case or {}).get("lease_requirements") if isinstance((case or {}).get("lease_requirements"), dict) else {}
    # A compiled resource contract separates device/session setup from account needs.
    # Free-form guest/browser setup text must not force an account lease.
    has_account_constraints = any(lease_requirements.get(key) for key in ("all", "prefer"))
    if isinstance(rk, dict):
        account = rk.get("account") if isinstance(rk.get("account"), dict) else {}
        constraints = account.get("requirements") if isinstance(account.get("requirements"), dict) else {}
        has_account_constraints = has_account_constraints or any(constraints.get(key) for key in ("all", "prefer"))
        need_account = bool(session or profile or address or template_id or has_account_constraints
                            or required_session(scene=row) == "logged_in")
    else:
        # Preserve legacy uncompiled cases until they have a structured contract.
        need_account = bool(session or pre or profile or address or template_id or has_account_constraints)
    return {
        "need_account": need_account,
        "session": session,
        "prompt": pre,
        "profile": profile,
        "address": address,
        "template_id": template_id,
        "requirements": dict(lease_requirements or {}),
    }


def parse_session_value(block: str) -> str:
    match = _SESSION_RE.search(str(block or ""))
    if not match:
        return ""
    return str(match.group(1) or "").strip().lower()


def session_mismatch_reason(
    *,
    scene: dict[str, Any] | None,
    session_block: str,
) -> Optional[str]:
    req = required_session(scene=scene)
    current = parse_session_value(session_block)
    from mino_nexus.resources.accounts.session_match import normalize_device_session

    if current:
        current = normalize_device_session(current)
    if not req or req == "any" or not current:
        return None
    if req == "guest" and current == "logged_in":
        return (
            "当前仍是已登录会话，本条要求未登录。"
            "请走路线图 logout 边或 run_flow_block(block_id=fb.global.app.restart) 后观察，禁止 signal_done。"
        )
    if req == "logged_in" and current in ("logged_out", "guest"):
        return (
            "当前未登录，本条要求已登录。"
            "请先完成登录（lease_account / get_otp / input_text），禁止 signal_done。"
        )
    return None


def ensure_case_account(ctx: Any, case: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
    """开跑时按场景租号；ctx 上账号与当前前置不一致时由 lease_for_context 重选。"""
    scene = getattr(ctx, "case_scene", None) or {}
    need = account_need_from_case(case or {}, scene if isinstance(scene, dict) else {})
    if not need.get("need_account"):
        return None, ""
    from mino_nexus.resources.accounts.lease import lease_for_context

    params: dict[str, Any] = {}
    prompt = str(need.get("prompt") or "").strip()
    if prompt:
        params["precondition"] = prompt
    observed = getattr(ctx, "account_probe_facets", None)
    obs_map = observed if isinstance(observed, dict) else None

    row, err = lease_for_context(
        ctx,
        params,
        ai_reasoning=prompt,
        need_facets=need,
        observed_by_account=obs_map,
    )
    if row:
        score = int(row.get("score") or 0)
        reason = str(row.get("reason") or "")
        if score < 0:
            SLog.w(TAG, f"auto-lease weak match score={score} reason={reason}")
        SLog.i(TAG, f"auto-leased {row.get('id') or row.get('phone') or '?'}")
        if ctx is not None:
            ctx.prep_lease_account_done = True
        return row, ""
    SLog.w(TAG, f"auto-lease skipped: {err}")
    return None, err or ""
