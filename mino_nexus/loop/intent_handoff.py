"""预编译交不出的步骤交给意图识别，再由解释器执行。

预编译只产出它能确定的计划。能力没注册、渠道做不了、编译成跳过、
一条可执行步骤都没有，都在这里选意图并派发，不把整条用例停掉。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.ai.schemas import AgentAction, AgentDecision


def question_of(row: dict[str, Any] | None, fallback: str = "") -> str:
    data = row if isinstance(row, dict) else {}
    for key in ("handoff_question", "source_clause", "source_line", "title", "evidence", "instruction"):
        text = str(data.get(key) or "").strip()
        if text:
            return text[:500]
    return str(fallback or "").strip()[:500]


def handoff_milestone(text: str, *, milestone_id: str, reason: str) -> dict[str, Any]:
    clause = str(text or "").strip()[:500] or "继续执行当前用例步骤"
    return {
        "id": milestone_id,
        "title": clause[:240],
        "kind": "intent_handoff",
        "status": "pending",
        "optional": False,
        "intent_handoff": True,
        "handoff_question": clause,
        "handoff_reason": str(reason or "precompile_gap"),
        "source_clause": clause,
        "source": "intent_handoff",
        "hook_cap": "run_flow_block",
    }


def choose_intent_id(question: str, *, ctx: Any, phase: str = "") -> str:
    """意图识别。模型无答案时用文本匹配，再落到通用导航，保证这一步还能执行。"""
    from mino_nexus.ai.intent_router import choose_intent
    from mino_nexus.knowledge.intent_graph import find_intent, intent_for_text

    app_id = str(getattr(ctx, "app_id", "") or "")
    choice = choose_intent(
        question=question,
        context=str(phase or getattr(ctx, "phase", "") or ""),
        app_id=app_id,
    )
    picked = str(choice.get("choice") or "").strip()
    if picked and picked != "noul":
        return picked
    node = intent_for_text(question, app_id=app_id) or find_intent("general.navigate", app_id=app_id)
    return str((node or {}).get("id") or "general.navigate")


def decision_for_gap(row: dict[str, Any], *, ctx: Any, phase: str = "") -> AgentDecision:
    """选中意图后交给 run_flow_block，由事件解释器展开执行。"""
    question = question_of(row)
    intent_id = choose_intent_id(question, ctx=ctx, phase=phase)
    params = dict(row.get("params") or {})
    params["intent_id"] = intent_id
    row["intent_id"] = intent_id
    row["params"] = params
    row["hook_cap"] = "run_flow_block"
    row["intent_handoff"] = False
    row["handoff_reason"] = str(row.get("handoff_reason") or "")
    return AgentDecision(
        status="continue",
        thought=f"预编译未覆盖「{question[:80]}」，意图识别选择 {intent_id} 并继续执行",
        action=AgentAction(capability_id="run_flow_block", params={"intent_id": intent_id}),
        confidence=0.6,
    )


def mark_gap(row: dict[str, Any], *, reason: str) -> None:
    """把本应跳过的里程碑改成交给意图识别的待执行步骤。"""
    row["status"] = "pending"
    row["optional"] = False
    row["intent_handoff"] = True
    row["handoff_reason"] = reason
    row["handoff_question"] = question_of(row)
    row["hook_cap"] = "run_flow_block"
    row.pop("skip_reason", None)
    row.pop("failure_reason", None)
    row.pop("skipped_by", None)
    row.pop("failed_by", None)
