"""目录 kind：generic / check / recovery / base。

recovery 与 SOP tool_kinds 同名：payload 带 match/actions 时为 L0 规则，
带 implementations（或本地编排 caller）时为可派单原子能力。
"""
from __future__ import annotations

from typing import Dict, Tuple

CAPABILITY_KINDS: Tuple[str, ...] = ("check", "generic")
RECOVERY_KIND = "recovery"
BASE_KIND = "base"
PACK_KINDS: Tuple[str, ...] = (RECOVERY_KIND, BASE_KIND)
ALL_KINDS: Tuple[str, ...] = CAPABILITY_KINDS + PACK_KINDS
# 菜单 / 派单：阶段 tool_kinds 含 recovery 时，recovery 原子能力也进 capabilities。
MENU_ATOMIC_KINDS: Tuple[str, ...] = CAPABILITY_KINDS + (RECOVERY_KIND,)

# 无 implementations、由 Nexus 本地编排的能力。Scout 不执行。
LOCAL_ORCH_IDS = frozenset({
    "relogin", "lease_account", "get_otp", "get_phone", "release_account", "fsm_navigate",
    "accept_legal_consent", "dismiss_ime", "request_sms_code",
})

KIND_META: Dict[str, Dict[str, str]] = {
    "check": {"label": "预期", "desc": "只在校验阶段注入模型"},
    "generic": {"label": "通用", "desc": "前置和操作阶段注入模型"},
    "recovery": {"label": "恢复", "desc": "模型无法继续时，或程序内部处理屏幕不可用"},
    "base": {"label": "基座", "desc": "平台入口与提供面。不进模型菜单"},
}

MUTATE_CAPS = frozenset({
    "tap_element", "multi_tap", "swipe_element_to_element", "swipe_direction",
    "input_text", "press_key", "long_press_element",
    "fsm_navigate",
    "accept_legal_consent", "dismiss_ime", "request_sms_code",
})

# 计入本步已执行（require_do_work）：突变 + 等待/租号/取码。等待不算改界面，但算进展。
PROGRESS_CAPS = MUTATE_CAPS | frozenset({
    "wait_ms",
    "wait_screen_ready",
    "lease_account",
    "get_otp",
})
