"""各 job 运行时槽值组装（Python 生产者）。"""
from __future__ import annotations

import json
from typing import Any

# agent-decide 未租号时的缺省指引：避免模型直接 signal_ask_human 跳过 lease_account。
_EMPTY_ACCOUNTS_JSON: dict[str, Any] = {
    "leased": False,
    "hint": "需要账号时先 lease_account；租号失败再 signal_ask_human",
}

EMPTY_ACCOUNTS_BRIEF = (
    "（未租到账号；需要手机号时先调 lease_account，再 input_text field=phone；"
    "租号失败才 signal_ask_human）"
)


def assemble_agent_decide_slots(
    *,
    goal: str,
    checkpoints_block: str,
    menu: list[dict[str, Any]],
    history_block: str,
    width: int,
    height: int,
    hierarchy_text: str = "",
    target_package: str = "",
    target_app_name: str = "",
    success_criteria: str | dict[str, Any] | None = None,
    memory_block: str = "",
    knowledge_hint: str = "",
    knowledge_body: str = "",
    session_json: dict[str, Any] | None = None,
    nav_assist: str = "",
    doc_context: str = "",
    accounts_json: dict[str, Any] | None = None,
    image_base64: str = "",
    image_mime: str = "image/png",
    phase: str = "do",
    case_scene: dict[str, Any] | None = None,
    # 兼容 v18 及更早 job 模板（v19 起可省略）
    session_block: str = "",
    accounts_brief: str = "",
    device_brief: dict[str, Any] | None = None,
) -> dict[str, str]:
    target_app = (
        f"{target_app_name}（{target_package}）" if target_package else "（未指定，谨慎启动应用）"
    )
    ph = str(phase or "do").strip().lower()
    sess_obj = dict(session_json or {})
    if not sess_obj:
        sess_obj = {"session": "unknown", "note": "session_json missing"}
    if ph == "prep":
        sess_default = "（前置不观察登录态；按 precondition 完成即可 signal_done。）"
    else:
        sess_default = "（尚未观察；首页/信息流看不出登录态属正常，继续按目标操作）"
    acc_obj = dict(accounts_json if accounts_json is not None else _EMPTY_ACCOUNTS_JSON)
    default_sc = {
        "schema": "mino.success_criteria.v1",
        "phase": ph,
        "status": "pending",
        "milestones": [],
    }
    if isinstance(success_criteria, dict):
        sc_obj = dict(success_criteria)
    elif isinstance(success_criteria, str) and success_criteria.strip().startswith("{"):
        try:
            sc_obj = json.loads(success_criteria)
        except json.JSONDecodeError:
            sc_obj = dict(default_sc)
    else:
        sc_obj = dict(default_sc)
    slots = {
        "goal": (goal or "").strip() or "（未提供目标）",
        "success_criteria": json.dumps(sc_obj, ensure_ascii=False, indent=2),
        "target_app": target_app,
        "session_json": json.dumps(sess_obj, ensure_ascii=False, indent=2, default=str),
        "accounts_json": json.dumps(acc_obj, ensure_ascii=False, indent=2, default=str),
        "checkpoints_block": checkpoints_block or "（无）",
        "screen_size_block": f"width={width}, height={height}",
        "history_block": history_block or "（这是第一步）",
        "memory_block": (memory_block or "").strip() or "（暂无）",
        "knowledge_hint": (knowledge_hint or "").strip(),
        "knowledge_body": (knowledge_body or "").strip(),
        "hierarchy_text": (hierarchy_text or "").strip(),
        "nav_assist": (nav_assist or "").strip(),
        "doc_context": (doc_context or "").strip(),
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/png",
    }
    # v18 模板仍引用旧槽时保持可渲染
    sess_json_text = slots["session_json"]
    acc_json_text = slots["accounts_json"]
    slots["session_block"] = (session_block or "").strip() or sess_json_text or sess_default
    slots["accounts_brief"] = (accounts_brief or "").strip() or acc_json_text or EMPTY_ACCOUNTS_BRIEF
    slots["menu_json"] = json.dumps(menu, ensure_ascii=False, default=str)
    if device_brief is not None:
        slots["device_brief_json"] = json.dumps(
            device_brief, ensure_ascii=False, indent=2, default=str
        )
    else:
        slots["device_brief_json"] = "{}"
    return slots


