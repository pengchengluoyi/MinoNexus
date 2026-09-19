"""Studio 试筛：Claim + 选号 + 机态缺口。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services.case_resource_claim import (
    default_package_for_project,
    preflight_device_app_gap,
    sync_case_resource_metadata,
)
from mino_nexus.services.device_app_session_store import get_session
from mino_nexus.services.project_env import pick_test_accounts, resolve_pick_requirements


def run_resource_trial(
    project_id: str,
    *,
    prompt: str,
    env: str = "test",
    sn: str = "",
    package_id: str = "",
    app_id: str = "",
    env_doc: dict | None = None,
) -> dict[str, Any]:
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.project_env import list_test_accounts

    pid = str(project_id or "").strip()
    if env_doc is None:
        env_doc = ps.project_env(pid)
    pkg = str(package_id or "").strip() or default_package_for_project(pid, app_id=app_id)
    case_stub: dict[str, Any] = {"precondition": str(prompt or "").strip()}
    sync_case_resource_metadata(
        case_stub,
        project_id=pid,
        env_doc=env_doc,
        package=pkg,
        env=str(env or "test").strip() or "test",
    )
    claim = case_stub.get("resource_key") if isinstance(case_stub.get("resource_key"), dict) else {}
    scene = case_stub.get("case_scene") if isinstance(case_stub.get("case_scene"), dict) else {}
    req = resolve_pick_requirements(
        prompt=str(prompt or ""),
        env=str(env or "test"),
        env_doc=env_doc,
        requirements=(claim.get("account") or {}).get("requirements")
        if isinstance(claim.get("account"), dict)
        else None,
    )
    rows = list_test_accounts(env_doc, project_id=pid)
    ranked = pick_test_accounts(
        rows,
        prompt=str(prompt or ""),
        env=str(env or "test"),
        surface="",
        channels=env_doc.get("channels") or [],
        env_doc=env_doc,
        requirements=req,
    )
    device_sn = str(sn or "").strip()
    gaps = preflight_device_app_gap(claim, sn=device_sn, package_id=pkg) if device_sn and pkg else []
    session_row = get_session(device_sn, pkg) if device_sn and pkg else None
    from mino_nexus.services.project_env import public_test_accounts

    return {
        "resource_key": claim,
        "case_scene": scene,
        "requirements": req,
        "accounts": public_test_accounts(ranked, env_doc, include_password=False),
        "package_id": pkg,
        "device_app_gaps": gaps,
        "device_app_session": session_row,
    }
