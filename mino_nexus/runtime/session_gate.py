"""兼容转发：场景判定已下沉到 resources.session.scene_gate（资源域要用）。"""
from mino_nexus.resources.session.scene_gate import *  # noqa: F401,F403,E402
from mino_nexus.resources.session.scene_gate import (  # noqa: F401,E402
    case_scene_reference,
    clamp_case_scene,
    ensure_case_scene,
    is_login_module_case,
    is_login_related_case,
    required_session,
    scene_allows_account_reset,
    session_prep_intent,
)
