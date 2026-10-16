"""Read-only Desktop tools derived from the selected Scout device manifest."""
from __future__ import annotations

from copy import deepcopy

from mino_nexus.catalog import registry
from mino_nexus.catalog.models import Capability, Implementation

_CAPS = {
    "tap_element": ("点击元素", "根据最新桌面截图点击坐标；x、y 为 0–1000 归一化坐标。", {
        "type": "object", "properties": {
            "x": {"type": "integer", "minimum": 0, "maximum": 1000},
            "y": {"type": "integer", "minimum": 0, "maximum": 1000},
        }, "required": ["x", "y"],
    }),
    "input_text": ("输入文本", "向已经点击并确认聚焦的输入框写入文本；不要传坐标。", {
        "type": "object", "properties": {
            "text": {"type": "string"},
            "mode": {"type": "string", "enum": ["replace", "append"]},
            "target_mode": {"type": "string", "enum": ["current_focus"]},
            "field": {"type": "string", "enum": ["email", "phone", "sms_code", "password", "text"]},
        }, "required": ["text"],
    }),
    "press_key": ("按键", "向被测桌面应用发送按键或组合键，如 Enter、Tab、Escape、primary+A。", {
        "type": "object", "properties": {
            "key": {"type": "string"},
            "modifiers": {"type": "array", "items": {"type": "string"}},
        }, "required": ["key"],
    }),
    "launch_app": ("启动应用", "启动并激活本次任务配置的被测 PC 应用；应用标识由任务上下文提供。", {
        "type": "object", "properties": {},
    }),
    "get_foreground_app": ("查看前台应用", "查看当前前台应用，并确认被测 PC 应用是否处于前台。", {
        "type": "object", "properties": {},
    }),
}
CAPABILITY_IDS = frozenset(_CAPS)


def capabilities(reported: list[str], os_name: str, *, platform: str = "desktop") -> list[Capability]:
    os_name = {"mac": "macos", "win": "windows"}.get(os_name, os_name)
    if platform not in {"desktop", "mac", "win"} or (platform == "mac" and os_name != "macos") or (platform == "win" and os_name != "windows"):
        return []
    if os_name not in ("macos", "windows"):
        return []
    available = set(reported or [])
    out = []
    for cap_id, (label, description, schema) in _CAPS.items():
        if cap_id not in available:
            continue
        current = registry.get_capability(cap_id)
        if current is not None and (
            not current.enabled or current.lifecycle == "deprecated" or not current.ui_coverable
        ):
            continue
        parameters = deepcopy(schema)
        if cap_id == "press_key":
            parameters["properties"]["modifiers"]["items"]["enum"] = [
                "ctrl", "alt", "shift", "command" if os_name == "macos" else "win", "primary",
            ]
        out.append(Capability(
            id=cap_id, kind="generic", display_name=label, description=description,
            platforms=[platform], caller="case", phases=["prep", "do"],
            visible_to=list(current.visible_to) if current is not None else ["case", "system"],
            implementations=[Implementation(id="desktop.cua", executor="desktop", display_name="Cua Desktop")],
            parameters=parameters,
        ))
    return out
