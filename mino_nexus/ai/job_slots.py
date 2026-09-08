"""各 job 运行时槽值组装（Python 生产者）。"""
from __future__ import annotations

import json
from typing import Any


def _playwright_brief(device_brief: dict[str, Any]) -> bool:
    flags = (device_brief or {}).get("flags") or {}
    if flags.get("playwright"):
        return True
    channels = (device_brief or {}).get("channels") or {}
    return str(channels.get("playwright") or "") in ("available", "connected")


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
    session_block: str = "",
    accounts_brief: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
) -> tuple[dict[str, str], dict[str, bool]]:
    flags = {"is_web_channel": _playwright_brief(device_brief)}
    target_app = (
        f"{target_app_name}（{target_package}）" if target_package else "（未指定，谨慎启动应用）"
    )
    slots = {
        "goal": (goal or "").strip() or "（未提供目标）",
        "success_criteria": (success_criteria or "").strip() or "（未提供，凭目标自行判断）",
        "target_app": target_app,
        "session_block": (session_block or "").strip()
        or "（尚未观察；首页/信息流看不出登录态属正常，继续按目标操作）",
        "accounts_brief": (accounts_brief or "").strip() or "（未租到账号；登录所需手机号只能问人）",
        "checkpoints_block": checkpoints_block or "（无）",
        "device_brief_json": json.dumps(device_brief, ensure_ascii=False, indent=2, default=str),
        "screen_size_block": f"width={width}, height={height}",
        "menu_json": json.dumps(menu, ensure_ascii=False, default=str),
        "history_block": history_block or "（这是第一步）",
        "memory_block": (memory_block or "").strip() or "（暂无）",
        "knowledge_hint": (knowledge_hint or "").strip(),
        "hierarchy_text": (hierarchy_text or "").strip(),
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/png",
    }
    return slots, flags


def assemble_assert_vision_slots(
    *,
    expectation: str,
    image_base64: str,
    image_mime: str = "image/jpeg",
    ai_hint: str = "",
    context_block: str = "",
) -> tuple[dict[str, str], dict[str, bool]]:
    ctx = (context_block or "").strip()
    hint = (ai_hint or "").strip()
    hint_block = ""
    if hint:
        hint_block = f"==== ai_hint（上游 reasoning，仅供参考）====\n{hint[:600]}\n"
    slots = {
        "expectation": (expectation or "").strip() or "（未提供预期）",
        "hint_block": hint_block,
        "context_block": f"{ctx}\n\n" if ctx else "",
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/jpeg",
    }
    return slots, {}


def assemble_inspect_session_slots(
    *,
    required_session: str = "",
    knowledge_hint: str = "",
    accounts_brief: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
) -> tuple[dict[str, str], dict[str, bool]]:
    slots = {
        "required_session": (required_session or "").strip() or "（未写明，记录看到的即可）",
        "knowledge_hint": (knowledge_hint or "").strip() or "（本步未命中知识）",
        "accounts_brief": (accounts_brief or "").strip() or "（未租到账号）",
        "image_base64": image_base64 or "",
        "image_mime": image_mime or "image/png",
    }
    return slots, {}


def assemble_json_chat_slots(*, user_payload: str) -> tuple[dict[str, str], dict[str, bool]]:
    return {"user_payload": user_payload or "{}"}, {}
