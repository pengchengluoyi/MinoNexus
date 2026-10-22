"""回合日志与事实记录：写 history、写 session 事件。"""
from __future__ import annotations

def _history(rows: list[str]) -> str:
    return "\n".join(rows[-12:]) if rows else "（还没有动作）"


def _history_line(
    seq: int,
    capability_id: str,
    status: str,
    summary: str,
    error: str,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    text = str(summary or error or "")
    sel = str((extra or {}).get("selector_text") or "").strip()
    if capability_id == "tap_element" and sel and ("「?」" in text or sel not in text):
        tail = text
        if tail.startswith("点击 "):
            tail = tail[3:].lstrip()
        return f"{seq}. {capability_id} → {status}: 点击「{sel}」→ {tail}"
    return f"{seq}. {capability_id} → {status}: {text}"


def _record(
    steps: list[dict[str, Any]],
    history: list[str],
    seq: int,
    *,
    capability_id: str,
    status: str,
    summary: str,
    thought: str = "",
    error: str = "",
    executor_used: str = "",
    elapsed_ms: int = 0,
    thumb: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    row = {
        "seq": seq,
        "step": seq,
        "capability_id": capability_id,
        "status": status,
        "summary": summary,
        "error": error,
        "executor_used": executor_used,
        "elapsed_ms": elapsed_ms,
        "thought": thought,
        "thumb": str(thumb or ""),
    }
    hist_extra = dict(extra or {})
    if extra:
        row.update(extra)
    steps.append(row)
    history.append(
        _history_line(
            seq,
            capability_id,
            status,
            summary,
            error,
            hist_extra,
        )
    )
    return row


def _log_turn_end(
    writer: SessionWriter | None,
    *,
    cap: str,
    status: str,
    **extra: Any,
) -> None:
    if writer is None:
        return
    payload: dict[str, Any] = {"decision_cap": cap, "decision_status": status}
    payload.update(extra)
    writer.append("turn/end", payload)


def _llm_trace(decision: AgentDecision) -> dict[str, Any]:
    raw = decision.raw_llm if isinstance(decision.raw_llm, dict) else {}
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    out: dict[str, Any] = {}
    did = str(meta.get("dispatch_id") or "").strip()
    if did:
        out["dispatch_id"] = did
    if raw.get("llm_input") is not None:
        out["llm_input"] = raw.get("llm_input")
    if raw.get("llm_output") is not None:
        out["llm_output"] = raw.get("llm_output")
    if meta:
        out["llm_meta"] = meta
    return out


def _log_context_slots(
    writer: SessionWriter | None,
    *,
    cursor: StepCursor,
    menu: list[dict[str, Any]],
    phase_tool_kinds: list[str] | None,
    inspect_slots: dict[str, str],
    history: list[str],
    steps: list[dict[str, Any]],
    ctx: Any,
) -> None:
    if writer is None:
        return
    from mino_nexus.loop.llm_step_context import (
        build_accounts_json,
        build_session_json,
        history_block_scoped,
        log_context_trace,
        step_scope_key,
    )

    cur_node = cursor.current()
    case_step, phase = step_scope_key(cursor)
    hist_scoped = history_block_scoped(steps, case_step=case_step, phase=phase)
    sess_json = build_session_json(ctx, str(inspect_slots.get("session_block") or ""))
    acc_json = build_accounts_json(ctx)
    from mino_nexus.loop.llm_step_context import success_criteria_for_llm

    sc_obj = success_criteria_for_llm(cursor)
    llm_injection = {
        "goal": cursor.decide_goal(),
        "checkpoints_block": cursor.prompt_block(),
        "success_criteria": sc_obj,
        "session_json": sess_json,
        "accounts_json": acc_json,
        "history_block": hist_scoped,
        "hierarchy_text": inspect_slots.get("hierarchy_text") or "",
        "knowledge_hint": inspect_slots.get("knowledge_hint") or "",
        "knowledge_body": inspect_slots.get("knowledge_body") or "",
        "nav_assist": inspect_slots.get("nav_assist") or "",
        "doc_context": inspect_slots.get("doc_context") or "",
    }
    writer.append(
        "context/slots",
        {
            "slots": {
                "phase": cursor.phase,
                "case_step": case_step,
                **llm_injection,
                "tool_kinds": list(phase_tool_kinds or []),
                "menu_count": len(menu),
                "cap_ids": [str(c.get("id") or "") for c in menu if c.get("id")][:80],
            },
        },
    )
    log_context_trace(
        writer,
        cursor=cursor,
        ctx=ctx,
        menu=menu,
        history=history,
        steps=steps,
        inspect_slots=inspect_slots,
        phase_tool_kinds=phase_tool_kinds,
        llm_injection=llm_injection,
        checkpoints_block_full=cursor.prompt_block_full(),
    )


def _log_program_tool(
    writer: SessionWriter | None,
    *,
    capability_id: str,
    params: dict[str, Any],
    status: str,
    summary: str,
    executor_used: str = "program",
    source: str = "program",
    execution_metadata: dict[str, Any] | None = None,
) -> None:
    if writer is None:
        return
    writer.append(
        "tool/call",
        {"capability_id": capability_id, "params": dict(params or {}), "source": source},
    )
    writer.append(
        "tool/result",
        {
            "capability_id": capability_id,
            "status": status,
            "summary": summary,
            "error": "",
            "executor_used": executor_used,
            "source": source,
            **(execution_metadata or {}),
        },
    )


# 模型空工具调用重试可抵扣的墙钟上限：防止 provider 持续异常时无限续命。
_BUDGET_CREDIT_CAP_SEC = 90.0
