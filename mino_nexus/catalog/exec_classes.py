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
# 菜单 / 派单：prep、do、check 都包含这些种类，不再按阶段拆开。
MENU_ATOMIC_KINDS: Tuple[str, ...] = CAPABILITY_KINDS + (RECOVERY_KIND,)

# 不出网的本地执行器。菜单过滤和派发读同一份。
LOCAL_EXECUTOR_IDS = frozenset({
    "lease_account", "get_otp", "get_phone", "release_account",
    "operation.check_box", "dismiss_ime", "confirm_login_state",
    "assert_visual", "persona_subtask", "wait_ms", "wait_screen_ready",
    "signal_nav_calib_step", "run_flow_block",
    # 自由文本证据留在 Nexus。未知能力不加进这份名单。
    "note", "sample",
})
LOCAL_ORCH_IDS = LOCAL_EXECUTOR_IDS

KIND_META: Dict[str, Dict[str, str]] = {
    "check": {"label": "预期", "desc": "任意阶段都可执行"},
    "generic": {"label": "通用", "desc": "任意阶段都可执行"},
    "recovery": {"label": "恢复", "desc": "模型无法继续时，或程序内部处理屏幕不可用"},
    "base": {"label": "基座", "desc": "平台入口与提供面。不进模型菜单"},
}

MUTATE_CAPS = frozenset({
    "tap_element", "multi_tap", "swipe_element_to_element", "swipe_direction",
    "input_text", "press_key", "long_press_element",
    "operation.check_box", "dismiss_ime",
})

# 计入本步已执行（require_do_work）：突变 + 等待/租号/取码/证据记录。
# 等待和自由文本证据不算改界面，但算进展。
PROGRESS_CAPS = MUTATE_CAPS | frozenset({
    "wait_ms",
    "wait_screen_ready",
    "lease_account",
    "get_otp",
    "note",
    "sample",
})
