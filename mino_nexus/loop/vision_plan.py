"""P6：看图规划（agent-vision-plan）——只维护 success_criteria.milestones。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.ai.planner import vision_plan_turn
from mino_nexus.ai.schemas import VisionPlanDecision
from mino_nexus.loop.llm_step_context import step_scope_key, success_criteria_for_llm
from mino_nexus.loop.milestones import (
    _normalize_milestone_row,
    read_state,
    write_state,
    _sync_scope,
)
from mino_nexus.loop.vision_flags import planning_phase_active

_PREP_APPEND_MUTATE = frozenset({
    "tap_element",
    "input_text",
    "swipe_element_to_element",
    "swipe_direction",
    "long_press_element",
    "multi_tap",
})


def _program_milestones_open(cursor: Any) -> bool:
    """程序种下的步骤还没走完时，不接受规划另起一条业务步骤。"""
    from mino_nexus.loop.milestones import _TERMINAL, read_state

    ms = read_state(cursor).get("milestones") or []
    program = [
        m
        for m in ms
        if isinstance(m, dict)
        and (
            str(m.get("source") or "") in ("prep_program", "do_program", "program_seed")
            or str(m.get("source_block") or "").strip()
        )
    ]
    if not program:
        return False
    return any(str(m.get("status") or "pending") not in _TERMINAL for m in program)


def _row_cap_ids(raw: dict[str, Any]) -> set[str]:
    caps: set[str] = set()
    for key in ("hook_cap", "device_cap", "cap"):
        val = str((raw or {}).get(key) or "").strip()
        if val:
            caps.add(val)
    listed = (raw or {}).get("exec_caps")
    if isinstance(listed, list):
        caps.update(str(item or "").strip() for item in listed if str(item or "").strip())
    return caps


def _is_tap_row(raw: dict[str, Any]) -> bool:
    return bool(_row_cap_ids(raw) & {"tap_element", "accept_legal_consent"})


def _is_wait_ready_row(raw: dict[str, Any]) -> bool:
    caps = _row_cap_ids(raw)
    return bool(caps) and caps <= {"wait_screen_ready", "wait_ms"} and "wait_screen_ready" in caps


def _wait_already_passed(cursor: Any) -> bool:
    return int(getattr(cursor, "wait_ready_passes", 0) or 0) >= 1


def _do_plan_locks_append(cursor: Any, case: dict[str, Any] | None) -> bool:
    """有 do 程序图时禁止规划追加业务边。"""
    from mino_nexus.loop.do_program_plan import has_do_program_steps

    return has_do_program_steps(cursor, case)


def hold_visual_do_exit_until_effect(cursor: Any, ctx: Any) -> None:
    """看图操作步：预期还没出现在屏上时，一次点击不能把准出带走。"""
    if str(getattr(cursor, "phase", "") or "").strip().lower() != "do":
        return
    cur = cursor.current() if hasattr(cursor, "current") else None
    expected = str(getattr(cur, "expected", "") or "").strip()
    if not expected or int(getattr(cursor, "step_effect_hit_streak", 0) or 0) > 0:
        return
    case = _case_from_ctx(ctx if ctx is not None else getattr(cursor, "run_context", None))
    if _do_plan_locks_append(cursor, case):
        return
    from mino_nexus.loop.milestones import read_state, write_state

    state = read_state(cursor)
    if state.get("exit_allowed") is not True:
        return
    state["exit_allowed"] = False
    write_state(cursor, state)


def retire_repeat_wait_focus(cursor: Any, *, writer: Any = None) -> bool:
    """同一屏已经判过可读之后，丢掉还排着的等待页就绪，避免一张张空卡。"""
    if not _wait_already_passed(cursor):
        return False
    from mino_nexus.loop.milestones import _sync_scope, read_state, write_state
    from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress

    state = _sync_scope(cursor, read_state(cursor))
    ms = [m for m in (state.get("milestones") or []) if isinstance(m, dict)]
    changed = False
    for row in ms:
        st = str(row.get("status") or "pending").strip().lower()
        if st not in ("pending", "in_progress"):
            continue
        if not _is_wait_ready_row(row):
            continue
        row["status"] = "skipped"
        row["skip_reason"] = "wait_already_ready"
        changed = True
    if not changed:
        return False
    state["milestones"] = ms
    write_state(cursor, state)
    ensure_single_in_progress(cursor, writer=writer)
    if writer is not None:
        try:
            writer.append("plan_append/rejected", {"reason": "wait_already_ready", "phase": "prep"})
        except Exception:  # noqa: BLE001
            pass
    return True


def _prep_plan_append_allowed(raw: dict[str, Any]) -> bool:
    """前置追加看 kind 和 hook_cap，不看标题里的固定词。"""
    kind = str((raw or {}).get("kind") or "").strip().lower()
    if kind in ("checkpoint", "internal"):
        return True
    if kind == "hook" and str((raw or {}).get("evaluate") or "").strip():
        return True
    caps = _row_cap_ids(raw)
    if not caps:
        return False
    return not bool(caps & _PREP_APPEND_MUTATE)


def _case_from_ctx(ctx: Any) -> dict[str, Any] | None:
    raw = getattr(ctx, "case", None) if ctx is not None else None
    return raw if isinstance(raw, dict) else None


def filter_plan_milestones_append(
    cursor: Any,
    ctx: Any,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """do 有 program_plan 时禁止 plan 追加业务边；check 仅允许对齐已有 checkpoint id。未注册 hook 丢掉。"""
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if not rows:
        return kept, rejected
    from mino_nexus.loop.case_menu import case_menu_ids
    from mino_nexus.catalog.skill_channel import (
        milestone_caps_registered,
        rewrite_milestone_aliases,
    )

    _, phase = step_scope_key(cursor)
    menu_ids = case_menu_ids(ctx, str(phase or "")) if ctx is not None else set()
    registered: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        raw = rewrite_milestone_aliases(raw)
        if menu_ids and not milestone_caps_registered(raw, menu_ids):
            raw["_reject_reason"] = "unregistered"
            rejected.append(raw)
            continue
        registered.append(raw)
    rows = registered
    if not rows:
        return kept, rejected
    ph = str(phase or "").strip().lower()
    case = _case_from_ctx(ctx)

    if ph == "do":
        if _do_plan_locks_append(cursor, case):
            for raw in rows:
                item = dict(raw)
                item["_reject_reason"] = "program_plan_locked"
                rejected.append(item)
            return kept, rejected

    if ph == "prep":
        from mino_nexus.loop.milestones import read_state

        ms = read_state(cursor).get("milestones") or []
        has_prep_program = any(
            isinstance(m, dict) and str(m.get("source") or "") == "prep_program" for m in ms
        )
        if has_prep_program:
            program_done = not _program_milestones_open(cursor)
            for raw in rows:
                if _is_wait_ready_row(raw) and _wait_already_passed(cursor):
                    item = dict(raw)
                    item["_reject_reason"] = "wait_already_ready"
                    rejected.append(item)
                    continue
                if program_done and _is_tap_row(raw):
                    kept.append(raw)
                    continue
                if program_done:
                    item = dict(raw)
                    item["_reject_reason"] = "program_plan_done"
                    rejected.append(item)
                    continue
                if _prep_plan_append_allowed(raw):
                    kept.append(raw)
                else:
                    item = dict(raw)
                    item["_reject_reason"] = "prep_append_filtered"
                    rejected.append(item)
            return kept, rejected

    if ph == "check":
        from mino_nexus.loop.check_program_plan import resolve_check_program_plan

        plan = resolve_check_program_plan(case, cursor)
        cps = plan.get("checkpoints") if isinstance(plan.get("checkpoints"), list) else []
        if cps:
            allowed = {
                str(cp.get("id") or "").strip()
                for cp in cps
                if isinstance(cp, dict) and str(cp.get("id") or "").strip()
            }
            for raw in rows:
                mid = str((raw or {}).get("id") or "").strip()
                if mid and mid in allowed:
                    kept.append(raw)
                else:
                    rejected.append(raw)
            return kept, rejected

    return list(rows), rejected


def reset_plan_context_for_check(cursor: Any) -> None:
    setattr(cursor, "vision_plan_last", None)
    setattr(cursor, "vision_assert_done", False)


def _append_rows(
    ms: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    source: str,
) -> int:
    n = 0
    for raw in rows:
        row = _normalize_milestone_row(raw, warnings=[])
        if not row:
            continue
        mid = str(row.get("id") or "").strip()
        if mid and mid in by_id:
            continue
        row["source"] = source
        ms.append(row)
        by_id[mid] = row
        n += 1
    return n


def apply_vision_plan_to_cursor(
    cursor: Any,
    plan: VisionPlanDecision,
    *,
    writer: Any = None,
    ctx: Any = None,
) -> None:
    """合并规划结果：更新状态 + 末尾追加；禁止整表替换。"""
    setattr(cursor, "vision_plan_last", plan)

    state = _sync_scope(cursor, read_state(cursor))
    ms = [dict(m) for m in (state.get("milestones") or []) if isinstance(m, dict)]
    by_id = {str(m.get("id") or ""): m for m in ms if str(m.get("id") or "")}

    append_rows = list(plan.milestones_append or [])
    if not append_rows and plan.milestones:
        append_rows = list(plan.milestones)
    run_ctx = ctx if ctx is not None else getattr(cursor, "run_context", None)
    kept, rejected = filter_plan_milestones_append(cursor, run_ctx, append_rows)
    if rejected and writer is not None:
        try:
            _, phase = step_scope_key(cursor)
            writer.append(
                "plan_append/rejected",
                {
                    "phase": phase,
                    "count": len(rejected),
                    "ids": [str((r or {}).get("id") or "")[:64] for r in rejected[:8]],
                    "reason": str((rejected[0] or {}).get("_reject_reason") or "program_plan_locked"),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    insert_before = bool(getattr(plan, "insert_before", False))
    if insert_before and kept:
        idx = next(
            (
                i
                for i, row in enumerate(ms)
                if str(row.get("status") or "").strip().lower() == "in_progress"
            ),
            len(ms),
        )
        if idx < len(ms):
            ms[idx]["status"] = "pending"
        inserted: list[dict[str, Any]] = []
        for raw in kept:
            row = _normalize_milestone_row(raw, warnings=[])
            if not row:
                continue
            mid = str(row.get("id") or "").strip()
            if mid and mid in by_id:
                continue
            row["source"] = "plan_insert"
            row["status"] = "pending"
            inserted.append(row)
            if mid:
                by_id[mid] = row
        if inserted:
            inserted[0]["status"] = "in_progress"
            ms[idx:idx] = inserted
            setattr(cursor, "milestone_hold_exec", True)
    else:
        _append_rows(ms, by_id, kept, source="plan_append")
    from mino_nexus.loop.milestones import _TERMINAL

    still_open = [
        m
        for m in ms
        if isinstance(m, dict)
        and not m.get("optional")
        and str(m.get("status") or "pending") not in _TERMINAL
    ]
    # 逻辑块已经走完、追加又被拒绝时才准出。看图操作步还要继续点，不能因此离开本步。
    _, phase_now = step_scope_key(cursor)
    visual_do = str(phase_now or "") == "do" and not _do_plan_locks_append(
        cursor, _case_from_ctx(run_ctx)
    )
    only_repeat_waits = bool(rejected) and all(
        str((row or {}).get("_reject_reason") or "") == "wait_already_ready" for row in rejected
    )
    failed_open = any(isinstance(m, dict) and str(m.get("status") or "") == "failed" for m in ms)
    if only_repeat_waits and not still_open and str(phase_now or "") == "do" and not failed_open:
        plan.exit_allowed = True
    if (
        rejected
        and not still_open
        and not visual_do
        and not only_repeat_waits
        and not failed_open
    ):
        plan.exit_allowed = True

    for cp in plan.checkpoints_plan or []:
        if not isinstance(cp, dict):
            continue
        cid = str(cp.get("id") or cp.get("checkpoint_id") or "").strip()
        if cid and cid in by_id:
            continue
        _append_rows(
            ms,
            by_id,
            [
                {
                    "id": cid or None,
                    "title": cp.get("title") or cp.get("expect") or cid,
                    "kind": "checkpoint",
                    "status": "pending",
                    "assert": cp,
                }
            ],
            source="plan_checkpoint",
        )

    state["milestones"] = ms
    state["status"] = "in_progress"
    if bool(getattr(plan, "exit_allowed", False)) or bool(
        getattr(plan, "step_requirements_complete", False)
    ):
        state["exit_allowed"] = True
    write_state(cursor, state)

    from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress
    from mino_nexus.loop.step_phase_fsm import apply_flow_block_skip_ops

    apply_flow_block_skip_ops(cursor, plan, writer=writer)
    ensure_single_in_progress(cursor, writer=writer)


def run_vision_plan_turn_if_enabled(
    *,
    cursor: Any,
    ctx: Any,
    writer: Any,
    shot: Any,
    steps: list[dict[str, Any]],
    inspect_slots: dict[str, str],
    provider_id: str = "",
    review_program_fail: str = "",
    send_image: bool = True,
) -> Optional[VisionPlanDecision]:
    if not planning_phase_active():
        return None
    from mino_nexus.loop.step_phase_fsm import (
        apply_failure_verdict_from_plan,
        program_fail_review_text,
    )

    review = str(review_program_fail or "").strip()
    if not review and getattr(cursor, "milestone_review_pending", False):
        review = program_fail_review_text(cursor)
    from mino_nexus.loop.vision_observation import apply_vision_observation_slots

    from mino_nexus.action_space.scheme import execution_scheme

    inspect_slots = dict(inspect_slots)
    if execution_scheme(ctx) == "visual":
        inspect_slots["hierarchy_text"] = ""
        inspect_slots["nav_assist"] = ""
    apply_vision_observation_slots(inspect_slots, cursor, writer=writer)
    case_step, phase = step_scope_key(cursor)
    from mino_nexus.loop.case_menu import case_menu_ids

    criteria = dict(success_criteria_for_llm(cursor))
    recovery = str(getattr(cursor, "correction_hint", "") or "").strip()
    if recovery:
        criteria["recovery"] = recovery
        setattr(cursor, "correction_hint", "")
    allowed = sorted(case_menu_ids(ctx, phase))
    if allowed:
        criteria["allowed_capability_ids"] = allowed
    plan = vision_plan_turn(
        run_context=ctx,
        cursor=cursor,
        width=int(getattr(shot, "width", None) or 1080),
        height=int(getattr(shot, "height", None) or 1920),
        image_base64=(
            str(getattr(shot, "image_base64", "") or "")
            if send_image and callable(getattr(shot, "has_image", None)) and shot.has_image()
            else ""
        ),
        image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
        hierarchy_text=str(inspect_slots.get("hierarchy_text") or ""),
        success_criteria=criteria,
        knowledge_hint=str(inspect_slots.get("knowledge_hint") or ""),
        knowledge_body=str(inspect_slots.get("knowledge_body") or ""),
        nav_assist=str(inspect_slots.get("nav_assist") or ""),
        doc_context=str(inspect_slots.get("doc_context") or ""),
        provider_id=provider_id or None,
        review_program_fail=review,
    )
    if plan.blocked_error:
        if writer is not None:
            writer.append("model/blocked", {k: plan.blocked_error.get(k) for k in
                ("error", "http_status", "provider_error_code", "provider_error_param", "provider_request_id", "fail_kind", "dispatch_id")})
        return plan
    from mino_nexus.loop.obstacle_review import review_expected_overlay
    if review_expected_overlay(cursor, ctx, shot, writer=writer,
            kind=str(plan.screen_obstacle.get("kind") or "").strip().lower()):
        return VisionPlanDecision()
    if handle_screen_obstacle(cursor, ctx, plan, writer=writer, shot=shot if send_image and callable(getattr(shot, "has_image", None)) and shot.has_image() else None):
        return plan
    apply_vision_plan_to_cursor(cursor, plan, writer=writer, ctx=ctx)
    if review:
        apply_failure_verdict_from_plan(cursor, plan, writer=writer)
        setattr(cursor, "milestone_review_pending", False)
    if writer is not None:
        try:
            st = read_state(cursor)
            writer.append(
                "plan/vision",
                {
                    "phase": phase,
                    "case_step": case_step,
                    "thought": (plan.thought or "")[:500],
                    "milestone_count": len(st.get("milestones") or []),
                    "insert_before": bool(getattr(plan, "insert_before", False)),
                    "parse_warnings": list(plan.parse_warnings or []),
                    "review_mode": bool(review_program_fail),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return plan


def _infer_obstacle_kind(ctx: Any, summary: str) -> str:
    """模型报了挡屏但没给 kind 时，用当前结构 + 它自己的描述补类别。

    以前直接判 ``screen_obstacle_invalid`` 终止整条会话（cr-318e39386a3b 的 api-1-233：
    系统弹窗挡屏、hierarchy 拿下 0 个节点，模型只说「有权限弹窗」没说 kind → 直接弃赛）。
    类别只是选哪条恢复块，能判出来就不该让人重跑。
    """
    from mino_nexus.loop.recovery_permission import system_dialog_present

    nodes = getattr(ctx, "nav_hierarchy_nodes", None) or []
    if system_dialog_present(nodes):
        return "permission"
    if _consent_controls_proof(nodes):
        return "consent"
    text = str(summary or "").lower()
    for kind, words in (
        ("consent", ("协议", "隐私", "条款", "privacy", "terms")),
        ("permission", ("权限", "允许", "拒绝", "permission", "deny", "allow")),
        ("loading", ("加载", "请稍候", "loading", "splash")),
        ("overlay", ("弹窗", "弹层", "广告", "升级", "更新", "dialog", "popup", "update")),
    ):
        if any(w in text for w in words):
            return kind
    return "overlay" if text else ""


def handle_execution_obstacle(cursor: Any, ctx: Any, decision: Any, *, writer: Any = None, shot: Any = None) -> VisionPlanDecision:
    """执行观察发现挡屏时复用规划恢复合同；报告不派发设备，也不裁定先前动作。"""
    action = getattr(decision, "action", None)
    params = getattr(action, "params", None)
    params = params if isinstance(params, dict) else {}
    kind = str(params.get("kind") or "").strip().lower()
    summary = str(params.get("summary") or "").strip()
    if kind not in {"consent", "permission", "overlay", "loading"}:
        inferred = _infer_obstacle_kind(ctx, summary)
        if inferred:
            if writer:
                writer.append("exec/obstacle_inferred", {"kind": inferred, "reported_kind": kind,
                              "summary": summary[:200]})
            kind = inferred
    plan = VisionPlanDecision(screen_obstacle={"kind": kind, "summary": summary,
        "dialog_region": params.get("dialog_region"), "dialog_evidence": params.get("dialog_evidence")})
    if kind not in {"consent", "permission", "overlay", "loading"} or not summary:
        # 报错不派设备，但也不终止：给一次带格式的补报机会。
        attempts = int(getattr(cursor, "obstacle_report_reasks", 0) or 0) + 1
        setattr(cursor, "obstacle_report_reasks", attempts)
        if writer:
            writer.append("contract/reask" if attempts <= 2 else "contract/blocked", {
                "capability_id": "report_screen_obstacle",
                "reason_code": "screen_obstacle_invalid",
                "attempt": attempts,
                "required": {"kind": ["consent", "permission", "overlay", "loading"], "summary": "一句话描述挡住了什么"},
            })
        if attempts <= 2:
            setattr(cursor, "correction_hint",
                    "挡屏报告要带 kind（consent | permission | overlay | loading）和 summary 一句话；"
                    "按当前截图重新报告一次，不要重复上一个动作。")
            return VisionPlanDecision()
        plan.blocked_error = {"source": "screen", "fail_kind": "screen_obstacle_invalid",
                              "error": "挡屏报告缺少有效类别或可见证据，未派发设备动作。"}
        return plan
    if writer:
        writer.append("exec/obstacle", {"kind": kind, "summary": summary[:300],
                      "execution_started": False, "business_milestone_completed": False})
    from mino_nexus.loop.obstacle_review import review_expected_overlay
    if review_expected_overlay(cursor, ctx, shot, writer=writer, kind=kind):
        # The actual expected-result proof consumed the pending action; final case checks remain independent.
        return VisionPlanDecision()
    handle_screen_obstacle(cursor, ctx, plan, writer=writer, shot=shot)
    if not plan.blocked_error and plan.screen_obstacle:
        setattr(cursor, "milestone_hold_exec", False)  # 报告轮已经结束，不再额外消耗一个仅规划轮。
        setattr(cursor, "correction_hint", "当前挡屏已进入独立恢复。只执行当前恢复目标，确认挡屏关闭后返回原业务目标。")
    return plan


def _consent_controls_proof(nodes: Any) -> dict[str, Any]:
    """当前结构里的协议弹窗证据：协议类文案 + 同意/不同意两个可点控件。"""
    labels: set[str] = set()
    choices: set[str] = set()
    for node in nodes or []:
        if not isinstance(node, dict) or node.get("visible", True) is False or node.get("cross_origin"):
            continue
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if text:
            labels.add(text)
            if node.get("clickable") and node.get("enabled", True):
                choices.add(text)
    terms = [t for t in labels if any(k in t for k in ("隐私", "用户协议", "服务协议", "Privacy", "Terms"))]
    if not terms or not ({"同意", "不同意"} <= choices or {"同意", "拒绝"} <= choices
                         or {"Agree", "Disagree"} <= choices or {"Accept", "Decline"} <= choices):
        return {}
    return {"source": "hierarchy", "kind": "consent", "term_labels": terms[:4],
            "choice_labels": sorted(choices & {"同意", "不同意", "拒绝", "Agree", "Disagree", "Accept", "Decline"})}


def handle_screen_obstacle(cursor: Any, ctx: Any, plan: VisionPlanDecision, *, writer: Any = None, shot: Any = None) -> bool:
    """挡屏先进入独立恢复，不能把恢复点击算成业务操作，也不能按已完成前置准出。"""
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
    from mino_nexus.loop.fuse.interrupt_stack import push_interrupt, stack_depth
    obstacle = plan.screen_obstacle
    kind = str(obstacle.get("kind") or "").strip().lower()
    state = _sync_scope(cursor, read_state(cursor))
    if kind == "overlay":
        from mino_nexus.action_space.scheme import action_scheme

        # 普通 overlay 的模型分类不覆盖当前结构中明确可见的应用协议事实。
        if action_scheme(ctx) == "dom_preferred" and getattr(ctx, "app_foreground", "") == "yes":
            nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or [])
                     if isinstance(n, dict) and n.get("visible", True) and not n.get("cross_origin")]
            labels = {str(n.get("text") or n.get("content_desc") or "").strip() for n in nodes}
            choices = {str(n.get("text") or n.get("content_desc") or "").strip() for n in nodes
                       if n.get("clickable") and n.get("enabled", True)}
            has_terms = any("隐私" in text or "用户协议" in text or "Privacy" in text or "Terms" in text
                            for text in labels)
            if has_terms and "同意" in choices and "不同意" in choices:
                kind = "consent"
                plan.screen_obstacle["kind"] = kind
                if writer:
                    writer.append("screen/obstacle_kind", {"from": "overlay", "to": kind,
                                  "reason": "application_consent_current_structure"})
    if kind not in {"consent", "permission", "overlay", "loading"}:
        state.pop("screen_obstacle", None)
        write_state(cursor, state)
        return False
    if kind != 'loading':
        from mino_nexus.navigation.overlay_evidence import dialog_evidence, has_back
        import hashlib
        nodes = getattr(ctx, 'nav_hierarchy_nodes', None) or []
        image = str(getattr(shot, 'image_base64', '') or '')
        proof = dialog_evidence(nodes, obstacle, frame_ref=hashlib.sha256(image.encode()).hexdigest() if image else '')
        if not proof and kind in ('consent', 'overlay'):
            # 无截图回合（纯文本决策）拿不到 frame_ref，但当前结构里若同时能看到
            # 「同意 / 不同意」两个可点控件和协议类文案，协议弹窗就是结构事实，
            # 不能因为没有图片证据就退回普通导航 —— 那会让模型报完挡屏后无动作可发。
            proof = _consent_controls_proof(nodes) or {}
        if not proof:
            state.pop('screen_obstacle', None)
            write_state(cursor, state)
            if nodes or image:
                plan.screen_obstacle = {}
                plan.exit_allowed = False
                plan.step_requirements_complete = False
                cursor.correction_hint = '当前没有对话框证据，按普通页面继续定位导航目标，不创建挡屏恢复。'
                if writer:
                    writer.append('screen/obstacle', {'kind': 'none', 'reason_code': 'ordinary_navigation', 'system_overlay': 'no'})
                return False
            plan.blocked_error = {'source': 'screen', 'fail_kind': 'observation_missing',
                                  'error': '没有当前截图或结构观察，无法确认页面状态。'}
            if writer:
                writer.append('screen/obstacle', {'kind': kind, 'system_overlay': 'no',
                    'reason_code': 'overlay_evidence_missing', 'execution_started': 'no'})
            return True
        obstacle['evidence'] = proof
    reason = str(obstacle.get("summary") or "当前画面被弹窗或加载层遮挡")[:300]
    previous_obstacle = state.get("screen_obstacle") or {}
    recovery_ids = list(previous_obstacle.get("recovery_ids") or []) if previous_obstacle.get("kind") == kind else []
    state["screen_obstacle"] = {"kind": kind, "summary": reason, "recovery_ids": recovery_ids}
    state["exit_allowed"] = False
    write_state(cursor, state)
    plan.exit_allowed = False
    plan.step_requirements_complete = False
    focus = in_progress_milestone(state)
    if writer:
        writer.append("screen/obstacle", {"kind": kind, "summary": reason, "focus_id": (focus or {}).get("id", ""), "evidence": obstacle.get("evidence", {})})
    if kind == "consent" and focus and (
        _row_cap_ids(focus) == {"accept_legal_consent"}
        or str(focus.get("id") or "") in recovery_ids
    ):
        return False
    from mino_nexus.loop.interrupt_recovery import (
        builtin_obstacle_steps, bound_recovery_ids, recovery_step_diagnostics, resolve_interrupt_steps,
    )
    # A visible consent/loading obstacle selects its own policy. A stale generic
    # foreground probe must not replace consent with launch_app.
    steps = builtin_obstacle_steps(kind, app_id=str(getattr(ctx, "app_id", "") or ""))
    if not steps and kind == "overlay":
        steps = resolve_interrupt_steps(cursor, ctx, router=getattr(ctx, "router", None),
                                        reason=reason, allow_fallback=False)
    _, phase = step_scope_key(cursor)
    bound = bound_recovery_ids(ctx, phase)
    issues = recovery_step_diagnostics(steps, bound)
    if issues:
        if writer:
            writer.append("screen/recovery_rejected", {"kind": kind, "phase": phase,
                "diagnostics": issues, "bound_capability_ids": sorted(bound), "execution_started": "no"})
        steps = []
    if steps and not stack_depth(cursor):
        if focus:
            inserted = push_interrupt(cursor, ctx, parent_step_id=str(focus["id"]), reason=reason, steps=steps, writer=writer)
        else:
            rows = list(state.get("milestones") or [])
            inserted = bool(_append_rows(rows, {str(row.get("id")): row for row in rows}, steps, source="screen_recovery"))
            state["milestones"] = rows
            if inserted:
                state["status"] = "in_progress"
            write_state(cursor, state)
        if inserted:
            state = read_state(cursor)
            state["screen_obstacle"]["recovery_ids"] = [str(step["id"]) for step in steps]
            write_state(cursor, state)
            if writer:
                writer.append("screen/recovery", {"kind": kind, "summary": reason,
                              "milestone_ids": state["screen_obstacle"]["recovery_ids"]})
            setattr(cursor, "milestone_hold_exec", True)
            return True
    if kind == "consent":
        remedy = "协议自动确认未能完成，请查看报告中的恢复动作及截图，检查当前渠道的点击能力和协议恢复规则。"
    elif kind == "permission":
        remedy = "请确认本次测试需要的权限，或配置相应的权限处理步骤后再执行。"
    else:
        remedy = "当前没有能处理此挡屏的有效恢复规则，请查看报告中的截图并配置对应恢复动作。"
    plan.blocked_error = {"source": "screen", "fail_kind": "screen_obstacle_" + kind, "error": reason.rstrip("。.!！ ") + "。" + remedy}
    return True
