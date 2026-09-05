"""目录 kind：prep / do / check / generic / recovery。与执行阶段、/packs?kind= 同一套词。"""
from __future__ import annotations

from typing import Dict, Tuple

CAPABILITY_KINDS: Tuple[str, ...] = ("prep", "do", "check", "generic")
PACK_KINDS: Tuple[str, ...] = ("recovery",)
ALL_KINDS: Tuple[str, ...] = CAPABILITY_KINDS + PACK_KINDS

# 无 implementations、由 Nexus 本地编排的能力。Scout 不执行。
LOCAL_ORCH_IDS = frozenset({
    "relogin", "lease_account", "get_otp", "get_phone", "release_account",
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
