"""登录身份取证的上下文保存与恢复；图像只在本次 cursor 内存中保留。"""
from __future__ import annotations

import hashlib
import time
from typing import Any

NAV_LIMIT = 5
RESTORE_LIMIT = 5


def active_evidence_navigation(cursor: Any) -> bool:
    """Read-only login observation needs the current frame as well as structure."""
    if cursor is None:
        return False
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
    from mino_nexus.loop.milestones import read_state

    row = in_progress_milestone(read_state(cursor)) or {}
    return (row.get("source") == "login_evidence_observation"
            and str(row.get("id") or "").startswith("login_evidence_nav_"))


def remember_context(cursor: Any, ctx: Any, shot: Any) -> bool:
    if getattr(cursor, "login_navigation_reference", None):
        return True
    image = str(getattr(shot, "image_base64", "") or "")
    if not image:
        return False
    from mino_nexus.core.secret_redaction import sanitize
    labels = []
    for node in getattr(ctx, "nav_hierarchy_nodes", None) or []:
        if not isinstance(node, dict) or node.get("editable") or node.get("password") or node.get("sensitive"):
            continue
        value = str(node.get("text") or node.get("content_desc") or "").strip()
        if value and value not in labels:
            labels.append(value[:100])
        if len(labels) >= 12:
            break
    current = cursor.current() if callable(getattr(cursor, "current", None)) else None
    cursor.login_navigation_reference = {
        "context_id": id(ctx), "scope": _evidence_scope(ctx), "image_base64": image,
        "image_mime": str(getattr(shot, "image_mime", "") or "image/png"),
        "image_hash": hashlib.sha256(image.encode()).hexdigest()[:20],
        "anchor": sanitize("、".join(labels))[:1000],
        "expected": sanitize(str(getattr(current, "expected", "") or ""))[:300],
    }
    return True


def navigation_title(cursor: Any, inspection: dict[str, Any], *, restore: bool) -> str:
    if restore:
        ref = getattr(cursor, "login_navigation_reference", None) or {}
        return ("仅导航恢复到身份取证前的同一业务对象与页面，不能改用另一个帖子或靠点赞/关注等业务写入凑预期。"
                f"原页面可见要素：{ref.get('anchor') or '结合本次历史中的登录提交后页面'}；"
                f"原步骤结果：{ref.get('expected') or '恢复原业务上下文'}。"
                "恢复参考图是取证前真实页面，不得按预期文字想象页面。底栏切换后返回键可能退出应用；应按当前图选择原 Tab 或同一业务对象，禁止重复无效返回。"
                "本轮只走一步导航；程序会对照原图独立核验，未恢复时继续下一步。禁止退出登录、切换账号、输入凭据或修改账号。")
    from mino_nexus.services.login_evidence_policy import submitted_account_enabled, submission_current
    ctx = getattr(cursor, "run_context", None)
    if ctx is not None and submitted_account_enabled(ctx) and submission_current(ctx):
        return ("应用配置采用本次提交账号绑定，UI 身份不可观察。只导航一步到能看到登录专属个人资料或退出登录控件的页面，"
                "用于独立观察登录成功。不要寻找手机号、邮箱或唯一账号 ID，不要进入订单。仅只读导航，禁止退出登录、切换账号或业务写入。")
    identity_only = inspection.get("session") == "logged_in"
    purpose = ("当前已见已登录页面，但本人账号身份尚未核对。进入可显示手机号、邮箱或唯一账号标识的个人资料/账号安全/设置页；"
               "昵称和作品数不能证明是租用账号。如果已经在我的/个人中心，继续打开资料或设置，不能再次点击当前我的 Tab。"
               if identity_only else "先导航一步到能观察登录及账号身份的页面（依据当前界面和应用知识）；")
    state = getattr(cursor, "success_criteria_state", None) or {}
    visited = [str(row.get("navigation_target") or "") for row in state.get("milestones", [])
               if str(row.get("id") or "").startswith("login_evidence_nav_") and row.get("status") == "pass"]
    return (purpose + "已走过的入口：" + "；".join(x for x in visited if x)[-500:]
            + "。不要重复当前已打开的入口。仅执行只读导航，禁止退出登录、切换账号、输入凭据或修改账号；登录及身份仍由程序核验。")


