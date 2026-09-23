"""设备 × App 跑批租约。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from mino_nexus.models.resource_ops import DeviceResourceLease

DEFAULT_TTL_SEC = 7200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expiry(ttl_sec: int = DEFAULT_TTL_SEC) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(300, int(ttl_sec)))).isoformat(timespec="seconds")


def _expired(exp: str) -> bool:
    raw = str(exp or "").strip()
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= dt.astimezone(timezone.utc)


def _holder_run_still_live(run_id: str) -> bool:
    rid = str(run_id or "").strip()
    if not rid:
        return False
    try:
        from mino_nexus.services.run_store import get, task_is_live

        return task_is_live(get(rid))
    except Exception:
        return False


def acquire_device_lease(
    *,
    sn: str,
    package_id: str,
    run_id: str,
    case_id: str = "",
    project_id: str = "",
    platform: str = "android",
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> tuple[bool, str]:
    s = str(sn or "").strip()
    p = str(package_id or "").strip()
    rid = str(run_id or "").strip()
    if not s or not p or not rid:
        return False, "缺少 sn / package / run_id"
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        from mino_nexus.runtime.run_context import is_web_slot

        web_slot = is_web_slot(s, platform)
        rows = (
            db.query(DeviceResourceLease)
            .filter(DeviceResourceLease.sn == s, DeviceResourceLease.package_id == p)
            .all()
        )
        for row in list(rows):
            if _expired(str(row.expires_at or "")):
                db.delete(row)
        db.flush()
        rows = (
            db.query(DeviceResourceLease)
            .filter(DeviceResourceLease.sn == s, DeviceResourceLease.package_id == p)
            .all()
        )
        for row in rows:
            holder = str(row.run_id or "").strip()
            if holder == rid:
                row.expires_at = _expiry(ttl_sec)
                row.case_id = str(case_id or "")[:64]
                db.commit()
                return True, ""
            if web_slot:
                continue
            if _expired(str(row.expires_at or "")):
                continue
            if not _holder_run_still_live(holder):
                db.delete(row)
                db.flush()
                continue
            return False, (
                f"设备 {s} 包 {p} 已被进行中的任务 {holder[:12]} 占用；"
                f"请等待该任务结束或取消后再跑。"
            )
        db.add(
            DeviceResourceLease(
                sn=s,
                package_id=p,
                run_id=rid,
                case_id=str(case_id or "")[:64],
                project_id=str(project_id or "")[:48],
                platform=str(platform or "android")[:16],
                leased_at=_now(),
                expires_at=_expiry(ttl_sec),
            )
        )
        db.commit()
    return True, ""


def release_device_lease(sn: str, package_id: str, run_id: str) -> int:
    """释放单条 sn×package 租约（同 run 多机时勿整 run 删除）。"""
    s = str(sn or "").strip()
    p = str(package_id or "").strip()
    rid = str(run_id or "").strip()
    if not s or not p or not rid:
        return 0
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        n = (
            db.query(DeviceResourceLease)
            .filter(
                DeviceResourceLease.sn == s,
                DeviceResourceLease.package_id == p,
                DeviceResourceLease.run_id == rid,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(n or 0)


def purge_expired_device_leases() -> int:
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        rows = db.query(DeviceResourceLease).all()
        n = 0
        for row in rows:
            if _expired(str(row.expires_at or "")):
                db.delete(row)
                n += 1
        db.commit()
        return n


def release_device_leases_for_run(run_id: str) -> int:
    rid = str(run_id or "").strip()
    if not rid:
        return 0
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        n = (
            db.query(DeviceResourceLease)
            .filter(DeviceResourceLease.run_id == rid)
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(n or 0)


def get_active_lease(sn: str, package_id: str) -> dict[str, Any] | None:
    s = str(sn or "").strip()
    p = str(package_id or "").strip()
    if not s or not p:
        return None
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        rows = (
            db.query(DeviceResourceLease)
            .filter(DeviceResourceLease.sn == s, DeviceResourceLease.package_id == p)
            .order_by(DeviceResourceLease.id.desc())
            .all()
        )
        for row in rows:
            if _expired(str(row.expires_at or "")):
                continue
            return {
                "sn": row.sn,
                "package_id": row.package_id,
                "run_id": row.run_id,
                "case_id": row.case_id or "",
                "project_id": row.project_id or "",
                "platform": row.platform or "",
                "expires_at": row.expires_at or "",
            }
    return None
