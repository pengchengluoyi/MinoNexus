"""任务下发「一张资源卡」：Claim + 租约 + 登记簿快照。"""
from __future__ import annotations

from typing import Any


def build_resource_card(
    *,
    case: dict[str, Any] | None,
    resource_key: dict[str, Any] | None,
    sn: str,
    package_id: str,
    platform: str = "android",
    project_id: str = "",
    run_id: str = "",
    preflight_gaps: list[str] | None = None,
    account_lease: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rk = resource_key if isinstance(resource_key, dict) else {}
    da = rk.get("device_app") if isinstance(rk.get("device_app"), dict) else {}
    acct = rk.get("account") if isinstance(rk.get("account"), dict) else {}
    from mino_nexus.services.device_app_session_store import get_session
    from mino_nexus.services.device_resource_lease import get_active_lease

    reg = get_session(str(sn or "").strip(), str(package_id or "").strip()) or {}
    dlease = get_active_lease(str(sn or "").strip(), str(package_id or "").strip())
    plat = str(platform or rk.get("platform") or da.get("platform") or "android").strip().lower()
    return {
        "version": 1,
        "run_id": str(run_id or "")[:80],
        "project_id": str(project_id or "")[:48],
        "case_id": str((case or {}).get("case_id") or "")[:64],
        "sn": str(sn or "").strip(),
        "package_id": str(package_id or "").strip(),
        "platform": plat,
        "claim": {
            "required_session": str(da.get("required_session") or "any"),
            "account_requirements": acct.get("requirements") if isinstance(acct.get("requirements"), dict) else {},
            "prep": list(da.get("prep") or [])[:20],
        },
        "registry": {
            "session": str(reg.get("session") or "unknown"),
            "bound_account_id": str(reg.get("bound_account_id") or ""),
            "stale": bool(reg.get("stale")),
        },
        "leases": {
            "account": dict(account_lease or {}),
            "device": dict(dlease or {}),
        },
        "preflight_gaps": list(preflight_gaps or [])[:8],
    }