def begin_restore(cursor: Any, ctx: Any, inspection: dict[str, Any], evidence: dict[str, Any]) -> bool:
    ref = getattr(cursor, "login_navigation_reference", None) or {}
    if not ref or ref.get("context_id") != id(ctx) or not evidence.get("login_state_verified"):
        return False
    cursor.login_navigation_proof = {"context_id": id(ctx), "at": time.monotonic(),
        "inspection": dict(inspection), "evidence": dict(evidence)}
    return True


def observe_restore(cursor: Any, ctx: Any, shot: Any, *, block_id: str, writer: Any = None) -> dict | None:
    proof = getattr(cursor, "login_navigation_proof", None)
    if not isinstance(proof, dict):
        return None
    ref = getattr(cursor, "login_navigation_reference", None) or {}
    failure = {"ok": False, "case_fail": True, "status": "blocked", "capability_id": "confirm_login_state",
               "milestone_id": "login_state", "step_id": "login_state", "block_id": block_id,
               "error_kind": "login_context_restore_unavailable", "session_logged": "flow_block"}
    from mino_nexus.services.login_evidence import evidence_current, can_commit
    if (not evidence_current(ctx, proof.get("evidence"))
            or not can_commit(ctx)
            or proof.get("context_id") != id(ctx) or ref.get("context_id") != id(ctx)
            or time.monotonic() - proof.get("at", 0) > 90
            or (getattr(ctx, "login_evidence", None) or {}).get("identity") == "mismatch"):
        return {**failure, "summary": "登录取证上下文已过期或身份冲突，不能沿用身份结论"}
    image = str(getattr(shot, "image_base64", "") or "")
    if not image:
        return {**failure, "summary": "缺少当前图像，无法核验业务上下文恢复"}
    from mino_nexus.loop.vision_assert import vision_assert_checkpoints
    result = vision_assert_checkpoints(
        expectation="当前页面是否已恢复为参考图中的同一业务对象和页面？忽略时钟、Toast、加载完成及无关动画差异。不能只因都含点赞/关注就把不同帖子视为同一对象；个人中心、资料或设置页不能代替原详情页。",
        image_base64=image, image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
        reference_image_base64=ref["image_base64"], reference_image_mime=ref["image_mime"],
        context_block="原页面文字锚点：" + str(ref.get("anchor") or "参考图为准"), run_context=ctx,
    )
    if writer:
        writer.append("login/context_restore_observation", {"source_image_hash": ref["image_hash"],
            "passed": bool(result.passed) and not result.error_kind and not result.parse_warnings,
            "error_kind": result.error_kind, "attempt": int(getattr(cursor, "login_context_restore_count", 0) or 0),
            "identity_observation_ref": proof["evidence"].get("observation_ref", "")})
    if result.error_kind or result.parse_warnings:
        return {**failure, "summary": "业务上下文恢复观察不可核验，保留原登录证据但不能结束步骤"}
    if not evidence_current(ctx, proof.get("evidence")) or not can_commit(ctx):
        return {**failure, "summary": "恢复观察期间登录证据失效，不能沿用旧身份结论"}
    if result.passed:
        # 保留身份证据的原 observation_ref；不把旧身份结论冒充为当前图观察。
        ctx.login_evidence = dict(proof["evidence"])
        return {"restored": True}
    from mino_nexus.loop.flow_block_runner_v2 import _schedule_login_evidence_navigation
    if _schedule_login_evidence_navigation(cursor, ctx=ctx, shot=shot, block_id=block_id, writer=writer, restore=True):
        return {**failure, "case_fail": False, "defer": True, "status": "wait", "error_kind": "",
                "summary": "身份已核验，业务上下文尚未恢复，继续有界返回导航"}
    return {**failure, "summary": "身份已核验，但业务上下文在有界导航内未恢复；不能判定业务通过"}


