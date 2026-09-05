"""设备身份落盘：一条 SN 一行。

账号归属按**首次出现**锁定，换 Scout 只改当前位置，不另开一行。
web 槽位是 `web`+scout_id，本来就不会在节点间搬家；ADB 序列号会。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mino_nexus.services.node_store import sanitize_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _row_from_model(row) -> dict[str, Any]:
    extra = dict(row.extra or {}) if isinstance(row.extra, dict) else {}
    extra["sn"] = row.sn
    extra["platform"] = row.platform or extra.get("platform") or ""
    extra["model"] = row.model or extra.get("model") or ""
    extra["status"] = row.status or extra.get("status") or ""
    extra["channels"] = row.channels if isinstance(row.channels, dict) else extra.get("channels") or {}
    extra["owner_user_id"] = row.owner_user_id or extra.get("owner_user_id") or ""
    extra["owner_name"] = row.owner_name or extra.get("owner_name") or ""
    extra["current_scout_id"] = row.node_id or extra.get("current_scout_id") or ""
    extra["current_studio_id"] = row.studio_id or extra.get("current_studio_id") or ""
    extra["updated_at"] = row.updated_at or extra.get("updated_at") or ""
    return extra


def _root() -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.device import Device

    ensure_db()
    db = SessionLocal()
    try:
        devices: dict[str, dict[str, Any]] = {}
        for row in db.query(Device).all():
            devices[row.sn] = _row_from_model(row)
        return {"devices": devices}
    finally:
        db.close()


def _device_row(sn: str, row: dict, secrets: dict) -> Any:
    from mino_nexus.models.device import Device

    pw = ""
    sec = secrets.get(sn) if isinstance(secrets, dict) else None
    if isinstance(sec, dict):
        pw = str(sec.get("password") or sec.get("lock_password") or "")
    elif isinstance(sec, str):
        pw = sec
    extra = dict(row)
    return Device(
        sn=sn,
        platform=str(row.get("platform") or row.get("type") or ""),
        model=str(row.get("model") or ""),
        node_id=str(row.get("current_scout_id") or row.get("node_id") or row.get("scout_id") or ""),
        studio_id=str(row.get("current_studio_id") or row.get("studio_id") or ""),
        owner_user_id=str(row.get("owner_user_id") or ""),
        owner_name=str(row.get("owner_name") or ""),
        password=pw,
        status=str(row.get("status") or ""),
        channels=row.get("channels") if isinstance(row.get("channels"), dict) else {},
        extra=extra,
        updated_at=str(row.get("updated_at") or ""),
    )


def _save(root: dict[str, Any]) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.device import Device

    with session_scope() as db:
        keep: set[str] = set()
        existing_pw = {r.sn: r.password or "" for r in db.query(Device).all()}
        for sn, row in (root.get("devices") or {}).items():
            if not isinstance(row, dict):
                continue
            sid = str(row.get("sn") or sn).strip()
            if not sid:
                continue
            keep.add(sid)
            obj = _device_row(sid, row, {})
            if not obj.password:
                obj.password = existing_pw.get(sid) or ""
            db.merge(obj)
        for row in db.query(Device).all():
            if row.sn not in keep:
                db.delete(row)


def _kind_of(sn: str, platform: str = "") -> str:
    sid = str(sn or "").strip().lower()
    plat = str(platform or "").strip().lower()
    if sid.startswith("web"):
        return "web"
    if plat in ("ios", "wda") or "ios" in sid:
        return "ios"
    if plat in ("android", "adb") or sid:
        return "adb"
    return plat or "unknown"


def list_devices() -> dict[str, dict[str, Any]]:
    root = _root()
    out: dict[str, dict[str, Any]] = {}
    for key, row in (root.get("devices") or {}).items():
        if not isinstance(row, dict):
            continue
        sn = str(row.get("sn") or key or "").strip()
        if not sn:
            continue
        out[sn] = dict(row)
        out[sn]["sn"] = sn
    return out


def get_device(sn: str) -> dict[str, Any] | None:
    sid = str(sn or "").strip()
    return list_devices().get(sid)


def remember(
    sn: str,
    *,
    platform: str = "",
    model: str = "",
    scout_id: str = "",
    studio_id: str = "",
    owner_user_id: str = "",
    owner_name: str = "",
    channels: dict[str, Any] | None = None,
    status: str = "",
) -> dict[str, Any]:
    """按 SN upsert。owner_user_id 只在空的时候写入。"""
    sid = str(sn or "").strip()
    if not sid:
        return {}
    root = _root()
    prev = root["devices"].get(sid) if isinstance(root["devices"].get(sid), dict) else {}
    row = dict(prev)
    row["sn"] = sid
    if platform:
        row["platform"] = str(platform)
    if model:
        row["model"] = str(model)
    row["kind"] = _kind_of(sid, str(row.get("platform") or platform))
    nid = sanitize_id(scout_id)
    if nid:
        row["current_scout_id"] = nid
        if not sanitize_id(str(row.get("first_scout_id") or "")):
            row["first_scout_id"] = nid
    studio = sanitize_id(studio_id)
    if studio:
        row["current_studio_id"] = studio
    owner = sanitize_id(owner_user_id)
    if owner and not sanitize_id(str(row.get("owner_user_id") or "")):
        row["owner_user_id"] = owner
        if owner_name:
            row["owner_name"] = str(owner_name).strip()
    elif owner_name and not str(row.get("owner_name") or "").strip():
        row["owner_name"] = str(owner_name).strip()
    if isinstance(channels, dict):
        row["channels"] = dict(channels)
    if status:
        row["status"] = str(status)
    if not row.get("first_seen"):
        row["first_seen"] = prev.get("first_seen") or _now_iso()
    row["updated_at"] = _now_iso()
    root["devices"][sid] = row
    _save(root)
    return row


def public_device(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["sn"] = str(item.get("sn") or "")
    item["owner_user_id"] = sanitize_id(str(item.get("owner_user_id") or ""))
    item["owner_name"] = str(item.get("owner_name") or "").strip()
    item["first_scout_id"] = sanitize_id(str(item.get("first_scout_id") or ""))
    item["current_scout_id"] = sanitize_id(str(item.get("current_scout_id") or ""))
    item["current_studio_id"] = sanitize_id(str(item.get("current_studio_id") or ""))
    return item
