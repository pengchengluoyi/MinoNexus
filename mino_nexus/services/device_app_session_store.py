"""device_app_sessions 读写。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mino_nexus.core.database import session_scope
from mino_nexus.models.device_app_session import DeviceAppSession


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: DeviceAppSession) -> dict[str, Any]:
    from mino_nexus.services.session_match import normalize_device_session

    return {
        "sn": row.sn,
        "package_id": row.package_id,
        "app_version": row.app_version or "",
        "session": normalize_device_session(row.session or ""),
        "bound_account_id": row.bound_account_id or "",
        "identity_hint": row.identity_hint or "",
        "stale": bool(row.stale),
        "stale_reason": row.stale_reason or "",
        "lease_run_id": row.lease_run_id or "",
        "observed_at": row.observed_at or "",
        "updated_at": row.updated_at or "",
    }


def list_sessions(
    *,
    sn: str = "",
    package_id: str = "",
    package_ids: list[str] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    with session_scope() as db:
        q = db.query(DeviceAppSession)
        s = str(sn or "").strip()
        p = str(package_id or "").strip()
        if s:
            q = q.filter(DeviceAppSession.sn == s)
        if p:
            q = q.filter(DeviceAppSession.package_id == p)
        elif package_ids:
            pkgs = [str(x).strip() for x in package_ids if str(x).strip()]
            if pkgs:
                q = q.filter(DeviceAppSession.package_id.in_(pkgs))
        rows = q.order_by(DeviceAppSession.updated_at.desc()).limit(max(1, min(int(limit), 2000))).all()
        return [_row_to_dict(r) for r in rows]


def get_session(sn: str, package_id: str) -> dict[str, Any] | None:
    s = str(sn or "").strip()
    p = str(package_id or "").strip()
    if not s or not p:
        return None
    with session_scope() as db:
        row = (
            db.query(DeviceAppSession)
            .filter(DeviceAppSession.sn == s, DeviceAppSession.package_id == p)
            .first()
        )
        return _row_to_dict(row) if row else None


def upsert_session(
    sn: str,
    package_id: str,
    *,
    session: str | None = None,
    bound_account_id: str | None = None,
    clear_binding: bool = False,
    identity_hint: str | None = None,
    app_version: str | None = None,
    stale: bool | None = None,
    stale_reason: str | None = None,
    lease_run_id: str | None = None,
    source: str = "",
) -> dict[str, Any]:
    s = str(sn or "").strip()
    p = str(package_id or "").strip()
    if not s or not p:
        raise ValueError("sn and package_id required")
    now = _now()
    with session_scope() as db:
        row = (
            db.query(DeviceAppSession)
            .filter(DeviceAppSession.sn == s, DeviceAppSession.package_id == p)
            .first()
        )
        if row is None:
            row = DeviceAppSession(sn=s, package_id=p, updated_at=now, observed_at=now)
            db.add(row)
        if session is not None:
            from mino_nexus.services.session_match import normalize_device_session

            row.session = normalize_device_session(session)[:32]
            row.observed_at = now
        if clear_binding:
            row.bound_account_id = ""
        elif bound_account_id is not None:
            row.bound_account_id = str(bound_account_id or "").strip()[:64]
        if identity_hint is not None:
            row.identity_hint = str(identity_hint or "")[:500]
        if app_version is not None:
            row.app_version = str(app_version or "")[:64]
        if stale is not None:
            row.stale = 1 if stale else 0
        if stale_reason is not None:
            row.stale_reason = str(stale_reason or "")[:200]
        if lease_run_id is not None:
            row.lease_run_id = str(lease_run_id or "")[:80]
        row.updated_at = now
        db.commit()
        db.refresh(row)
        return _row_to_dict(row)
