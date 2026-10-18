"""设备 × App 跑批租约。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from mino_nexus.models.resource_ops import DeviceResourceLease
from mino_nexus.services.run_write_safety import protected

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
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        from mino_nexus.runtime.run_context import is_web_slot

        web_slot = is_web_slot(s, platform)
        rows = (
            db.query(DeviceResourceLease)
            .filter(DeviceResourceLease.sn == s, DeviceResourceLease.package_id == p)
            .all()
        )
        for row in rows:
            holder = str(row.run_id or "").strip()
            if protected(holder, db=db, sn=s):
                return False, f"设备存在未确认停止的写动作，任务 {holder} 的资源仍隔离"
            if holder == rid:
                row.expires_at = _expiry(ttl_sec)
                row.case_id = str(case_id or "")[:64]
                db.commit()
                return True, ""
            if web_slot:
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


def release_device_lease(sn: str, package_id: str, run_id: str, *, attempt_id: str = "") -> int:
    """释放单条 sn×package 租约（同 run 多机时勿整 run 删除）。"""
    s = str(sn or "").strip()
    p = str(package_id or "").strip()
    rid = str(run_id or "").strip()
    if not s or not p or not rid:
        return 0
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        if protected(rid, db=db, sn=s):
            return 0
        if attempt_id:
            from mino_nexus.models.execution_resource import ExecutionResource
            current = db.query(ExecutionResource).filter_by(run_id=rid, sn=s).filter(ExecutionResource.state != "released").all()
            if any(resource.attempt_id != attempt_id for resource in current):
                return 0
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
    """Expiry alone cannot release remote work. Finalization/recovery owns release."""
    return 0


def release_device_leases_for_run(run_id: str) -> int:
    rid = str(run_id or "").strip()
    if not rid:
        return 0
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        rows = db.query(DeviceResourceLease).filter(DeviceResourceLease.run_id == rid).all()
        released = 0
        for row in rows:
            if protected(rid, db=db, sn=str(row.sn or "")):
                continue
            db.delete(row)
            released += 1
        db.commit()
        return released


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
