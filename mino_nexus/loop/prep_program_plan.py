"""前置程序计划：Claim / prep_flow → 子里程碑 + prep_program_plan（供 agent-vision-plan 只读）。"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestones import (
    _log_milestones_snapshot,
    milestones_empty,
    read_state,
    write_state,
    _sync_scope,
)
from mino_nexus.resources.session.ensure import account_need_from_case
from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx
from mino_nexus.resources.case_resource_claim import _case_resource_key
from mino_nexus.resources.case_resource_key_catalog import catalog_payload
from mino_nexus.resources.resource_preflight import claim_requires_clear_cache, _claim_from_case










def milestones_from_prep_program(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        sid = str(step.get("id") or "").strip()
        if not sid:
            continue
        row: dict[str, Any] = {
            "id": sid,
            "title": str(step.get("title") or sid).strip()[:240],
            "kind": str(step.get("kind") or "hook").strip().lower(),
            "status": "pending",
            "optional": bool(step.get("optional")),
            "source": "prep_program",
            "prep_flow_id": str(step.get("prep_flow_id") or ""),
        }
        if step.get("hook_cap"):
            row["hook_cap"] = str(step.get("hook_cap") or "")
        if step.get("required_session"):
            row["required_session"] = str(step.get("required_session") or "")
        if str(step.get("status") or "") == "skipped":
            row["status"] = "skipped"
            row["skip_reason"] = str(step.get("skip_reason") or "channel_absent")
        if step.get("evaluate"):
            row["evaluate"] = str(step.get("evaluate") or "")
        block_id = str(step.get("block_id") or "").strip()
        if block_id:
            from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps
            from mino_nexus.loop.milestones import milestones_from_flow_steps

            app_id = str(step.get("app_id") or "").strip()
            block_steps = resolve_effective_steps(app_id=app_id, block_id=block_id)
            expanded = milestones_from_flow_steps(block_steps, block_id=block_id)
            for item in expanded:
                item = {**item, "source": "prep_block", "source_block": block_id}
                rows.append(item)
            continue
        rows.append(row)
    return rows


def sync_prep_internal_milestones(cursor: Any, ctx: Any) -> bool:
    """已满足的 internal / 前置 ctx 标记 → 自动 pass 对应子里程碑。"""
    _, phase = step_scope_key(cursor)
    if phase != "prep":
        return False
    state = _sync_scope(cursor, read_state(cursor))
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    if not ms:
        return False
    sn = str(getattr(ctx, "sn", "") or "").strip()
    cache_passed = any(
        isinstance(row, dict)
        and str(row.get("id") or "") == "prep_clear_cache"
        and str(row.get("status") or "") == "pass"
        for row in ms
    )
    cleared = bool(getattr(ctx, "prep_clear_done", False)) or cache_passed
    launch_ok = bool(getattr(ctx, "app_launch_confirmed", False))
    changed = False
    for row in ms:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") in ("pass", "skipped", "failed"):
            continue
        mid = str(row.get("id") or "")
        kind = str(row.get("kind") or "").strip().lower()
        if mid == "prep_pick_device" and kind == "internal" and sn:
            row["status"] = "pass"
            row["evidence"] = f"sn={sn[:16]}"
            changed = True
        elif mid == "prep_clear_cache" and cleared:
            row["status"] = "pass"
            row["evidence"] = "clear_app_cache"
            cache_passed = True
            changed = True
        elif mid == "prep_launch_app" and launch_ok:
            hook = str(row.get("hook_cap") or "").strip()
            if hook in ("launch_app", "open_url", "open_app"):
                row["status"] = "pass"
                row["evidence"] = "launch_confirmed"
                changed = True
                plat = str(getattr(ctx, "platform", "") or "").strip().lower()
                if plat in ("web", "browser", "playwright"):
                    from mino_nexus.loop.login_state_probe import note_web_context_start

                    note_web_context_start(ctx)
    if not changed:
        return False
    state["milestones"] = ms
    state["status"] = "in_progress"
    write_state(cursor, state)
    return True


def seed_prep_milestones_from_claim(
    cursor: Any,
    ctx: Any,
    case: dict[str, Any] | None,
    *,
    writer: Any = None,
) -> dict[str, Any] | None:
    """prep 阶段首轮：用 Claim 种子里程碑并缓存 prep_program_plan。"""
    _, phase = step_scope_key(cursor)
    if phase != "prep":
        return None
    state = _sync_scope(cursor, read_state(cursor))
    if not milestones_empty(state):
        sync_prep_internal_milestones(cursor, ctx)
        return getattr(cursor, "prep_program_plan", None)

    scene = dict(getattr(ctx, "case_scene", None) or {})
    pre = str(
        getattr(cursor, "precondition_text", "")
        or getattr(cursor, "precondition", "")
        or (case or {}).get("precondition")
        or ""
    ).strip()
    claim = resolve_prep_claim(case, ctx)
    plan = build_prep_program_plan(
        claim=claim,
        scene=scene,
        precondition=pre,
        ctx=ctx,
        case=case,
    )
    rows = milestones_from_prep_program(plan)
    if not rows:
        return plan
    want = str((plan.get("flags") or {}).get("device_session") or "").strip()
    if want in ("logged_in", "guest"):
        scene_obj = getattr(ctx, "case_scene", None)
        if isinstance(scene_obj, dict) and str(scene_obj.get("required_session") or "any") in ("", "any"):
            scene_obj["required_session"] = want
    state["milestones"] = rows
    state["status"] = "in_progress"
    write_state(cursor, state)
    setattr(cursor, "prep_program_plan", plan)
    sync_prep_internal_milestones(cursor, ctx)
    from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress

    ensure_single_in_progress(cursor, writer=writer)
    _log_milestones_snapshot(writer, read_state(cursor), source="program_seed:prep_claim")
    if writer is not None:
        try:
            writer.append(
                "milestone/program_seed",
                {
                    "phase": "prep",
                    "source": "prep_claim",
                    "step_count": len(rows),
                    "flags": plan.get("flags"),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return plan


def prep_program_plan_json(cursor: Any, case: dict[str, Any] | None, ctx: Any) -> str:
    plan = getattr(cursor, "prep_program_plan", None)
    if not isinstance(plan, dict) or not plan.get("steps"):
        claim = resolve_prep_claim(case, ctx)
        scene = dict(getattr(ctx, "case_scene", None) or {})
        pre = str(getattr(cursor, "precondition", "") or (case or {}).get("precondition") or "")
        plan = build_prep_program_plan(
            claim=claim,
            scene=scene,
            precondition=pre,
            ctx=ctx,
            case=case,
        )
        setattr(cursor, "prep_program_plan", plan)
    return json.dumps(plan, ensure_ascii=False, indent=2, default=str)


def schedule_prep_reauthentication(cursor: Any, ctx: Any, pending: dict[str, Any], *, writer: Any = None) -> bool:
    """Bounded, app-configured logout navigation; the original identity hook stays pending."""
    from mino_nexus.navigation.login.evidence import EVIDENCE_MAX_AGE_SEC, scope
    from mino_nexus.navigation.login.bookkeeping import policy_for_context, submitted_account_enabled, submission_current

    if (step_scope_key(cursor)[1] != "prep" or pending.get("source") != "prep_program"
            or pending.get("hook_cap") != "confirm_login_state" or pending.get("required_session") != "logged_in"
            or not submitted_account_enabled(ctx) or submission_current(ctx)):
        return False
    from mino_nexus.loop.step_flow_scope import prep_login_preservation_reason

    if prep_login_preservation_reason(ctx):
        return False
    policy = policy_for_context(ctx)
    hint = policy["logout_navigation_hint"]
    current_scope = scope(ctx)
    account = getattr(ctx, "picked_account", None) or {}
    evidence = getattr(ctx, "login_evidence", None) or {}
    at = evidence.get("observed_at_monotonic")
    age = time.monotonic() - at if type(at) in (int, float) else float("inf")
    deadline = getattr(ctx, "case_deadline_ts", 0) or 0
    if (not hint or not current_scope["account_id"] or str(account.get("id") or "") != current_scope["account_id"]
            or evidence.get("scope") != current_scope or not evidence.get("observation_ref")
            or not math.isfinite(age) or not 0 <= age <= EVIDENCE_MAX_AGE_SEC
            or evidence.get("identity") in {"mismatch", "match"} or (deadline and time.time() >= deadline)):
        return False
    guard_scope = {**current_scope, "app_id": str(getattr(ctx, "app_id", "") or ""),
                   "env_profile": str(getattr(ctx, "env_profile", "") or ""),
                   "env_surface": str(getattr(ctx, "env_surface", "") or "")}
    guard = getattr(cursor, "prep_reauthentication_guard", None)
    if guard is None:
        if evidence.get("authentication") != "logged_in" or evidence.get("identity") != "unknown":
            return False
        guard = {"context_id": id(ctx), "scope": guard_scope, "hint": hint, "steps": 0}
    if (guard.get("context_id") != id(ctx) or guard.get("scope") != guard_scope or guard.get("hint") != hint
            or guard.get("completed") or guard.get("steps", 0) >= 6):
        return False
    state = read_state(cursor)
    rows = [dict(row) for row in state.get("milestones") or [] if isinstance(row, dict)]
    mid = str(pending.get("id") or "")
    index = next((i for i, row in enumerate(rows) if row.get("id") == mid
                  and row.get("status") in {"pending", "in_progress"}), None)
    if index is None or any(str(row.get("id") or "").startswith("prep_reauth_nav_")
                            and row.get("status") not in {"pass", "skipped"} for row in rows):
        return False
    number = guard["steps"] + 1
    nav_id = f"prep_reauth_nav_{number}"
    rows[index]["status"] = "pending"
    rows.insert(index, {"id": nav_id, "kind": "visual_action", "status": "in_progress", "optional": False,
                        "source": "prep_program", "prep_flow_id": "reauthentication",
                        "title": "按应用配置执行一步退出登录导航", "navigation_target": hint,
                        "exec_caps": ["tap_element", "swipe_direction", "press_key"],
                        "expected_result": "比较操作前后两图，页面切换、滚动位置变化、退出确认弹窗出现或退出确认弹窗消失，任一项可见变化即可；仅核验本步导航变化，随后由程序独立核验登录态"})
    state.update(milestones=rows, status="in_progress", exit_allowed=False,
                 reauthentication={"mode": "logout_then_login", "navigation_hint": hint,
                                   "step": number, "max_steps": 6, "milestone_id": nav_id})
    cursor.prep_reauthentication_guard = {**guard, "steps": number}
    write_state(cursor, state)
    if writer is not None:
        writer.append("prep/reauthentication", {"milestone_id": nav_id, "verification_milestone_id": mid,
                      "step": number, "max_steps": 6, "status": "pending", "account_binding_verified": False})
    return True


def finish_prep_reauthentication(cursor: Any) -> None:
    guard = getattr(cursor, "prep_reauthentication_guard", None)
    if isinstance(guard, dict):
        cursor.prep_reauthentication_guard = {**guard, "completed": True}
        state = read_state(cursor)
        state.pop("reauthentication", None)
        write_state(cursor, state)


def schedule_prep_session_navigation(cursor: Any, ctx: Any, pending: dict[str, Any], *, writer: Any = None) -> bool:
    """Keep the original prep check pending while observing its configured location."""
    import math
    import time
    from mino_nexus.navigation.login.evidence import EVIDENCE_MAX_AGE_SEC, scope
    from mino_nexus.navigation.login.bookkeeping import policy_for_context, submitted_account_enabled

    if (step_scope_key(cursor)[1] != "prep" or pending.get("source") != "prep_program"
            or pending.get("hook_cap") != "confirm_login_state" or not submitted_account_enabled(ctx)):
        return False
    policy = policy_for_context(ctx)
    hint = policy["authenticated_content_hint"] or policy["logout_navigation_hint"]
    after_reauthentication = bool(getattr(cursor, "prep_reauthentication_guard", None))
    if after_reauthentication:
        hint = policy["logout_navigation_hint"] or hint
    evidence = getattr(ctx, "login_evidence", None) or {}
    at = evidence.get("observed_at_monotonic")
    age = time.monotonic() - at if type(at) in (int, float) else float("inf")
    current_scope = {**scope(ctx), "app_id": str(getattr(ctx, "app_id", "") or ""),
                     "env_profile": str(getattr(ctx, "env_profile", "") or ""),
                     "env_surface": str(getattr(ctx, "env_surface", "") or "")}
    if (not hint or evidence.get("authentication") != "unknown" or evidence.get("identity") == "mismatch"
            or evidence.get("scope") != scope(ctx) or not evidence.get("observation_ref")
            or not math.isfinite(age) or not 0 <= age <= EVIDENCE_MAX_AGE_SEC
            or (getattr(ctx, "case_deadline_ts", 0) or 0) and time.time() >= ctx.case_deadline_ts):
        return False
    targets = [policy["authenticated_content_hint"], policy["logout_navigation_hint"]]
    guard = getattr(cursor, "prep_session_navigation_guard", None) or {
        "context_id": id(ctx), "scope": current_scope, "hint": hint, "policy_targets": targets, "steps": 0}
    if (guard.get("context_id") != id(ctx) or guard.get("scope") != current_scope
            or guard.get("policy_targets") != targets or guard.get("steps", 0) >= 5):
        return False
    state = read_state(cursor)
    rows = [dict(row) for row in state.get("milestones") or [] if isinstance(row, dict)]
    index = next((i for i, row in enumerate(rows) if row.get("id") == pending.get("id")
                  and row.get("status") in {"pending", "in_progress"}), None)
    if index is None or any(str(row.get("id") or "").startswith("login_evidence_nav_prep_")
                            and row.get("status") not in {"pass", "skipped"} for row in rows):
        return False
    number = guard["steps"] + 1
    nav_id = f"login_evidence_nav_prep_{number}"
    rows[index]["status"] = "pending"
    rows.insert(index, {"id": nav_id, "kind": "visual_action", "status": "in_progress", "optional": False,
        "source": "login_evidence_observation", "navigation_target": hint,
        "after_reauthentication_navigation": after_reauthentication,
        "previous_navigation_actions": [row.get("navigation_action", "") for row in rows
            if str(row.get("id") or "").startswith("prep_reauth_nav_") and row.get("status") == "pass"],
        "title": "按应用配置只读导航一步到可观察当前登录态的位置，禁止退出或换号",
        "exec_caps": ["tap_element", "swipe_direction", "press_key"],
        "expected_result": "本次只读导航引起可见页面或滚动位置变化；随后由原前置检查核验登录态"})
    state.update(milestones=rows, status="in_progress", exit_allowed=False)
    cursor.prep_session_navigation_guard = {**guard, "hint": hint, "steps": number}
    write_state(cursor, state)
    if writer is not None:
        writer.append("prep/session_observe", {"milestone_id": pending.get("id"),
            "observation_milestone_id": nav_id, "attempt": number, "limit": 5,
            "session": "unknown", "status": "pending", "login_verified": False})
    return True


def active_reauthentication_navigation(cursor: Any) -> bool:
    if cursor is None or step_scope_key(cursor)[1] != "prep":
        return False
    state = read_state(cursor)
    marker = state.get("reauthentication") or {}
    return marker.get("mode") == "logout_then_login" and any(
        isinstance(row, dict) and row.get("status") == "in_progress" and row.get("source") == "prep_program"
        and row.get("prep_flow_id") == "reauthentication" and row.get("id") == marker.get("milestone_id")
        for row in state.get("milestones") or [])


def resource_claim_json_for_plan(case: dict[str, Any] | None, ctx: Any) -> str:
    claim = resolve_prep_claim(case, ctx)
    if not claim:
        return "{}"
    slim = {
        "version": claim.get("version"),
        "platform": claim.get("platform"),
        "device_app": claim.get("device_app"),
        "account": {
            "env": (claim.get("account") or {}).get("env"),
            "required_session": (claim.get("account") or {}).get("required_session"),
        },
        "case_scene": claim.get("case_scene"),
    }
    return json.dumps(slim, ensure_ascii=False, indent=2, default=str)


from mino_nexus.casework.prep_plan import (  # noqa: F401,E402
    _account_clause,
    _device_session_clause,
    _launch_hook_cap,
    _pre_lines,
    _session_want,
    build_prep_program_plan,
    resolve_prep_claim,
)


from mino_nexus.casework.prep_plan import (  # noqa: F401,E402
    _claim_needs_lease,
    _env_clear_clause,
    _extra_prep_catalog_steps,
)
