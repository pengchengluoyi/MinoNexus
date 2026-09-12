"""目录 kind：prep / do / check / generic / recovery。

recovery 与 SOP tool_kinds 同名：payload 带 match/actions 时为 L0 规则，
带 implementations（或本地编排 caller）时为可派单原子能力。
"""
from __future__ import annotations

from typing import Dict, Tuple

CAPABILITY_KINDS: Tuple[str, ...] = ("prep", "do", "check", "generic")
RECOVERY_KIND = "recovery"
PACK_KINDS: Tuple[str, ...] = (RECOVERY_KIND,)
ALL_KINDS: Tuple[str, ...] = CAPABILITY_KINDS + PACK_KINDS
# 菜单 / 派单：阶段 tool_kinds 含 recovery 时，recovery 原子能力也进 capabilities。
MENU_ATOMIC_KINDS: Tuple[str, ...] = CAPABILITY_KINDS + (RECOVERY_KIND,)

# 无 implementations、由 Nexus 本地编排的能力。Scout 不执行。
LOCAL_ORCH_IDS = frozenset({
    "relogin", "lease_account", "get_otp", "get_phone", "release_account", "fsm_navigate",
})

KIND_META: Dict[str, Dict[str, str]] = {
    "prep": {"label": "前置操作", "desc": "Agent 开跑前可调：账号、登录、环境"},
    "do": {"label": "操作步骤", "desc": "执行器提供抽象原语；步骤阶段再并上通用能力"},
    "check": {"label": "预期结果", "desc": "做成之后只看、不准再点"},
    "generic": {"label": "通用能力", "desc": "点击、滑动、等待：prep / do 都能调"},
    "recovery": {"label": "恢复", "desc": "系统/设备异常怎么处置"},
}

MUTATE_CAPS = frozenset({
    "tap_element", "multi_tap", "swipe_element_to_element", "swipe_direction",
    "input_text", "press_key", "long_press_element",
})
