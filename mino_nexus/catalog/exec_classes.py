"""执行能力四类：前置 / 步骤 / 预期 / 通用。控制台 Tab 由这类元数据下发。"""
from __future__ import annotations

from typing import Dict, Tuple

EXEC_KINDS: Tuple[str, ...] = ("prep", "step", "expect", "generic")

KIND_META: Dict[str, Dict[str, str]] = {
    "prep": {"label": "前置操作", "desc": "开跑前：账号、登录、环境"},
    "step": {"label": "操作步骤", "desc": "按编号走到场景"},
    "expect": {"label": "预期结果", "desc": "做成之后只看、不准再点"},
    "generic": {"label": "通用能力", "desc": "点击、滑动、等待：哪一列都能调"},
}

CAP_CLASS: Dict[str, str] = {
    "wake_screen": "prep",
    "dismiss_keyguard": "prep",
    "clear_app_cache": "prep",
    "kill_app": "prep",
    "install_apk": "prep",
    "read_device_data": "prep",
    "probe_device_state": "prep",
    "get_app_version": "prep",
    "get_foreground_app": "prep",
    "persona_subtask": "prep",
    "assert_visual": "expect",
    "tap_element": "generic",
    "multi_tap": "generic",
    "long_press_element": "generic",
    "swipe_direction": "generic",
    "swipe_element_to_element": "generic",
    "input_text": "generic",
    "press_key": "generic",
    "set_clipboard": "generic",
    "launch_app": "generic",
    "close_app": "generic",
    "wait_ms": "generic",
    "wait_screen_ready": "generic",
    "exec_script": "generic",
    "human_confirm": "generic",
    "human_input_text": "generic",
    "human_choice_single": "generic",
    "human_choice_multiple": "generic",
    "human_acknowledge": "generic",
    "human_upload_image": "generic",
}
