"""把用例行编译成执行场景（资源键 + 场景）。用例侧逻辑，资源域只提供键。"""
from __future__ import annotations

import copy
from typing import Any

from mino_nexus.resources.case_resource_claim import (
    _case_resource_key,
    _set_case_resource_key,
    compile_resource_key_from_precondition,
    sync_case_resource_metadata,
)


# 协议 / 隐私弹窗的处置意图在**编译期**定死，运行期只按 block id 执行，不再猜。
# 只认「主题词 + 选择词」同时出现，避免普通用例里出现「同意」就误插一步点击。
_CONSENT_TOPIC = ("协议", "隐私", "条款", "授权弹窗", "privacy", "terms", "consent")
_CONSENT_DECLINE = ("拒绝", "不同意", "decline", "reject", "deny", "not now")
_CONSENT_ACCEPT = ("同意", "接受", "允许", "accept", "agree", "allow")


def _consent_block_id(row: dict[str, Any], *, platform: str) -> str:
    """从用例原文推导首启协议弹窗的处置块；判不出就返回空（不插步骤）。"""
    if platform != "android":
        return ""
    text = " ".join(str(row.get(k) or "") for k in
                    ("title", "name", "precondition", "precondition_raw", "steps_text", "expected")).lower()
    if not text.strip() or not any(w in text for w in _CONSENT_TOPIC):
        return ""
    if any(w in text for w in _CONSENT_DECLINE):
        return "fb.global.privacy.decline"
    if any(w in text for w in _CONSENT_ACCEPT):
        return "fb.global.privacy.accept"
    return ""


# 系统权限弹窗（相机/相册/通知…）：用例说了「拒绝/不允许/取消」就走 deny 变体，否则默认允许。
# 同样在编译期定死，运行期只按 block id 执行。
_PERMISSION_TOPIC = ("权限", "permission", "授权", "弹窗", "对话框")
_PERMISSION_DENY = ("拒绝", "不允许", "禁止", "取消授权", "deny", "don't allow", "do not allow")


def _system_dialog_block_id(row: dict[str, Any], *, platform: str) -> str:
    if platform != "android":
        return ""
    text = " ".join(str(row.get(k) or "") for k in
                    ("title", "name", "precondition", "precondition_raw", "steps_text", "expected")).lower()
    if not text.strip() or not any(w in text for w in _PERMISSION_TOPIC):
        return ""
    if any(w in text for w in _PERMISSION_DENY):
        return "fb.global.system_dialog.deny"
    return "fb.global.system_dialog"


def compile_execution_scene(row: dict[str, Any], *, project_id: str) -> dict[str, Any]:
    """Compile scene from current source, never from a previous artifact.

    Resource/session requirements use the current precondition compiler. The
    declared target platform is authoritative, not words appearing in prose.
    """
    from mino_nexus.core.platform_contract import target_platform
    from mino_nexus.runtime.session_gate import clamp_case_scene
    platform = target_platform(row.get("platform"))
    source = copy.deepcopy(row)
    source.pop("case_scene", None)
    source.pop("resource_key", None)
    source.pop("meta", None)
    out = sync_case_resource_metadata(source, project_id=project_id, force_recompile=True)
    key = _case_resource_key(out)
    if not key:
        key = compile_resource_key_from_precondition("", platform=platform)
        _set_case_resource_key(out, key)
    from mino_nexus.casework.case_semantic_integrity import compile_preconditions
    from mino_nexus.resources import project_store
    key = compile_preconditions(str(row.get("precondition") or row.get("precondition_raw") or ""),
                                key, platform=platform, env_doc=project_store.project_env(project_id))
    _set_case_resource_key(out, key)
    scene = copy.deepcopy(key["case_scene"])
    scene["device_need"] = "web" if platform == "web" else "app"
    consent = _consent_block_id(row, platform=platform)
    if consent:
        scene["consent_block_id"] = consent
    else:
        scene.pop("consent_block_id", None)
    dialog = _system_dialog_block_id(row, platform=platform)
    if dialog:
        scene["system_dialog_block_id"] = dialog
    else:
        scene.pop("system_dialog_block_id", None)
    out["case_scene"] = clamp_case_scene(scene)
    return out
