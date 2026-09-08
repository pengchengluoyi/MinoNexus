"""AI-led 回归 Plan / Replan 的高层入口。

负责：
  1. 把 RunContext → run_brief（注入 prompt）
  2. 把 capability_menu → 精简版（注入 prompt）
  3. 调 llm_client.call_chat_text 跑 chat completion
  4. 解析 LLM JSON → Pydantic 模型（PlanResult / ReplanResult）
  5. 做语义校验：capability_id 必须在菜单里；expected_executor 必须在该 cap 的 implementations 里；
     不合规直接降级为 decline，并把 parse_warnings 记下来

这是 Step 3 的 PUBLIC 入口；上层 orchestrator（Step 4+ 才实现）来调它。
"""
from __future__ import annotations
import re
import time
from typing import Any, Optional
from pydantic import ValidationError
from mino_nexus.core.log import SLog
from mino_nexus.ai.job_slots import (
    assemble_agent_decide_slots,
    assemble_assert_vision_slots,
    assemble_inspect_session_slots,
)
from mino_nexus.ai.prompt_render import JobRenderError, render
from mino_nexus.ai.coords import lift_selector_target, prepare_xy_params_for_execute
from mino_nexus.ai.llm_client import (
    call_chat_text,
    resolve_regression_provider,
)
from mino_nexus.ai.schemas import (
    AssertResult,
    AgentDecision,
    AgentAction,
    BaselineContext,
    CaseGoal,
    CaseCheckpoint,
    CaseSpec,
    HitlComposerResult,
    LocateResult,
    PersonaExpandResult,
    PlanEvent,
    PlanResult,
    ReplanResult,
)
from mino_nexus.runtime.menu import available_menu_brief
from mino_nexus.runtime.run_context import RunContext
TAG = "RegressionPlanner"
def _chat(*, job: str, provider, messages, job_meta: dict | None = None, **kwargs):
    from mino_nexus.ai import dispatch_log as dispatch

    tok = dispatch.bind(role="test-engineer", job=job, skill=job)
    try:
        raw, meta = call_chat_text(provider=provider, messages=messages, **kwargs)
        if job_meta:
            meta = {**meta, "job_id": job_meta.get("job_id") or job, "role_id": job_meta.get("role_id") or ""}
        return raw, meta
    finally:
        dispatch.reset(tok)
def _build_menu_index(menu: list[dict[str, Any]]) -> dict[str, set[str]]:
    """{capability_id: {executor_ids ...}}，用于事件级校验。"""
    idx: dict[str, set[str]] = {}
    for cap in menu:
        cap_id = str(cap.get("id") or "")
        if not cap_id:
            continue
        execs: set[str] = set()
        for impl in cap.get("implementations") or []:
            ex = str(impl.get("executor") or "")
            if ex:
                execs.add(ex)
        idx[cap_id] = execs
    return idx
def _validate_events(
    events: list[PlanEvent],
    menu_index: dict[str, set[str]],
) -> tuple[list[PlanEvent], list[str]]:
    """逐条事件校验 capability_id + expected_executor。返回 (合法事件, warnings)。"""
    out: list[PlanEvent] = []
    warnings: list[str] = []
    for ev in events:
        if ev.capability_id not in menu_index:
            warnings.append(
                f"event seq={ev.seq} capability_id={ev.capability_id!r} 不在菜单里，已丢弃"
            )
            continue
        allowed = menu_index[ev.capability_id]
        if ev.expected_executor and ev.expected_executor not in allowed:
            warnings.append(
                f"event seq={ev.seq} capability={ev.capability_id} "
                f"expected_executor={ev.expected_executor!r} 不在该 cap 的 implementations 里 "
                f"{sorted(allowed)}，尝试用首个可用执行器替换"
            )
            ev = ev.model_copy(update={"expected_executor": next(iter(sorted(allowed)), "")})
        if ev.fallback_executors:
            cleaned = [x for x in ev.fallback_executors if x in allowed and x != ev.expected_executor]
            if cleaned != ev.fallback_executors:
                ev = ev.model_copy(update={"fallback_executors": cleaned})
        out.append(ev)
    return out, warnings
