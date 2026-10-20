"""Project FSM policy implementation, not a registered/model-callable event."""
from typing import Any
import time
from mino_nexus.core.schemas import PlanEvent, EventResult
from mino_nexus.core.protocol import EventStatus
from mino_nexus.loop.local_executors import (_result, _dispatch_device, _hierarchy_nodes,
    _FSM_DEGRADE_HINT, _FSM_SAME_PAGE_HINT, _skip_tab_fallback,
    _fold_tap_label, _params_tap_label, _tap_params_has_point, _tap_selector_likely_on_screen, _visible_tab_labels)

def plan_and_dispatch_next(
    event: PlanEvent,
    *,
    ctx: Any,
    t0: float,
    router: Any = None,
) -> EventResult:
    """当前屏 → 目标屏：规划最短路并执行第一步 tap（非仅 advise）。"""
    from mino_nexus.services import nav_route
    from mino_nexus.services.nav_state_resolve import plan_route_resolved

    params = dict(event.params or {})
    from_raw = str(
        params.get("from_state")
        or params.get("current_state")
        or params.get("from")
        or ""
    ).strip()
    to_raw = str(
        params.get("to_state")
        or params.get("expected_state")
        or params.get("goal_state")
        or params.get("to")
        or ""
    ).strip()
    localized = getattr(ctx, "nav_localized", None)
    if not isinstance(localized, dict):
        localized = {}
    conf = float(getattr(ctx, "nav_localized_confidence", 0) or 0)
    chosen = str(getattr(ctx, "nav_localized_state", "") or "").strip()
    if not from_raw and chosen and conf >= 0.35:
        from_raw = chosen
    app_id = str(params.get("app_id") or getattr(ctx, "app_id", "") or "").strip()
    project_id = str(
        params.get("project_id")
        or getattr(ctx, "nav_project_id", "")
        or ""
    ).strip()

    def _attempt(**kw: Any) -> dict[str, Any]:
        base = {
            "from": from_raw,
            "to": to_raw,
            "degraded": False,
        }
        base.update(kw)
        return base

    def _degrade(summary: str, error: str, attempt: dict[str, Any], receipt=None) -> EventResult:
        attempt["degraded"] = True
        return _result(
            event,
            status=EventStatus.DECLINED,
            summary=summary,
            error=error,
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={
                **dict(receipt or {}),
                "local_reason": "fsm_degraded",
                "correction_hint": _FSM_DEGRADE_HINT,
                "nav_attempt": attempt,
            },
        )

    if not app_id:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="缺少 app_id，无法查路线图",
            error="missing app_id",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    if not to_raw:
        return _result(
            event,
            status=EventStatus.FAIL,
            summary="需要 to_state/expected_state（目标逻辑页或 state_id）",
            error="missing to_state",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    fsm_doc, _ = nav_route.load_fsm_doc(
        app_id,
        project_id=project_id,
        use_live=False,
        app_version=str(getattr(ctx, "app_version", "") or ""),
    )
    fsm = fsm_doc or {}
    if not fsm:
        return _degrade(
            "没有 NavFSM 配置，无法规划路线",
            "missing fsm",
            _attempt(plan_ok=False, plan_error="missing fsm"),
        )

    from mino_nexus.services.nav_route import coerce_oral_nav_ref

    to_raw = coerce_oral_nav_ref(fsm, to_raw)
    if from_raw:
        from_raw = coerce_oral_nav_ref(fsm, from_raw)

    fork = getattr(ctx, "nav_guest_tab_fork", None)
    if fork is not None:
        from mino_nexus.loop.nav_session_fork import fsm_goal_conflicts_with_guest_fork

        nodes_fork = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
        block, fork_hint = fsm_goal_conflicts_with_guest_fork(
            fork=fork,
            to_raw=to_raw,
            hierarchy_nodes=nodes_fork,
            localized=localized,
        )
        if block:
            return _result(
                event,
                status=EventStatus.DECLINED,
                summary=fork_hint,
                error="guest_tab_login_fork",
                executor="internal",
                elapsed_ms=int((time.time() - t0) * 1000),
                raw_response={
                    "local_reason": "nav_guest_fork",
                    "correction_hint": fork_hint,
                    "nav_attempt": _attempt(plan_ok=False, plan_error="guest_tab_login_fork"),
                },
            )

    scene = getattr(ctx, "case_scene", None) if hasattr(ctx, "case_scene") else {}
    if not isinstance(scene, dict):
        scene = {}
    cur_instr = str(getattr(getattr(ctx, "cursor", None), "instruction", "") or "")
    cur_exp = str(getattr(getattr(ctx, "cursor", None), "expected", "") or "")
    from mino_nexus.loop.nav_session_fork import (
        fsm_blocked_logged_in_session_drift,
        required_session_from_scene,
    )

    req_sess = required_session_from_scene(scene)
    nodes_drift = list(getattr(ctx, "nav_hierarchy_nodes", None) or [])
    drift_block, drift_hint = fsm_blocked_logged_in_session_drift(
        required_session=req_sess,
        instruction=cur_instr,
        expected=cur_exp,
        to_raw=to_raw,
        hierarchy_nodes=nodes_drift,
        localized=localized,
    )
    if drift_block:
        return _result(
            event,
            status=EventStatus.DECLINED,
            summary=drift_hint,
            error="logged_in_session_drift",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={
                "local_reason": "logged_in_session_drift",
                "correction_hint": drift_hint,
                "nav_attempt": _attempt(plan_ok=False, plan_error="logged_in_session_drift"),
            },
        )

    plan_msg = ""
    step_cap = "tap_element"
    step_params: dict[str, Any] = {}
    nav_attempt = _attempt()
    first: dict[str, Any] = {}

    if from_raw:
        plan = plan_route_resolved(
            fsm,
            from_ref=from_raw,
            to_ref=to_raw,
            localized=localized,
        )
        resolve = plan.get("resolve") if isinstance(plan.get("resolve"), dict) else {}
        nav_attempt.update(
            {
                "resolved_from": resolve.get("resolved_from") or "",
                "resolved_to": resolve.get("resolved_to") or "",
                "name_score": {
                    "from": resolve.get("from_name_score"),
                    "to": resolve.get("to_name_score"),
                },
                "screen_score": resolve.get("from_screen_score"),
            }
        )
        if plan.get("ok"):
            hops = int(plan.get("hop_count") or 0)
            summary = str(plan.get("summary") or "")
            nav_attempt["plan_ok"] = True
            if hops == 0:
                click_label = ""  # Reaching the observed target never clicks it again.
                if click_label:
                    plan_msg = (
                        f"图上已在 {plan.get('to_state')}，仍按目标文案点击「{click_label}」"
                    )
                    step_cap = "tap_element"
                    step_params = {"selector_text": click_label, "text": click_label}
                    nav_attempt["plan_error"] = ""
                    nav_attempt["step_pick"] = "same_page_click_label"
                    nav_attempt["planned_hops"] = 0
                    nav_attempt["fallback_tab"] = click_label
                else:
                    plan_msg = f"已在目标屏 {plan.get('to_state')}"
                    nav_attempt["plan_error"] = ""
                    nav_attempt["arrived_at_target"] = True
                    nav_attempt["hops_remaining"] = 0
                    hits = getattr(ctx, "_fsm_same_page_hits", None)
                    if not isinstance(hits, dict):
                        hits = {}
                        if ctx is not None:
                            setattr(ctx, "_fsm_same_page_hits", hits)
                    same_key = (
                        f"{nav_attempt.get('resolved_from') or from_raw}"
                        f"->{nav_attempt.get('resolved_to') or to_raw}"
                    )
                    n = int(hits.get(same_key, 0) or 0) + 1
                    hits[same_key] = n
                    if n > 1:
                        return _degrade(
                            f"{plan_msg}。{_FSM_SAME_PAGE_HINT}",
                            "same_page_repeat",
                            nav_attempt,
                        )
                    return _result(
                        event,
                        status=EventStatus.PASS,
                        summary=f"{plan_msg}。{_FSM_SAME_PAGE_HINT}",
                        executor="internal",
                        elapsed_ms=int((time.time() - t0) * 1000),
                        raw_response={
                            "nav_attempt": nav_attempt,
                            "correction_hint": _FSM_SAME_PAGE_HINT,
                        },
                    )
            else:
                first, pick_meta = nav_route.pick_fsm_first_step(fsm, plan)
                nav_attempt["planned_hops"] = int(pick_meta.get("planned_hops") or hops)
                nav_attempt["step_pick"] = str(pick_meta.get("step_pick") or "")
                step_cap, step_params = nav_route.dispatch_spec_for_edge(fsm, first)
                if step_cap == "press_key" and str(step_params.get("key") or "").upper() == "BACK":
                    if not nav_route.edge_is_system_back(fsm, first):
                        return _degrade(
                            f"{summary}；首 hop 非系统返回边，禁止 BACK（D1-C）。"
                            "导航策略继续规划 或 tap 目标 Tab/入口。",
                            "back_not_on_route",
                            nav_attempt,
                        )
                    nav_attempt["allow_back"] = True
                plan_msg = (
                    f"规划 {hops} 步：{summary}；本步 {first.get('edge_id') or ''} "
                    f"→ {first.get('to') or ''}"
                )
                dest_tab = nav_route.tab_root_label_for_state(
                    fsm, str(nav_attempt.get("resolved_to") or plan.get("to_state") or "")
                )
                if not dest_tab and nav_route.is_oral_home_ref(to_raw):
                    dest_tab = nav_route.oral_home_tab_label(fsm)
                if not dest_tab:
                    want = _fold_tap_label(to_raw)
                    dest_tab = next(
                        (lab for lab in nav_route.tab_slot_labels(fsm) if _fold_tap_label(lab) == want),
                        "",
                    )
                if dest_tab:
                    already_tab = _params_tap_label(step_params) == _fold_tap_label(dest_tab)
                    tabs_visible = not _skip_tab_fallback(localized, ctx)
                    if tabs_visible and not already_tab:
                        step_cap = "tap_element"
                        step_params = {"selector_text": dest_tab, "text": dest_tab}
                        nav_attempt["step_pick"] = "tab_bar_visible_direct"
                        plan_msg = f"{plan_msg}；底栏可见，本步直点 Tab「{dest_tab}」"
                    elif (not tabs_visible) and step_cap == "tap_element" and not already_tab:
                        if first and nav_route.edge_is_system_back(fsm, first):
                            step_cap = "press_key"
                            step_params = {"key": "BACK"}
                            nav_attempt["step_pick"] = "tab_target_system_back"
                            plan_msg = (
                                f"{plan_msg}；路线图首 hop 为系统返回，"
                                "本步 press_key BACK 后再 fsm_navigate"
                            )
                        else:
                            nav_attempt["allow_back"] = False
                            return _degrade(
                                f"{plan_msg}；目标 Tab「{dest_tab}」但无底栏且首 hop 非返回边，"
                                "请 tap_element 沿 onboarding 主流程出墙或探索补边（D1-C·开环）。",
                                "tab_not_visible_no_back_edge",
                                nav_attempt,
                            )
        else:
            err = str(plan.get("error") or "无路径")
            nav_attempt["plan_ok"] = False
            nav_attempt["plan_error"] = err
            if _skip_tab_fallback(localized, ctx):
                two_stage = nav_route.tab_root_entry_hint(
                    fsm, resolve.get("resolved_to") or to_raw
                )
                hint = f"{err}；{two_stage or '无 nav 路线（L1 降级），请 tap 探索或补 Atlas 边。'}"
                nav_attempt["degrade_level"] = 1
                return _degrade(hint, err, nav_attempt)
            else:
                step_params = nav_route.direct_tab_tap_params(
                    fsm, resolve.get("resolved_to") or to_raw
                )
                step_cap = "tap_element"
                if step_params:
                    plan_msg = (
                        f"{err}；尝试直接点击 Tab「{step_params.get('selector_text') or ''}」"
                    )
                    nav_attempt["fallback_tab"] = step_params.get("selector_text") or ""
                else:
                    return _degrade(err, err, nav_attempt)
    else:
        plan_msg = f"未提供当前屏，直接尝试点击目标 Tab「{to_raw}」"
        step_params = nav_route.direct_tab_tap_params(fsm, to_raw)
        step_cap = "tap_element"
        nav_attempt["plan_ok"] = False
        nav_attempt["plan_error"] = "missing from_state"
        if not step_params:
            return _degrade(
                "需要 from_state/current_state，或提供可识别的目标 Tab 文案",
                "missing from_state",
                nav_attempt,
            )

    if not step_params:
        msg = f"{plan_msg or '无路径'}（边未配置 target_page，无法执行）"
        nav_attempt["plan_ok"] = False
        nav_attempt.setdefault("plan_error", "missing tap params")
        return _degrade(msg, "missing tap params", nav_attempt)

    if step_cap == "tap_element":
        nodes = getattr(ctx, "nav_hierarchy_nodes", None)
        if isinstance(nodes, list) and nodes:
            from mino_nexus.loop.tap_enrich import enrich_tap_params

            step_params = enrich_tap_params(
                step_params,
                nodes,
                hint=str(step_params.get("selector_text") or ""),
            )
            from mino_nexus.ai.coords import lift_selector_target

            lift_selector_target(step_params)

    if step_cap == "tap_element" and isinstance(nodes, list) and nodes:
        if not _tap_params_has_point(step_params) and not _tap_selector_likely_on_screen(
            step_params, nodes
        ):
            sel = str(step_params.get("selector_text") or step_params.get("text") or "").strip()
            tabs = _visible_tab_labels(ctx)
            hint_tabs = f"可见底栏：{' / '.join(tabs[:6])}" if tabs else "当前屏无底栏 Tab 匹配"
            return _degrade(
                f"目标「{sel or to_raw}」不在当前层级，跳过盲目 tap。{hint_tabs}。"
                f"{_FSM_DEGRADE_HINT}",
                "tap_target_not_on_screen",
                nav_attempt,
            )

    if router is None:
        return _result(
            event,
            status=EventStatus.PASS,
            summary=f"{plan_msg}；未连接设备，仅返回规划",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={"nav_attempt": nav_attempt},
        )

    from mino_nexus.loop.web.web_env import agent_step_idx

    exec_label = step_params.get("selector_text") or step_params.get("key") or step_cap
    exec_event = PlanEvent(
        seq=event.seq,
        capability_id=step_cap,
        event_kind=step_cap,
        params=step_params,
        ai_reasoning=str(event.ai_reasoning or plan_msg or "fsm_navigate"),
        label=f"FSM→{exec_label}",
    )
    scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
    case_seq = int(getattr(ctx, "case_seq", 0) or 0)
    nav_attempt["exec_cap"] = step_cap
    exec_result = router.dispatch(
        exec_event,
        run_id=scout_run_id,
        step_idx=agent_step_idx(case_seq, event.seq), ctx=ctx,
    )
    exec_st = exec_result.status.value if hasattr(exec_result.status, "value") else str(exec_result.status)
    exec_summary = str(exec_result.summary or exec_result.error or "")
    nav_attempt["tap_status"] = exec_st
    if step_cap == "tap_element":
        nav_attempt["fallback_tab"] = step_params.get("selector_text") or ""
    if exec_st in ("pass", "done"):
        verb = "已按键" if step_cap == "press_key" else f"已点击「{step_params.get('selector_text') or ''}」"
        resolved_to = str(nav_attempt.get("resolved_to") or "").strip()
        first_to = ""
        if from_raw and nav_attempt.get("plan_ok"):
            first_to = str((first or {}).get("to") or "").strip()
        planned_hops = int(nav_attempt.get("planned_hops") or 0)
        arrived = bool(resolved_to and first_to and first_to == resolved_to)
        nav_attempt["arrived_at_target"] = arrived
        nav_attempt["hops_remaining"] = 0 if arrived else max(0, planned_hops - 1)
        raw: dict[str, Any] = {**dict(exec_result.raw_response or {}), "nav_attempt": nav_attempt}
        summary = f"{plan_msg}；{verb}"
        recover_hint = str(nav_attempt.get("recover_hint") or "")
        if recover_hint:
            raw["correction_hint"] = recover_hint
        elif not arrived and planned_hops > 1:
            progress = (
                f"【导航进行中】本步为路线图第 1/{planned_hops} 步，未到目标「{to_raw}」；"
                f"导航策略继续规划（当前为 Tab 根时可直点目标 Tab）。"
            )
            summary = f"{summary}（{progress}）"
            raw["correction_hint"] = progress
        return _result(
            event,
            status=EventStatus.PASS,
            summary=summary,
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response=raw,
        )
    fail_verb = "按键失败" if step_cap == "press_key" else "点击失败"
    return _degrade(
        f"{plan_msg}；{fail_verb}：{exec_summary}",
        exec_summary or "exec failed",
        nav_attempt, exec_result.raw_response,
    )




def dispatch_pending(proxy, ctx, cursor, *, turn_seq, writer=None):
    from mino_nexus.loop.milestones import read_state, write_state, first_pending_hook_milestone
    state = read_state(cursor)
    pending = first_pending_hook_milestone(state)
    if not pending or pending.get('tier') != 'policy' or not pending.get('nav_target'):
        return None
    attempts = int(pending.get('policy_attempts') or 0)
    if attempts >= 12:
        return {'ok': False, 'case_fail': True, 'status': 'blocked', 'error_kind': 'navigation_policy_exhausted',
                'summary': '项目导航未在有限步数内到达目标', 'milestone_id': pending['id']}
    pending['status'] = 'in_progress'
    pending['policy_attempts'] = attempts + 1
    write_state(cursor, state)
    result = plan_and_dispatch_next(PlanEvent(seq=turn_seq, capability_id='tap_element',
        params={'to_state': pending['nav_target']}, label='项目导航策略'), ctx=ctx, router=proxy, t0=time.time())
    attempt = (result.raw_response or {}).get('nav_attempt') or {}
    missing_fsm = (
        str(getattr(result, "error", "") or "") == "missing fsm"
        or str(attempt.get("plan_error") or "") == "missing fsm"
    )
    if missing_fsm:
        if writer:
            writer.append('policy/navigation', {'milestone_id': pending['id'], 'attempt': attempt,
                'status': 'blocked', 'reason_code': 'navigation_configuration_required'})
        return {'ok': False, 'case_fail': True, 'status': 'blocked',
                'error_kind': 'navigation_configuration_required', 'milestone_id': pending['id'],
                'summary': '缺少已发布导航图；已停止导航，不自动切换 DOM 或视觉执行'}
    ok = result.status == EventStatus.PASS
    # A planned edge reaching the destination is not observation of arrival.
    arrived = bool(ok and attempt.get('resolved_to') and
        str(getattr(ctx, 'nav_localized_state', '') or '') == attempt['resolved_to'] and
        float(getattr(ctx, 'nav_localized_confidence', 0) or 0) >= .8 and not attempt.get('exec_cap'))
    if arrived:
        pending['status'] = 'pass'
        pending['evidence'] = 'current_observed_navigation_target'
        write_state(cursor, state)
    if writer:
        writer.append('policy/navigation', {'milestone_id': pending['id'], 'attempt': attempt,
            'status': 'pass' if arrived else 'defer' if ok else 'blocked', 'raw_response': result.raw_response})
    return {'ok': arrived, 'defer': ok and not arrived, 'case_fail': not ok,
            'status': 'pass' if arrived else 'defer' if ok else 'blocked',
            'milestone_id': pending['id'], 'capability_id': attempt.get('exec_cap', ''),
            'summary': result.summary, 'error_kind': '' if ok else 'navigation_policy_failed'}