def assemble_vision_plan_slots(
    *,
    success_criteria: str | dict[str, Any] | None = None,
    knowledge_hint: str = "",
    knowledge_body: str = "",
    nav_assist: str = "",
    doc_context: str = "",
    hierarchy_text: str = "",
    review_program_fail: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
    **_: Any,
) -> dict[str, str]:
    """agent-vision-plan：仅 success_criteria + 可选知识/导航/文档/截图。"""
    default_sc = {
        "schema": "mino.success_criteria.v1",
        "status": "pending",
        "milestones": [],
    }
    if isinstance(success_criteria, dict):
        sc_obj = dict(success_criteria)
    elif isinstance(success_criteria, str) and success_criteria.strip().startswith("{"):
        try:
            sc_obj = json.loads(success_criteria)
        except json.JSONDecodeError:
            sc_obj = dict(default_sc)
    else:
        sc_obj = dict(default_sc)
    return {
        "success_criteria": json.dumps(sc_obj, ensure_ascii=False, indent=2),
        "knowledge_hint": (knowledge_hint or "").strip(),
        "knowledge_body": (knowledge_body or "").strip(),
        "nav_assist": (nav_assist or "").strip(),
        "doc_context": (doc_context or "").strip(),
        "hierarchy_text": (hierarchy_text or "").strip(),
        "review_program_fail": (review_program_fail or "").strip(),
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/png",
    }


def assemble_vision_exec_slots(
    *,
    menu: list[dict[str, Any]],
    success_criteria: str | dict[str, Any] | None = None,
    knowledge_hint: str = "",
    knowledge_body: str = "",
    nav_assist: str = "",
    doc_context: str = "",
    hierarchy_text: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
    **_: Any,
) -> dict[str, str]:
    """agent-vision-exec：success_criteria（含每条里程碑 status）+ 截图 + 能力菜单。"""
    default_sc = {
        "schema": "mino.success_criteria.v1",
        "status": "pending",
        "milestones": [],
    }
    if isinstance(success_criteria, dict):
        sc_obj = dict(success_criteria)
    elif isinstance(success_criteria, str) and success_criteria.strip().startswith("{"):
        try:
            sc_obj = json.loads(success_criteria)
        except json.JSONDecodeError:
            sc_obj = dict(default_sc)
    else:
        sc_obj = dict(default_sc)
    return {
        "success_criteria": json.dumps(sc_obj, ensure_ascii=False, indent=2),
        "knowledge_hint": (knowledge_hint or "").strip(),
        "knowledge_body": (knowledge_body or "").strip(),
        "nav_assist": (nav_assist or "").strip(),
        "doc_context": (doc_context or "").strip(),
        "hierarchy_text": (hierarchy_text or "").strip(),
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/png",
        "menu_json": json.dumps(menu, ensure_ascii=False, default=str),
    }


def assemble_assert_vision_slots(
    *,
    expectation: str,
    image_base64: str,
    image_mime: str = "image/jpeg",
    ai_hint: str = "",
    context_block: str = "",
) -> dict[str, str]:
    ctx = (context_block or "").strip()
    hint = (ai_hint or "").strip()
    hint_block = ""
    if hint:
        hint_block = f"==== ai_hint（上游 reasoning，仅供参考）====\n{hint[:600]}\n"
    return {
        "expectation": (expectation or "").strip() or "（未提供预期）",
        "hint_block": hint_block,
        "context_block": f"{ctx}\n\n" if ctx else "",
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/jpeg",
    }


def assemble_inspect_session_slots(
    *,
    required_session: str = "",
    knowledge_hint: str = "",
    accounts_brief: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
) -> dict[str, str]:
    return {
        "required_session": (required_session or "").strip() or "（未写明，记录看到的即可）",
        "knowledge_hint": (knowledge_hint or "").strip() or "（本步未命中知识）",
        "accounts_brief": (accounts_brief or "").strip() or EMPTY_ACCOUNTS_BRIEF,
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/png",
    }


def assemble_json_chat_slots(*, user_payload: str) -> dict[str, str]:
    return {"user_payload": user_payload or "{}"}


def assemble_atlas_morph_slots(
    *,
    context_json: str,
    image_base64: str = "",
    image_mime: str = "image/jpeg",
) -> dict[str, str]:
    return {
        "context_json": (context_json or "").strip() or "{}",
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/jpeg",
    }


def assemble_widget_state_slots(
    *,
    widget: str,
    candidate_states: list[str],
    hint: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
) -> dict[str, str]:
    return {
        "widget": (widget or "").strip(),
        "candidate_states": "、".join(str(s).strip() for s in candidate_states if str(s).strip()),
        "hint": (hint or "").strip(),
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/png",
    }
