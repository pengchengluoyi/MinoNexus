"""设备锁屏密码。明文落盘，跟 SMTP 一样，不另做加密。"""
from __future__ import annotations

from typing import Any


def get_lock_password(sn: str) -> str:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.device import Device

    key = str(sn or "").strip()
    if not key:
        return ""
    ensure_db()
    db = SessionLocal()
    try:
        row = db.get(Device, key)
        return str(row.password or "").strip() if row else ""
    finally:
        db.close()


def lock_password_public(sn: str) -> dict[str, Any]:
    return {"password_configured": bool(get_lock_password(sn))}


def set_lock_password(sn: str, password: str) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.device import Device

    key = str(sn or "").strip()
    if not key:
        raise ValueError("缺少设备 SN")
    pwd = str(password or "").strip()
    with session_scope() as db:
        row = db.get(Device, key)
        if row is None:
            row = Device(sn=key, extra={"sn": key})
            db.add(row)
        row.password = pwd
    return lock_password_public(key)
