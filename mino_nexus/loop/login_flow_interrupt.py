"""非登录步被登录挡屏：终端走 fb.global.login 宏（D4）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.loop.step_contract import instruction_allows_login_flow


def login_overlay_blocks_flow(
    *,
    hierarchy_nodes: list[dict[str, Any]] | None,
    instruction: str,
    login_module_case: bool,
) -> bool:
    """结构判定：主流程步上出现短信/手机号登录控件（不用 App 词表）。"""
    if instruction_allows_login_flow(instruction, login_module_case=login_module_case):
        return False
    nodes = list(hierarchy_nodes or [])
    if not nodes:
        return False
    from mino_nexus.loop.ui_sms_request import (
        find_phone_field,
        find_send_code_button,
        phone_field_filled,
    )

    phone = find_phone_field(nodes)
    if phone is None:
        pass
    else:
        send = find_send_code_button(nodes, phone)
        if send is not None:
            return True
        if phone_field_filled(nodes):
            return True
    from mino_nexus.loop.ui_consent import find_consent_control

    if find_consent_control(nodes) is not None:
        from mino_nexus.loop.ui_consent import any_focused_input

        if any_focused_input(nodes):
            return True
    return False
