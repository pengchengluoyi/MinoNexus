"""前置程序计划：Claim / prep_flow → 子里程碑 + prep_program_plan（供 agent-vision-plan 只读）。"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestones import (
    _log_milestones_snapshot,
    milestone_v1_enabled,
    milestones_empty,
    read_state,
    write_state,
    _sync_scope,
)
from mino_nexus.loop.session_ensure import account_need_from_case
from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx
from mino_nexus.services.case_resource_claim import _case_resource_key
from mino_nexus.services.case_resource_key_catalog import catalog_payload
from mino_nexus.services.resource_preflight import claim_requires_clear_cache, _claim_from_case


def resolve_prep_claim(case: dict[str, Any] | None, ctx: Any) -> dict[str, Any]:
    if isinstance(case, dict):
        rk = _case_resource_key(case)
        if rk:
            return dict(rk)
        pre = str(case.get("precondition") or case.get("precondition_raw") or "").strip()
        if pre:
            from mino_nexus.services.case_resource_claim import ensure_resource_key_on_case

            pkg = str(getattr(ctx, "target_package", "") or getattr(ctx, "package_id", "") or "")
            env = str(getattr(ctx, "env", "") or "test")
            return dict(ensure_resource_key_on_case(case, package=pkg, env=env) or {})
    return dict(_claim_from_case(case or {}) or {})


def _launch_hook_cap(ctx: Any, claim: dict[str, Any]) -> str:
    return "launch_app"


_EMPTY_PRE_VALUE = frozenset({"", "无", "没有", "暂无", "不需要", "无要求", "任意", "any", "none"})


def _pre_lines(pre: str) -> list[tuple[str, str]]:
    from mino_nexus.services.case_resource_claim import _parse_precondition_lines

    return _parse_precondition_lines(pre)


def _session_want(val: str) -> str:
    from mino_nexus.services.case_resource_claim import _required_session_from_value

    want = _required_session_from_value(val)
    return want if want in ("logged_in", "guest") else ""


def _device_session_clause(pre: str) -> tuple[str, str]:
    """设备/应用登录态。返回 (logged_in|guest, 展示文案)。"""
    from mino_nexus.services.account_requirement_compile import (
        TITLE_DEVICE_LOGIN,
        classify_precondition_title,
    )

    for title, val in _pre_lines(pre):
        if classify_precondition_title(title) != TITLE_DEVICE_LOGIN:
            continue
        if str(val or "").strip().lower() in _EMPTY_PRE_VALUE:
            continue
        want = _session_want(val)
        if not want:
            continue
        label = f"{title}：{val}".strip("：") if title else str(val).strip()
        return want, label[:240]
    return "", ""


def _extra_prep_catalog_steps(pre: str, used_caps: set[str]) -> list[dict[str, Any]]:
    """编号前置行里目录能认出、固定模板又没收的事件，按 hook 追加。"""
    from mino_nexus.catalog.skill_channel import capability_dispatch_ok, canonical_cap
    from mino_nexus.catalog.registry import get_capability
    from mino_nexus.services.account_requirement_compile import TITLE_OTHER, classify_precondition_title
    from mino_nexus.services.case_resource_key_catalog import catalog_payload

    out: list[dict[str, Any]] = []
    known = {str(x) for x in (used_caps or set())}
    entries = [
        e
        for e in (catalog_payload().get("entries") or [])
        if isinstance(e, dict) and str(e.get("key_layer") or "") == "precondition"
    ]
    for title, val in _pre_lines(pre):
        if classify_precondition_title(title) != TITLE_OTHER:
            continue
        text = str(val or "").strip()
        if text.lower() in _EMPTY_PRE_VALUE:
            continue
        blob = f"{title}{text}"
        hit: dict[str, Any] | None = None
        best = 0
        for ent in entries:
            cat = str(ent.get("write_category") or "").strip()
            examples = [str(x).strip() for x in (ent.get("write_examples") or []) if str(x).strip()]
            score = 0
            if cat and cat in blob:
                score = max(score, len(cat))
            for ex in examples:
                if ex in blob or blob in ex:
                    score = max(score, len(ex))
            if score > best:
                best = score
                hit = ent
        if not hit or best < 2:
            continue
        caps = [str(x).strip() for x in (hit.get("config_keys") or []) if str(x).strip()]
        # config_keys also contains resource fields such as session/profile.
        # Only registered executable events may become extra prep hooks;
        # resource claims retain their own preflight and session-gate checks.
        cap = next((canonical_cap(c) for c in caps
                    if get_capability(canonical_cap(c)) is not None
                    and capability_dispatch_ok(c)), "")
        if not cap or cap in known:
            continue
        known.add(cap)
        label = f"{title}：{text}".strip("：") if title else text
        out.append(
            {
                "id": f"prep_extra_{cap}"[:64],
                "prep_flow_id": "catalog",
                "title": label[:240],
                "kind": "hook",
                "hook_cap": cap,
                "optional": False,
                "key_ref": str(hit.get("key_ref") or ""),
                "source_clause": label[:240],
            }
        )
    return out


def _account_clause(pre: str, case: dict[str, Any] | None, scene: dict[str, Any] | None) -> str:
    """账号登录态与账号与数据。文案保留字段名和状态。"""
    from mino_nexus.services.account_requirement_compile import (
        TITLE_ACCOUNT_DATA,
        TITLE_ACCOUNT_LOGIN,
        classify_precondition_title,
    )

    bits: list[str] = []
    for title, val in _pre_lines(pre):
        kind = classify_precondition_title(title)
        if kind not in (TITLE_ACCOUNT_DATA, TITLE_ACCOUNT_LOGIN):
            continue
        text = str(val or "").strip()
        if text.lower() in _EMPTY_PRE_VALUE:
            continue
        bits.append(f"{title}：{text}".strip("：") if title else text)
    if bits:
        return "；".join(bits)[:240]
    if _claim_needs_lease(case, scene):
        return "筛选账号"
    return ""


def _env_clear_clause(pre: str) -> str:
    from mino_nexus.services.account_requirement_compile import (
        TITLE_ENV_PERM,
        classify_precondition_title,
    )

    for title, val in _pre_lines(pre):
        if classify_precondition_title(title) != TITLE_ENV_PERM:
            continue
        text = str(val or "").strip()
        if not text or text.lower() in _EMPTY_PRE_VALUE:
            continue
        label = f"{title}：{text}".strip("：") if title else text
        return label[:240]
    return ""


def _claim_needs_lease(case: dict[str, Any] | None, scene: dict[str, Any] | None) -> bool:
    """与 ensure_case_account 近似：仅有设备/环境前置时不种 lease 里程碑。"""
    need = account_need_from_case(case or {}, scene if isinstance(scene, dict) else {})
    req = dict(need.get("requirements") or {})
    if req.get("all") or req.get("prefer"):
        return True
    if str(need.get("session") or "").strip():
        return True
    if need.get("profile") or need.get("address"):
        return True
    if str(need.get("template_id") or "").strip():
        return True
    claim = resolve_prep_claim(case, None) if isinstance(case, dict) else {}
    acc = claim.get("account") if isinstance(claim.get("account"), dict) else {}
    ar = acc.get("requirements") if isinstance(acc.get("requirements"), dict) else {}
    return bool(ar.get("all") or ar.get("prefer"))


def build_prep_program_plan(
    *,
    claim: dict[str, Any],
    scene: dict[str, Any] | None,
    precondition: str,
    ctx: Any,
    case: dict[str, Any] | None,
) -> dict[str, Any]:
    """与 catalog prep_flow 对齐的可执行步骤列表（程序真源，非 LLM 发明）。"""
    flow = (catalog_payload().get("prep_flow") or {}).get("steps") or []
    steps_out: list[dict[str, Any]] = []
    need_lease = bool(_account_clause(precondition, case, scene))
    device_want, device_label = _device_session_clause(precondition)
    need_clear = claim_requires_clear_cache(claim, scene, precondition)
    env_label = _env_clear_clause(precondition) if need_clear else ""
    launch_cap = _launch_hook_cap(ctx, claim)

    for row in flow:
        if not isinstance(row, dict):
            continue
        fid = str(row.get("id") or "").strip()
        label = str(row.get("label") or fid).strip()
        if fid == "pick_account" and not need_lease:
            continue
        if fid == "pick_account":
            steps_out.append(
                {
                    "id": "prep_lease_account",
                    "prep_flow_id": fid,
                    "title": _account_clause(precondition, case, scene) or label or "筛选账号",
                    "kind": "hook",
                    "hook_cap": "lease_account",
                    "optional": False,
                }
            )
        elif fid == "pick_device":
            # 设备渠道由 Nexus 按用例标签/平台派单，不进 LLM 里程碑。
            continue
        elif fid == "env_cleanup":
            clear_step = {
                "id": "prep_clear_cache",
                "prep_flow_id": fid,
                "title": env_label or label or "清除应用缓存",
                "kind": "hook",
                "hook_cap": "clear_app_cache",
                "optional": False,
            }
            if not need_clear:
                clear_step["status"] = "skipped"
                clear_step["skip_reason"] = "not_required"
            steps_out.append(clear_step)

    if not any(str(step.get("id") or "") == "prep_clear_cache" for step in steps_out):
        clear_step = {
            "id": "prep_clear_cache",
            "prep_flow_id": "env_cleanup",
            "title": env_label or "清除应用缓存",
            "kind": "hook",
            "hook_cap": "clear_app_cache",
            "optional": False,
        }
        if not need_clear:
            clear_step["status"] = "skipped"
            clear_step["skip_reason"] = "not_required"
        steps_out.append(clear_step)

    steps_out.append(
        {
            "id": "prep_launch_app",
            "prep_flow_id": "launch",
            "title": "打开被测应用",
            "kind": "hook",
            "hook_cap": launch_cap,
            "optional": False,
        }
    )
    if device_want:
        steps_out.append(
            {
                "id": "prep_device_session",
                "prep_flow_id": "device_session",
                "title": device_label or "确认应用登录态",
                "kind": "hook",
                "hook_cap": "confirm_login_state",
                "optional": False,
                "required_session": device_want,
            }
        )

    used_caps = {
        str(step.get("hook_cap") or "")
        for step in steps_out
        if isinstance(step, dict) and str(step.get("hook_cap") or "").strip()
    }
    for extra in _extra_prep_catalog_steps(precondition, used_caps):
        steps_out.append(extra)

    da = claim.get("device_app") if isinstance(claim.get("device_app"), dict) else {}
    for step in steps_out:
        if isinstance(step, dict) and not step.get("ui_status"):
            step["ui_status"] = "pending"
    return {
        "schema": "mino.prep_program_plan.v1",
        "steps": steps_out,
        "spans": [],
        "flags": {
            "need_lease": need_lease,
            "need_clear_cache": need_clear,
            "need_device_session": bool(device_want),
            "device_session": device_want,
            "launch_cap": launch_cap,
        },
        "claim_digest": {
            "device_app_session": str(da.get("required_session") or ""),
            "platform": str(claim.get("platform") or ""),
        },
    }


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
        rows.append(row)
    return rows


def sync_prep_internal_milestones(cursor: Any, ctx: Any) -> bool:
    """已满足的 internal / 前置 ctx 标记 → 自动 pass 对应子里程碑。"""
    if not milestone_v1_enabled():
        return False
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
    if not milestone_v1_enabled():
        return None
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
    from mino_nexus.services.login_evidence import EVIDENCE_MAX_AGE_SEC, scope
    from mino_nexus.services.login_evidence_policy import policy_for_context, submitted_account_enabled, submission_current

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
    from mino_nexus.services.login_evidence import EVIDENCE_MAX_AGE_SEC, scope
    from mino_nexus.services.login_evidence_policy import policy_for_context, submitted_account_enabled

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
