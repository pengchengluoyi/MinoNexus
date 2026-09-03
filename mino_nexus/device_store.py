"""设备身份落盘：一条 SN 一行。

账号归属按**首次出现**锁定，换 Scout 只改当前位置，不另开一行。
web 槽位是 `web`+scout_id，本来就不会在节点间搬家；ADB 序列号会。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mino_nexus.json_store import load_json, save_json
from mino_nexus.node_store import sanitize_id

_FILE = "devices.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _root() -> dict[str, Any]:
    raw = load_json(_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    devices = raw.get("devices")
    if not isinstance(devices, dict):
        raw["devices"] = {}
    return raw


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
    save_json(_FILE, root)
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
