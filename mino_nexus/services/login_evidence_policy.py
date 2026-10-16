"""Application-owned login evidence policy; the default requires visible identity."""
from __future__ import annotations
from copy import deepcopy
import math
import time
from typing import Any

DEFAULT_POLICY = {"mode": "ui_identity", "env_profiles": [], "env_surfaces": [],
                  "authenticated_content_hint": "", "logout_navigation_hint": ""}


def normalize_policy(value: Any) -> dict:
    if value is None:
        return deepcopy(DEFAULT_POLICY)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError("登录校验配置字段无效")
    mode = value.get("mode", "ui_identity")
    if mode not in {"ui_identity", "submitted_account"}:
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


def _now_valid(ctx: Any, proof: Any, *, max_age: int = 90, observed_at: Any = None) -> bool:
    from mino_nexus.services.login_evidence import scope
    if not isinstance(proof, dict) or proof.get("scope") != scope(ctx):
        return False
    at = proof.get("at")
    now = time.monotonic()
    reference = now if observed_at is None else observed_at
    if type(reference) not in (int, float) or not math.isfinite(reference) or reference > now:
        return False
    age = reference - at if type(at) in (int, float) else float("inf")
    return (math.isfinite(age) and 0 <= age <= max_age
            and bool(proof.get("attempt_ref")) and bool(proof.get("observation_ref")))


def confirm_account_fill(ctx: Any, params: dict, *, attempt_ref: str, observation_ref: str) -> bool:
    """Called only after the actual input has passed its existing result review."""
    clear_submission(ctx)
    if not submitted_account_enabled(ctx) or not attempt_ref or not observation_ref:
        return False
    from mino_nexus.services.account_credential_text import lease_email_address, lease_phone_digits
    from mino_nexus.services.login_evidence import scope
    lease = getattr(ctx, "resource_lease", None) or {}
    account = getattr(ctx, "picked_account", None) or {}
    account_id = str(lease.get("account_id") or "")
    if not account_id or str(account.get("id") or "") != account_id:
        return False
    field = str(params.get("field") or "").lower()
    actual = str(params.get("text") or "").strip()
    expected = lease_email_address(account) if field in {"email", "login_email"} else lease_phone_digits(account) if field in {"phone", "login_phone"} else ""
    if not actual or actual != expected:
        return False
    ctx.login_account_fill_proof = {"scope": scope(ctx), "at": time.monotonic(),
        "attempt_ref": attempt_ref, "observation_ref": observation_ref}
    return True


def note_submit_result(ctx: Any, *, passed: bool, attempt_ref: str, observation_ref: str) -> bool:
    """A successful device receipt binds the reviewed input; it does not prove login."""
    ctx.login_submission_proof = {}
    fill = getattr(ctx, "login_account_fill_proof", None)
    if (not passed or not submitted_account_enabled(ctx) or not _now_valid(ctx, fill)
            or not attempt_ref or not observation_ref):
        return False
    ctx.login_submission_proof = {**deepcopy(fill), "at": time.monotonic(),
        "fill_attempt_ref": fill["attempt_ref"], "attempt_ref": attempt_ref,
        "observation_ref": observation_ref}
    from mino_nexus.loop.observe.session_log import active_writer
    writer = active_writer()
    if writer:
        writer.append("login/submission_binding", {"source": "submitted_account",
            "fill_attempt_ref": fill["attempt_ref"], "submit_attempt_ref": attempt_ref,
            "ui_identity": "unknown", "authentication_verified": False})
    return True


def submission_current(ctx: Any, *, observed_at: Any = None) -> bool:
    proof = getattr(ctx, "login_submission_proof", None)
    if not submitted_account_enabled(ctx) or not _now_valid(ctx, proof, observed_at=observed_at):
        return False
    # Positive authentication must be observed after this device submission.
    return observed_at is None or (type(observed_at) in (int, float) and observed_at >= proof["at"])


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