def navigation_binding(cursor: Any, ctx: Any, writer: Any) -> dict | None:
    from mino_nexus.loop.llm_step_context import step_scope_key
    from mino_nexus.loop.milestones import read_state
    from mino_nexus.loop.visual_review import _prior_still_current
    prior = getattr(cursor, "visual_prior", None)
    ref = getattr(cursor, "navigation_dispatch_reference", None)
    if not isinstance(prior, dict) or not isinstance(ref, dict) or writer is None:
        return None
    mid = str(prior.get("milestone_id") or "")
    turn = int(getattr(writer, "turn", 0) or 0)
    if (not mid.startswith(("login_evidence_nav_", "login_context_restore_", "prep_reauth_nav_"))
            or not prior.get("attempt_ref") or not prior.get("expected")
            or ref.get("context_id") != id(ctx) or ref.get("scope") != step_scope_key(cursor)
            or ref.get("milestone_id") != mid or ref.get("attempt_ref") != prior.get("attempt_ref")
            or ref.get("turn") != prior.get("dispatch_turn") or ref.get("turn", 0) <= 0
            or turn <= ref.get("turn", 0) or not ref.get("image_base64")
            or not _prior_still_current(read_state(cursor), mid)):
        return None
    return {"prior": dict(prior), "reference": dict(ref), "scope": step_scope_key(cursor),
            "attempt_ref": prior["attempt_ref"], "milestone_id": mid, "expected": prior["expected"]}


def rejected_navigation_action(cursor: Any, ctx: Any, cap: str, params: Any) -> bool:
    from mino_nexus.loop.llm_step_context import step_scope_key
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
    from mino_nexus.loop.milestones import read_state

    row = in_progress_milestone(read_state(cursor)) or {}
    rejected = getattr(cursor, "navigation_rejected_action", None) or {}
    if (not str(row.get("id") or "").startswith(("login_evidence_nav_", "login_context_restore_", "prep_reauth_nav_"))
            or rejected.get("context_id") != id(ctx) or rejected.get("scope") != step_scope_key(cursor)
            or cap != rejected.get("capability_id")):
        return False
    old, new = rejected.get("params") or {}, params if isinstance(params, dict) else {}
    if cap == "press_key":
        return bool(old.get("key")) and str(old["key"]).upper() == str(new.get("key") or "").upper()
    if cap == "swipe_direction":
        return bool(old.get("direction")) and old["direction"] == new.get("direction")
    return bool(old) and old == new


