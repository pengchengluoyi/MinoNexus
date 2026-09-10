"""各 job 运行时槽值组装（Python 生产者）。"""
from __future__ import annotations

import json
from typing import Any

# agent-decide 未租号时的缺省指引：避免模型直接 signal_ask_human 跳过 lease_account。
EMPTY_ACCOUNTS_BRIEF = (
    "（未租到账号；需要手机号时先调 lease_account，再 input_text field=phone；"
    "租号失败才 signal_ask_human）"
)


def assemble_agent_decide_slots(
    *,
    goal: str,
    checkpoints_block: str,
    device_brief: dict[str, Any],
    menu: list[dict[str, Any]],
    history_block: str,
    width: int,
    height: int,
    hierarchy_text: str = "",
    target_package: str = "",
    target_app_name: str = "",
    success_criteria: str = "",
    memory_block: str = "",
    knowledge_hint: str = "",
    knowledge_body: str = "",
    session_block: str = "",
    accounts_brief: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
    phase: str = "do",
    case_scene: dict[str, Any] | None = None,
) -> dict[str, str]:
    target_app = (
        f"{target_app_name}（{target_package}）" if target_package else "（未指定，谨慎启动应用）"
    )
    slots = {
        "goal": (goal or "").strip() or "（未提供目标）",
        "success_criteria": (success_criteria or "").strip() or "（未提供，凭目标自行判断）",
        "target_app": target_app,
        "session_block": (session_block or "").strip()
        or "（尚未观察；首页/信息流看不出登录态属正常，继续按目标操作）",
        "accounts_brief": (accounts_brief or "").strip() or EMPTY_ACCOUNTS_BRIEF,
        "checkpoints_block": checkpoints_block or "（无）",
        "device_brief_json": json.dumps(device_brief, ensure_ascii=False, indent=2, default=str),
        "screen_size_block": f"width={width}, height={height}",
        "menu_json": json.dumps(menu, ensure_ascii=False, default=str),
        "history_block": history_block or "（这是第一步）",
        "memory_block": (memory_block or "").strip() or "（暂无）",
        "knowledge_hint": (knowledge_hint or "").strip(),
        "knowledge_body": (knowledge_body or "").strip(),
        "hierarchy_text": (hierarchy_text or "").strip(),
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/png",
    }
    return slots


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
