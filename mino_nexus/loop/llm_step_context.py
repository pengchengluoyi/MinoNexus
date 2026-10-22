"""单步 LLM 注入与排查 trace（P0：瘦身 prompt，全量写入 session_events）。"""
from __future__ import annotations

import json
from typing import Any, Optional

from mino_nexus.resources.session.view import build_accounts_json, build_session_json  # noqa: F401
from mino_nexus.runtime.cursor_state import (  # noqa: F401
    SUCCESS_CRITERIA_SCHEMA,
    empty_success_criteria,
    step_scope_key,
)


def success_criteria_for_llm(cursor: Any) -> dict[str, Any]:
    """注入 LLM 的唯一进度真源（与 cursor.success_criteria_state 一致）。"""
    from mino_nexus.loop.milestones import read_state

    return dict(read_state(cursor))


def build_task_context_json(ctx: Any) -> dict[str, Any]:
    """批次 / 设备渠道上下文（不含账号明细）。"""
    from mino_nexus.contract.execution.ui import ui_channel_from_ctx

    ch = ui_channel_from_ctx(ctx)
    return {
        "run_id": str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "").strip(),
        "task_id": str(getattr(ctx, "task_id", "") or "").strip(),
        "app_id": str(getattr(ctx, "app_id", "") or "").strip(),
        "platform": str(getattr(ctx, "platform", "") or "").strip(),
        "ui_channel": str(ch.value if hasattr(ch, "value") else ch),
        "device_sn": str(getattr(ctx, "sn", "") or "").strip()[:32],
        "env": str(getattr(ctx, "env", "") or "").strip(),
        "target_package": str(getattr(ctx, "target_package", "") or "").strip()[:240],
        "target_app_name": str(getattr(ctx, "target_app_name", "") or "").strip()[:120],
    }


def build_case_execution_context_json(cursor: Any, ctx: Any) -> dict[str, Any]:
    """当前用例执行切片（步序、阶段文案、目标）。"""
    case_step, phase = step_scope_key(cursor)
    case = getattr(ctx, "case", None) if ctx is not None else None
    case_d = case if isinstance(case, dict) else {}
    cur = cursor.current() if cursor is not None and hasattr(cursor, "current") else None
    out: dict[str, Any] = {
        "case_id": str(case_d.get("case_id") or case_d.get("id") or "").strip(),
        "title": str(case_d.get("title") or case_d.get("name") or "").strip()[:240],
        "phase": phase,
        "case_step": case_step,
        "goal": "",
        "precondition": "",
        "instruction": "",
        "expected": "",
    }
    if hasattr(cursor, "decide_goal"):
        try:
            out["goal"] = str(cursor.decide_goal() or "").strip()[:800]
        except Exception:
            pass
    if phase == "prep":
        out["precondition"] = str(
            getattr(cursor, "precondition_text", "")
            or getattr(cursor, "precondition", "")
            or case_d.get("precondition")
            or ""
        ).strip()[:1200]
    elif cur is not None:
        out["instruction"] = str(getattr(cur, "instruction", "") or "").strip()[:1200]
        out["expected"] = str(getattr(cur, "expected", "") or "").strip()[:1200]
    tags = case_d.get("tags")
    if isinstance(tags, list):
        out["tags"] = [str(t).strip() for t in tags if str(t).strip()][:24]
    return out


def success_criteria_json_text(cursor: Any) -> str:
    return json.dumps(success_criteria_for_llm(cursor), ensure_ascii=False, indent=2)


def _row_scope(row: dict[str, Any]) -> tuple[int, str]:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    ph = str(
        extra.get("loop_phase")
        or row.get("loop_phase")
        or ""
    ).strip().lower()
    cs = int(
        extra.get("case_step")
        or row.get("case_step")
        or extra.get("case_step_index")
        or row.get("case_step_index")
        or 0
    )
    return cs, ph


def history_block_scoped(
    steps: list[dict[str, Any]],
    *,
    case_step: int,
    phase: str,
    limit: int = 24,
) -> str:
    ph = str(phase or "do").strip().lower()
    cs = int(case_step or 0)
    lines: list[str] = []
    for row in steps:
        row_cs, row_ph = _row_scope(row)
        if row_ph != ph:
            continue
        if row_cs != cs:
            continue
        seq = int(row.get("seq") or 0)
        cap = str(row.get("capability_id") or "")
        st = str(row.get("status") or "")
        summ = str(row.get("summary") or row.get("error") or "")
        row_extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        sel = str(row.get("selector_text") or row_extra.get("selector_text") or "").strip()
        if cap == "tap_element" and sel:
            lines.append(f"{seq}. {cap} → {st}: 点击「{sel}」→ {summ}")
        else:
            lines.append(f"{seq}. {cap} → {st}: {summ}")
    if not lines:
        return "（本步本阶段尚无操作记录）"
    return "\n".join(lines[-limit:])




def log_context_trace(
    writer: Any,
    *,
    cursor: Any,
    ctx: Any,
    menu: list[dict[str, Any]],
    history: list[str],
    steps: list[dict[str, Any]],
    inspect_slots: dict[str, Any],
    phase_tool_kinds: list[str] | None,
    llm_injection: dict[str, Any],
    checkpoints_block_full: str = "",
) -> None:
    if writer is None:
        return
    case_step, phase = step_scope_key(cursor)
    device_brief = {}
    try:
        device_brief = ctx.to_prompt_brief()
    except Exception:
        device_brief = {}
    writer.append(
        "context/trace",
        {
            "case_step": case_step,
            "phase": phase,
            "history_full": list(history[-200:]),
            "history_step": history_block_scoped(
                steps, case_step=case_step, phase=phase, limit=200
            ),
            "device_brief": device_brief,
            "menu_snapshot": {
                "cap_ids": [str(c.get("id") or "") for c in menu if c.get("id")],
                "tool_kinds": list(phase_tool_kinds or []),
            },
            "inspect_slots": {
                k: (str(v)[:4000] if isinstance(v, str) else v)
                for k, v in (inspect_slots or {}).items()
            },
            "success_criteria": success_criteria_for_llm(cursor),
            "session_json": build_session_json(
                ctx, str(inspect_slots.get("session_block") or "")
            ),
            "accounts_json": build_accounts_json(ctx),
            "llm_injection": llm_injection,
            "checkpoints_block_full": checkpoints_block_full[:12000]
            if checkpoints_block_full
            else "",
        },
    )
