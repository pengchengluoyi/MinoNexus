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
    out["case_scene"] = clamp_case_scene(scene)
    return out
