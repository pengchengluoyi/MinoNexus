"""前置程序计划：Claim / prep_flow → 子里程碑 + prep_program_plan（供 agent-vision-plan 只读）。"""
from __future__ import annotations

import json
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
    plat = str(claim.get("platform") or getattr(ctx, "platform", "") or "").strip().lower()
    if ui_channel_from_ctx(ctx) == UiChannel.WEB or plat in ("web", "playwright"):
        return "open_url"
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
                "hook_cap": "relogin",
                "optional": False,
                "required_session": device_want,
            }
        )

    da = claim.get("device_app") if isinstance(claim.get("device_app"), dict) else {}
    return {
        "schema": "mino.prep_program_plan.v1",
        "steps": steps_out,
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
        elif (
            mid == "prep_device_session"
            and cache_passed
            and str(row.get("hook_cap") or "") == "relogin"
            and str(row.get("required_session") or "").strip().lower() in ("guest", "logged_out")
        ):
            # 刚 pm clear，前置就是未登录：不必再看图确认登录态。要已登录时仍走 relogin。
            row["status"] = "pass"
            row["evidence"] = "pm clear"
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
