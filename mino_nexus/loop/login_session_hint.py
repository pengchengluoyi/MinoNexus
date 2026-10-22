"""把场景要求编译成 prompt 里的登录态提示行。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.resources.session.scene_gate import is_login_module_case, required_session


def compile_login_session_hint(
    scene: Optional[dict[str, Any]] = None,
    session_block: str = "",
) -> str:
    """B3：编译进 checkpoints_block，与 agent-decide 登录态块配合。"""
    if not is_login_module_case(scene=scene):
        return ""
    block = str(session_block or "")
    if "session=logged_in" in block or "next=logout" in block:
        return (
            "【登录态】当前已登录或须退出：先退出至登录页，再执行本步「点登录方式」类操作；"
            "勿在「我的」/个人主页上找登录入口；勿用 close_app 循环代替退出。"
        )
    if required_session(scene=scene) == "guest":
        return (
            "【登录态】本用例测登录流程，起点应为未登录或完整登录页；"
            "若屏幕为个人主页/昵称页，先退出再点登录方式。"
        )
    return ""