def observe_navigation_step(cursor: Any, ctx: Any, shot: Any, *, writer: Any = None) -> bool:
    """Page-change proof only; identity and business context are checked afterwards."""
    from mino_nexus.loop.llm_step_context import step_scope_key
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
    from mino_nexus.loop.milestones import read_state
    from mino_nexus.loop.visual_review import _confirm_visual_prior, _prior_still_current
    from mino_nexus.loop.obstacle_review import proof_accepted
    image = str(getattr(shot, "image_base64", "") or "")
    image_ref = str(getattr(ctx, "decision_screenshot_hash", "") or "")
    candidate = navigation_binding(cursor, ctx, writer)
    if (candidate and image and image_ref
            and getattr(cursor, "navigation_reviewed_attempt", None) != candidate["attempt_ref"]):
        cursor.navigation_reviewed_attempt = candidate["attempt_ref"]
        from mino_nexus.loop.vision_assert import vision_assert_checkpoints
        ref = candidate["reference"]
        result = vision_assert_checkpoints(
            expectation=candidate["expected"], image_base64=image,
            image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
            reference_image_base64=ref["image_base64"], reference_image_mime=ref["image_mime"],
            checkpoints=[{"point_id": "expected_action_result", "description": candidate["expected"]}],
            context_block='{"reference_relation":"before_navigation","verification":"page_change_only"}',
            run_context=ctx,
        )
        current = (getattr(cursor, "visual_prior", None) == candidate["prior"]
                   and step_scope_key(cursor) == candidate["scope"]
                   and str(getattr(ctx, "decision_screenshot_hash", "") or "") == image_ref
                   and _prior_still_current(read_state(cursor), candidate["milestone_id"]))
        passed = current and proof_accepted(result)
        writer.append("login/navigation_result", {"milestone_id": candidate["milestone_id"],
            "attempt_ref": candidate["attempt_ref"], "passed": passed, "proof_current": current,
            "before_observation_ref": ref.get("observation_ref", ""), "after_observation_ref": image_ref,
            "error_kind": result.error_kind, "parse_warnings": list(result.parse_warnings),
            "checkpoint_results": result.checkpoint_results, "identity_verified": False,
            "business_context_verified": False, "execution_started": False})
        if passed:
            cursor.navigation_rejected_action = None
            state = read_state(cursor)
            state.pop("navigation_rejected_action", None)
            from mino_nexus.loop.milestones import write_state
            write_state(cursor, state)
            _confirm_visual_prior(cursor, ctx, candidate["milestone_id"],
                capability_id=str(candidate["prior"].get("capability_id") or ""), writer=writer,
                in_prep=step_scope_key(cursor)[1] == "prep")
            cursor.visual_prior = None
            cursor.pending_visual_review = None
            cursor.navigation_dispatch_reference = None
            cursor.navigation_frame_candidate = None
            return True
        if current and not result.error_kind and not result.parse_warnings and result.passed is False:
            # A read-only navigation with no visible effect is not business failure.
            # Keep the original check pending and consume the existing bounded budget.
            from mino_nexus.loop.milestones import write_state
            from mino_nexus.loop.milestone_orchestrator import advance_in_progress_focus
            prior = candidate["prior"]
            rejected = {"context_id": id(ctx), "scope": step_scope_key(cursor),
                        "capability_id": str(prior.get("capability_id") or ""),
                        "params": dict(prior.get("params") or {})}
            cursor.navigation_rejected_action = rejected
            state = read_state(cursor)
            for row in state.get("milestones") or []:
                if row.get("id") == candidate["milestone_id"]:
                    row.update(status="skipped", evidence="no_visible_navigation_change")
            state["navigation_rejected_action"] = {k: rejected[k] for k in ("capability_id", "params")}
            state["exit_allowed"] = False
            write_state(cursor, state)
            cursor.visual_prior = None
            cursor.pending_visual_review = None
            cursor.navigation_dispatch_reference = None
            cursor.navigation_frame_candidate = None
            cursor.correction_hint = "上一导航经前后图核验没有可见变化，不能重复该动作；请按当前截图选择其他可见入口或相反滚动方向。登录及业务仍未确认。"
            advance_in_progress_focus(cursor, writer=writer)
            writer.append("login/navigation_no_change", {"milestone_id": candidate["milestone_id"],
                "attempt_ref": candidate["attempt_ref"], "status": "skipped",
                "identity_verified": False, "business_context_verified": False})
            return True
    # Capture only the current frame. defer_visual_pass binds it to the actual dispatched attempt.
    focus = in_progress_milestone(read_state(cursor)) or {}
    mid = str(focus.get("id") or "")
    if writer is not None and image and image_ref and mid.startswith(("login_evidence_nav_", "login_context_restore_", "prep_reauth_nav_")):
        cursor.navigation_frame_candidate = {"context_id": id(ctx), "milestone_id": mid,
            "scope": step_scope_key(cursor), "turn": int(getattr(writer, "turn", 0) or 0),
            "image_base64": image, "image_mime": str(getattr(shot, "image_mime", "") or "image/png"),
            "observation_ref": image_ref}
    else:
        cursor.navigation_frame_candidate = None
    return False


def _evidence_scope(ctx):
    from mino_nexus.services.login_evidence import scope
    return scope(ctx)


def restore_execution_reference(cursor, ctx):
    """Reference image is an immutable navigation target, never current evidence."""
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
    from mino_nexus.loop.milestones import read_state
    from mino_nexus.services.login_evidence import can_commit
    row = in_progress_milestone(read_state(cursor)) or {}
    ref = getattr(cursor, "login_navigation_reference", None) or {}
    if (not str(row.get("id") or "").startswith("login_context_restore_")
            or ref.get("context_id") != id(ctx) or ref.get("scope") != _evidence_scope(ctx)
            or not ref.get("image_base64") or not can_commit(ctx)):
        return {}
    return {"image_base64": ref["image_base64"], "image_mime": ref["image_mime"]}
