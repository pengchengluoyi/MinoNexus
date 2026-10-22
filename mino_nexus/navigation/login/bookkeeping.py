"""登录证据的**记账**部分。

纯策略已归 `navigation/login/policy.py`（登录是页面事实，不是执行细节）。
本模块只保留需要读账号凭据、写 session 事件的函数：
`_now_valid` / `confirm_account_fill` / `note_submit_result` / `submission_current`。

为什么拆：策略与记账同文件时，`login_evidence_policy ↔ login_evidence` 互相 import，
而 `login_evidence` 又依赖 `loop.login_transition`，于是 `navigation` 无法持有策略而不反向依赖执行层。
拆开后本模块仍留在 `services/`，待 `records/eventlog` 叶子化后整体迁到 `records/evidence/`。
"""
from __future__ import annotations

from copy import deepcopy
import math
import time
from typing import Any

# 纯策略 re-export：既有 `from mino_nexus.navigation.login.bookkeeping import policy_for_context`
# 的调用点不需要一次性全改；新代码请直接 import `navigation.login.policy`。
from mino_nexus.navigation.login.policy import (  # noqa: F401
    DEFAULT_POLICY,
    MODES,
    clear_submission,
    configured_session_knowledge,
    normalize_policy,
    policy_for_context,
    submitted_account_enabled,
)


def _now_valid(ctx: Any, proof: Any, *, max_age: int = 90, observed_at: Any = None) -> bool:
    from mino_nexus.navigation.login.evidence import scope
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
    from mino_nexus.resources.account_credential_text import lease_email_address, lease_phone_digits
    from mino_nexus.navigation.login.evidence import scope
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
    from mino_nexus.records.eventlog.writer import active_writer
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