def _parse_plan_result(raw: dict[str, Any], case_id: str, menu_index: dict[str, set[str]]) -> PlanResult:
    """LLM raw JSON → PlanResult；遇到 schema 错降级为 decline。"""
    warnings: list[str] = []
    mode = (raw.get("mode") or "plan").strip().lower()
    if mode not in {"plan", "decline"}:
        warnings.append(f"unknown mode={mode!r}, 强制 decline")
        mode = "decline"

    events: list[PlanEvent] = []
    raw_events = raw.get("events") or []
    if mode == "plan":
        if not isinstance(raw_events, list) or not raw_events:
            warnings.append("mode=plan 但 events 为空，强制 decline")
            mode = "decline"
        else:
            for idx, ev_raw in enumerate(raw_events, start=1):
                if not isinstance(ev_raw, dict):
                    warnings.append(f"events[{idx-1}] 非 dict，丢弃")
                    continue
                ev_raw.setdefault("seq", idx)
                ev_raw.setdefault("ai_reasoning", "（模型未给出 reasoning）")
                ev_raw.setdefault("capability_id", "")
                try:
                    events.append(PlanEvent.model_validate(ev_raw))
                except ValidationError as ve:
                    warnings.append(f"events[{idx-1}] schema 不合法: {ve.errors()[:1]}")
            events, more_warn = _validate_events(events, menu_index)
            warnings.extend(more_warn)
            if not events:
                warnings.append("events 全部校验失败，强制 decline")
                mode = "decline"

    if mode == "decline" and not raw.get("decline_reason"):
        raw["decline_reason"] = "模型未指明原因" if not warnings else "; ".join(warnings[:3])

    result = PlanResult(
        mode=mode,  # type: ignore[arg-type]
        case_id=raw.get("case_id") or case_id,
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
        confidence=float(raw.get("confidence") or 0.0),
        events=events,
        decline_reason=str(raw.get("decline_reason") or ""),
        open_questions=[str(x) for x in (raw.get("open_questions") or []) if str(x).strip()],
        raw_llm=raw,
        parse_warnings=warnings,
    )
    return result
def _parse_replan_result(raw: dict[str, Any], menu_index: dict[str, set[str]]) -> ReplanResult:
    warnings: list[str] = []
    mode = (raw.get("mode") or "replan").strip().lower()
    if mode not in {"replan", "decline", "give_up"}:
        warnings.append(f"unknown mode={mode!r}, 强制 decline")
        mode = "decline"

    events: list[PlanEvent] = []
    raw_events = raw.get("events") or []
    if mode == "replan":
        if not isinstance(raw_events, list) or not raw_events:
            warnings.append("mode=replan 但 events 为空，强制 decline")
            mode = "decline"
        else:
            for idx, ev_raw in enumerate(raw_events, start=1):
                if not isinstance(ev_raw, dict):
                    warnings.append(f"events[{idx-1}] 非 dict，丢弃")
                    continue
                ev_raw.setdefault("seq", idx)
                ev_raw.setdefault("ai_reasoning", "（模型未给出 reasoning）")
                ev_raw.setdefault("capability_id", "")
                try:
                    events.append(PlanEvent.model_validate(ev_raw))
                except ValidationError as ve:
                    warnings.append(f"events[{idx-1}] schema 不合法: {ve.errors()[:1]}")
            events, more_warn = _validate_events(events, menu_index)
            warnings.extend(more_warn)
            if not events:
                warnings.append("events 全部校验失败，强制 decline")
                mode = "decline"

    if mode in {"decline", "give_up"} and not raw.get("decline_reason"):
        raw["decline_reason"] = "模型未指明原因" if not warnings else "; ".join(warnings[:3])

    return ReplanResult(
        mode=mode,  # type: ignore[arg-type]
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
        events=events,
        drop_remaining=bool(raw.get("drop_remaining", True)),
        decline_reason=str(raw.get("decline_reason") or ""),
        needs_human=bool(raw.get("needs_human", False)),
        raw_llm=raw,
        parse_warnings=warnings,
    )
def _parse_assert_result(raw: dict[str, Any]) -> AssertResult:
    warnings: list[str] = []
    passed = bool(raw.get("passed"))
    confidence = float(raw.get("confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    evidence = str(raw.get("evidence") or "").strip()
    if not evidence:
        warnings.append("evidence 为空，模型未遵守约束")
    return AssertResult(
        passed=passed,
        confidence=confidence,
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
        evidence=evidence,
        raw_llm=raw,
        parse_warnings=warnings,
    )
def assert_visual(
    *,
    expectation: str,
    image_base64: str,
    image_mime: str = "image/jpeg",
    ai_hint: str = "",
    context_block: str = "",
    provider_id: Optional[str] = None,
    timeout_sec: int = 60,
) -> AssertResult:
    """ASSERT_VISION：判断当前截图是否满足预期。"""
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return AssertResult(
            passed=False, confidence=0.0,
            ai_reasoning=f"未启用 AI 视觉：{gate.get('reason')}",
            evidence="",
            parse_warnings=["provider unavailable"],
        )
    slots, flags = assemble_assert_vision_slots(
        expectation=expectation,
        image_base64=image_base64,
        image_mime=image_mime,
        ai_hint=ai_hint,
        context_block=context_block,
    )
    try:
        messages, job_meta = render("assert-vision", slots, flags)
    except JobRenderError as e:
        return AssertResult(
            passed=False, confidence=0.0,
            ai_reasoning=str(e),
            evidence="",
            parse_warnings=["job render failed"],
        )
    call = job_meta.get("call") or {}
    raw, meta = _chat(
        job="assert-vision",
        provider=provider,
        messages=messages,
        job_meta=job_meta,
        temperature=float(call.get("temperature", 0.0)),
        max_tokens=int(call.get("max_tokens", 512)),
        timeout_sec=int(call.get("timeout_sec", timeout_sec)),
        json_mode=bool(call.get("json_mode", True)),
    )
    if raw is None:
        return AssertResult(
            passed=False, confidence=0.0,
            ai_reasoning="LLM 返回空 / JSON 解析失败",
            evidence="",
            parse_warnings=[str(meta.get("error") or meta.get("content_preview") or "")[:160]],
            raw_llm={"meta": meta},
        )
    return _parse_assert_result(raw)
def verify_step_expected(
    *,
    expectation: str,
    image_base64: str,
    image_mime: str = "image/png",
    provider_id: Optional[str] = None,
    timeout_sec: int = 60,
    context_block: str = "",
) -> AssertResult:
    """系统校验当前步骤预期。agent 菜单没有这条能力；signal_done 之后由循环调用。"""
    extra = (
        "只根据当前截图判定。图上有的东西就是有；禁止假设会被关掉或点掉。"
        "不要因为「可以关掉所以算不出现」而判通过。"
    )
    ctx = "\n".join(x for x in ((context_block or "").strip(), extra) if x)
    return assert_visual(
        expectation=expectation,
        image_base64=image_base64,
        image_mime=image_mime,
        ai_hint="",
        context_block=ctx,
        provider_id=provider_id,
        timeout_sec=timeout_sec,
    )
_HITL_FALLBACK_TITLES = {
    "confirm":          "需要您确认下一步操作",
    "input_text":       "需要您输入信息",
    "choice_single":    "需要您从下列选项中选择",
    "choice_multiple":  "需要您从下列选项中多选",
    "upload_image":     "需要您上传一张参考截图",
    "acknowledge":      "请知悉以下事项",
}
def _parse_persona_sub_events(
    raw_events: list[Any],
    menu_index: dict[str, set[str]],
) -> tuple[list[PlanEvent], list[str]]:
    """对 sub_events 做与 PlanResult 相同的硬校验：capability_id ∈ menu，executor ∈ impls。"""
    out: list[PlanEvent] = []
    warnings: list[str] = []
    if not isinstance(raw_events, list):
        return out, ["sub_events 非数组，全部丢弃"]

    for idx, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            warnings.append(f"sub_events[{idx}] 非对象，跳过")
            continue
        cap = str(raw.get("capability_id") or "")
        if not cap or cap not in menu_index:
            warnings.append(f"sub_events[{idx}] capability_id={cap!r} 不在菜单，丢弃")
            continue
        ex = str(raw.get("expected_executor") or "")
        allowed = menu_index.get(cap, set())
        if ex and ex not in allowed:
            warnings.append(
                f"sub_events[{idx}] expected_executor={ex!r} 不在 cap={cap!r} 的 impls"
                f" {sorted(allowed)} → 清空，让 router 自选"
            )
            ex = ""
        try:
            seq = int(raw.get("seq") or (idx + 1))
        except (TypeError, ValueError):
            seq = idx + 1
        ev = PlanEvent(
            seq=seq,
            case_step_index=raw.get("case_step_index"),
            capability_id=cap,
            event_kind=str(raw.get("event_kind") or cap),
            params=raw.get("params") or {},
            needs_vlm=bool(raw.get("needs_vlm")),
            expected_executor=ex,
            fallback_executors=[
                str(x) for x in (raw.get("fallback_executors") or [])
                if isinstance(x, str) and x in allowed
            ],
            ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
            label=str(raw.get("label") or "")[:120],
        )
        out.append(ev)
    return out, warnings
_PROCESS_KIND_RE = re.compile(r"加载占位|加载中|生成中|切换中|转圈|进度未|占位符|占位图")
def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []
def _checkpoint_kind(raw_kind: str, description: str) -> str:
    kind = (raw_kind or "").strip().lower()
    if kind in {"process", "transient", "mid"}:
        return "process"
    if kind in {"terminal", "final", "end"}:
        return "terminal"
    return "process" if _PROCESS_KIND_RE.search(description or "") else "terminal"
def _split_process_from_success(
    success: str, cps: list[CaseCheckpoint],
) -> tuple[list[CaseCheckpoint], str]:
    """终态标准里若混入过程态句子，挪到 process 检查点，并给终态补一句。"""
    text = (success or "").strip()
    if not text:
        return cps, text
    parts = [p.strip() for p in re.split(r"[。；;\n]+", text) if p.strip()]
    keep: list[str] = []
    moved: list[str] = []
    for part in parts:
        if _PROCESS_KIND_RE.search(part):
            moved.append(part)
        else:
            keep.append(part)
    extra = list(cps)
    for i, desc in enumerate(moved, 1):
        if any(desc in c.description or c.description in desc for c in extra):
            for c in extra:
                if desc in c.description or c.description in desc:
                    c.kind = "process"
            continue
        extra.append(CaseCheckpoint(id=f"cp_p{i}", description=desc, kind="process"))
    success_out = "。".join(keep) if keep else text
    if moved:
        success_out = success_out.rstrip("。") + "。终态只判定完成后的稳定界面，不要把加载/占位/生成中/切换中当作最终失败理由。"
    return extra, success_out
def _checkpoints_from_expected(case_spec: CaseSpec) -> list[CaseCheckpoint]:
    """检查点 = 有编号的预期原文；缺号步骤不生成检查点。"""
    from mino_nexus.ai.case_text import parse_numbered_items_rules

    rows: list[tuple[int, str]] = []
    seen: set[str] = set()
    for step in case_spec.steps or []:
        t = (step.expected or "").strip()
        n = int(getattr(step, "index", 0) or 0)
        if t and n and t not in seen:
            seen.add(t)
            rows.append((n, t))
    if not rows:
        raw = (case_spec.expected or "").strip()
        for it in parse_numbered_items_rules(raw) if raw else []:
            t = str(it.get("text") or "").strip()
            n = int(it.get("num") or 0) or (len(rows) + 1)
            if t and t not in seen:
                seen.add(t)
                rows.append((n, t))
    return [
        CaseCheckpoint(
            id=f"cp{n}",
            description=desc,
            kind=_checkpoint_kind("", desc),
        )
        for n, desc in rows
    ]
def _parse_agent_decision(raw: dict[str, Any], width: int, height: int) -> AgentDecision:
    warnings: list[str] = []
    status = (raw.get("status") or "continue").strip().lower()
    if status not in {"continue", "done", "give_up", "ask_human"}:
        warnings.append(f"unknown status={status!r} → continue")
        status = "continue"
    action: Optional[AgentAction] = None
    raw_action = raw.get("action")
    if isinstance(raw_action, dict) and raw_action.get("capability_id"):
        params = raw_action.get("params") if isinstance(raw_action.get("params"), dict) else {}
        prepare_xy_params_for_execute(params, width, height)
        lift_selector_target(params)
        action = AgentAction(capability_id=str(raw_action.get("capability_id")), params=params)
    if status in {"continue", "ask_human"} and action is None:
        warnings.append(f"status={status} 但无有效 action")
    subflow = str(raw.get("subflow") or "none").strip().lower() or "none"
    if subflow in {"create", "publish", "create_publish", "generating"}:
        subflow = "create_publish"
    elif subflow not in {"none", "create_publish"}:
        subflow = "none"
    published: dict[str, Any] = {}
    raw_pub = raw.get("published")
    if isinstance(raw_pub, dict):
        published = {k: v for k, v in raw_pub.items() if v not in (None, "", [])}
    elif isinstance(raw_pub, str) and raw_pub.strip() and raw_pub.strip().lower() not in {"null", "none"}:
        published = {"note": raw_pub.strip()}
    return AgentDecision(
        thought=str(raw.get("thought") or "").strip(),
        action=action,
        expected_after=str(raw.get("expected_after") or "").strip(),
        status=status,  # type: ignore[arg-type]
        confidence=max(0.0, min(1.0, float(raw.get("confidence") or 0.0))),
        remember=_as_str_list(raw.get("remember")),
        checkpoint_ids=_as_str_list(raw.get("checkpoint_ids") or raw.get("checkpoints")),
        knowledge_ids=_as_str_list(raw.get("knowledge_ids") or raw.get("knowledge_id")),
        subflow=subflow,
        published=published,
        raw_llm=raw,
        parse_warnings=warnings,
    )
def decide_next_action(
    *,
    goal: str,
    checkpoints_block: str,
    run_context: RunContext,
    history_block: str,
    width: int,
    height: int,
    image_base64: str,
    image_mime: str = "image/png",
    hierarchy_text: str = "",
    success_criteria: str = "",
    memory_block: str = "",
    knowledge_hint: str = "",
    session_block: str = "",
    provider_id: Optional[str] = None,
    timeout_sec: int = 90,
    menu_ids: Optional[set[str]] = None,
    phase: str = "do",
    tool_kinds: Optional[list[str]] = None,
) -> AgentDecision:
    """看图决定下一步一个动作。永远返回 AgentDecision。"""
    menu = available_menu_brief(
        run_context,
        kind="agent",
        phase=phase,
        platform=str(getattr(run_context, "platform", "") or ""),
        tool_kinds=tool_kinds,
    )
    if not menu:
        return AgentDecision(status="give_up", thought="capability_menu 为空（连通性丢失）",
                             parse_warnings=["empty menu"])
    if menu_ids:
        allow = {str(x) for x in menu_ids}
        menu = [c for c in menu if str(c.get("id") or "") in allow]
        if not menu:
            return AgentDecision(status="give_up", thought="备会话菜单为空（连通性丢失）",
                                 parse_warnings=["empty session menu"])
    from mino_nexus.catalog.tool_schema import tools_chat_payload, tools_for_menu

    tool_payload = tools_chat_payload(tools_for_menu(menu))
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return AgentDecision(status="ask_human", thought=f"未启用 AI 视觉：{gate.get('reason')}",
                             parse_warnings=["provider unavailable"])
    accounts_brief = str(getattr(run_context, "accounts_brief", "") or "").strip()
    slots, flags = assemble_agent_decide_slots(
        goal=goal,
        checkpoints_block=checkpoints_block,
        device_brief=run_context.to_prompt_brief(),
        menu=menu,
        history_block=history_block,
        width=width,
        height=height,
        image_base64=image_base64,
        image_mime=image_mime,
        hierarchy_text=hierarchy_text,
        target_package=str(getattr(run_context, "target_package", "") or ""),
        target_app_name=str(getattr(run_context, "target_app_name", "") or ""),
        success_criteria=success_criteria,
        memory_block=memory_block,
        knowledge_hint=knowledge_hint,
        session_block=session_block,
        accounts_brief=accounts_brief,
    )
    try:
        messages, job_meta = render("agent-decide", slots, flags)
    except JobRenderError as e:
        return AgentDecision(
            status="ask_human",
            thought=str(e),
            parse_warnings=["job render failed"],
        )
    call = job_meta.get("call") or {}

    # 给前端展示：我们喂给模型的“文本块/上下文”（不直接回传超大 image_base64）。
    llm_input_debug = {
        "goal": goal,
        "checkpoints_block": checkpoints_block,
        "history_block": history_block,
        "knowledge_hint": knowledge_hint,
        "session_block": session_block,
        "accounts_brief": accounts_brief,
        "memory_block": memory_block,
        "success_criteria": success_criteria,
        "device_brief": run_context.to_prompt_brief(),
        "target_package": str(getattr(run_context, "target_package", "") or ""),
        "capability_menu": menu,
        "tools": list(tool_payload.get("tools") or []),
        "tool_choice": tool_payload.get("tool_choice") or "",
        "image": {"width": width, "height": height, "mime": image_mime, "note": "image_base64 omitted"},
    }
    raw, meta = _chat(
        job="agent-decide",
        provider=provider, messages=messages,
        job_meta=job_meta,
        temperature=float(call.get("temperature", 0.1)),
        max_tokens=int(call.get("max_tokens", 2048)),
        timeout_sec=int(call.get("timeout_sec", timeout_sec)),
        json_mode=bool(call.get("json_mode", False)),
        extra_payload=tool_payload or None,
        allow_tools_downgrade=False,
        require_tool_calls=bool(call.get("require_tool_calls", True)),
    )
    if raw is None:
        err = str(meta.get("error") or "").strip() or "LLM 返回空/解析失败"
        SLog.w(TAG, f"decide_next_action LLM failed err={err!r}")
        return AgentDecision(
            status="give_up",
            thought=err[:200],
            raw_llm={"llm_input": llm_input_debug, "llm_output": None, "meta": meta},
            parse_warnings=["llm failed"],
        )

    decision = _parse_agent_decision(raw, width, height)
    allow_ids = {str(c.get("id") or "") for c in menu}
    allow_ids.add("human_input_text")
    if decision.action and str(decision.action.capability_id or "") not in allow_ids:
        cid = decision.action.capability_id
        decision.parse_warnings.append(f"capability_id={cid!r} 不在菜单，已丢弃")
        decision.action = None
    # 覆盖 raw_llm：既保留输出，也带上“喂给模型的输入”用于 UI 可视化溯源。
    decision.raw_llm = {"llm_input": llm_input_debug, "llm_output": raw, "meta": meta}
    return decision
def _parse_inspect_session_raw(raw: dict[str, Any]) -> dict[str, Any]:
    session = str(raw.get("session") or "unknown").strip().lower()
    if session not in {"logged_out", "logged_in", "unknown"}:
        session = "unknown"
    identity = str(raw.get("identity") or "unknown").strip().lower()
    if identity not in {"match", "mismatch", "unknown"}:
        identity = "unknown"
    nxt = str(raw.get("next") or "keep").strip().lower()
    if nxt not in {"keep", "logout", "login", "switch", "human"}:
        nxt = "keep"
    probe = raw.get("probe")
    if isinstance(probe, str):
        probe = probe.strip().lower() in {"true", "1", "yes"}
    seen = str(raw.get("seen") or "").strip()[:240]
    reason = str(raw.get("reason") or "").strip()[:240] or "（未说明）"
    return {
        "session": session,
        "identity": identity,
        "seen": seen,
        "probe": bool(probe),
        "next": nxt,
        "reason": reason,
        "ok": True,
    }
def inspect_session(
    *,
    required_session: str = "",
    knowledge_hint: str = "",
    accounts_brief: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
    provider_id: Optional[str] = None,
    timeout_sec: int = 60,
) -> dict[str, Any]:
    """观察当前屏登录态。空输出/解析失败重试；耗尽后 ok=False，不当成功。"""
    empty = {
        "session": "unknown",
        "identity": "unknown",
        "seen": "",
        "probe": False,
        "next": "keep",
        "reason": "未观察",
        "ok": False,
    }
    if not image_base64:
        empty["reason"] = "无截图，跳过会话观察"
        return empty
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        empty["reason"] = f"未启用 AI：{gate.get('reason')}"
        return empty
    slots, flags = assemble_inspect_session_slots(
        required_session=required_session,
        knowledge_hint=knowledge_hint,
        accounts_brief=accounts_brief,
        image_base64=image_base64,
        image_mime=image_mime,
    )
    try:
        messages, job_meta = render("inspect-session", slots, flags)
    except JobRenderError as e:
        empty["reason"] = str(e)
        return empty
    call = job_meta.get("call") or {}
    last_err = "LLM 返回空/解析失败"
    attempts = 3
    for attempt in range(1, attempts + 1):
        raw, meta = _chat(
            job="inspect-session",
            provider=provider, messages=messages,
            job_meta=job_meta,
            temperature=float(call.get("temperature", 0.1)),
            max_tokens=int(call.get("max_tokens", 512)),
            timeout_sec=int(call.get("timeout_sec", timeout_sec)),
            json_mode=bool(call.get("json_mode", True)),
        )
        if isinstance(raw, dict) and str(raw.get("session") or "").strip():
            row = _parse_inspect_session_raw(raw)
            if attempt > 1:
                SLog.i(TAG, f"inspect_session recovered on attempt {attempt}")
            return row
        last_err = str((meta or {}).get("error") or "").strip() or "LLM 返回空/解析失败"
        SLog.w(TAG, f"inspect_session attempt {attempt}/{attempts} failed err={last_err!r}")
        if attempt < attempts:
            time.sleep(0.4)
    empty["reason"] = last_err
    return empty
def _parse_inspect_env_raw(raw: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.runtime.env_names import canon_run_env

    env_raw = str(raw.get("env") or "").strip()
    env = canon_run_env(env_raw) or ("unknown" if env_raw.lower() == "unknown" or not env_raw else env_raw)
    if env not in ("dev", "test", "pre", "prod", "unknown"):
        env = "unknown"
    return {
        "env": env,
        "seen": str(raw.get("seen") or "").strip()[:200],
        "reason": str(raw.get("reason") or "").strip()[:240],
        "ok": True,
    }
