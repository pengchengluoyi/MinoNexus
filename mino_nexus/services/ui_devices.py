"""把 NodeRegistry 缓存转成 UI 认的设备 / 节点行。连通性只来自 Scout 心跳。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from mino_nexus.services import auth_store
from mino_nexus.services.device_secrets import lock_password_public
from mino_nexus.services.device_store import get_device, list_devices as stored_devices, public_device
from mino_nexus.services.node_registry import NodeRegistry, NodeSession, get_registry
from mino_nexus.services.node_store import list_nodes as stored_nodes
from mino_nexus.services.node_store import list_studios as stored_studios

_ONLINE_CHANNEL = frozenset({"connected", "online", "available"})


def _type_of(platform: str) -> str:
    p = str(platform or "").lower()
    if p in ("android", "adb"):
        return "android"
    if p in ("ios", "wda"):
        return "ios"
    if p in ("web", "playwright"):
        return "web"
    return p or "unknown"


_CHANNEL_ALIASES = (
    ("adb", "adb_state"),
    ("playwright", "playwright_state"),
    ("remote", "remote_state"),
    ("ios", "ios_state"),
)


def _channel_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("state") or value.get("status") or "").strip().lower()
    return str(value or "").strip().lower()


def device_channels_online(channels: Any) -> bool:
    """True when Scout reported at least one exec channel as connected."""
    if not isinstance(channels, dict):
        return False
    return any(_channel_value(v) in _ONLINE_CHANNEL for v in channels.values())


def device_ui_online(node_alive: bool, channels: Any) -> bool:
    """Device row is online only if the node is alive *and* a channel is up.

    Node-alive alone used to keep unplugged phones `status=online` forever.
    """
    return bool(node_alive) and device_channels_online(channels)


def _ui_channels(channels: dict[str, Any]) -> dict[str, Any]:
    """协议通道名 + 旧 UI 的 *_state 别名。不探测，只转译 Scout 已上报的缓存。"""
    out = dict(channels)
    for proto, legacy in _CHANNEL_ALIASES:
        if proto in out and legacy not in out:
            out[legacy] = out[proto]
    return out


def _last_online(ts: Any) -> str:
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def ui_devices(registry: Optional[NodeRegistry] = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in (registry or get_registry()).devices():
        raw = row.get("channels") if isinstance(row.get("channels"), dict) else {}
        channels = _ui_channels(raw)
        online = device_ui_online(bool(row.get("node_alive")), channels)
        sn = row.get("sn") or ""
        secrets = lock_password_public(sn)
        persisted = get_device(sn) or {}
        owner_id = str(persisted.get("owner_user_id") or "")
        owner_name = str(persisted.get("owner_name") or "").strip()
        if owner_id and not owner_name:
            pub = auth_store.public_user_by_id(owner_id)
            if pub:
                owner_name = str(pub.get("name") or pub.get("username") or "")
        out.append({
            "sn": sn,
            "node_id": row.get("node_id") or persisted.get("current_scout_id") or "",
            "type": _type_of(str(row.get("platform") or persisted.get("platform") or "")),
            "model": row.get("model") or persisted.get("model") or "",
            "ip": "",
            "status": "online" if online else "offline",
            "role": "node",
            "channels": channels,
            "platform": row.get("platform") or persisted.get("platform") or "",
            "last_online": _last_online(row.get("last_seen")),
            "password_configured": bool(secrets.get("password_configured")),
            "owner_user_id": owner_id,
            "owner_name": owner_name,
            "first_scout_id": persisted.get("first_scout_id") or "",
            "current_scout_id": persisted.get("current_scout_id") or row.get("node_id") or "",
        })
    return out


def _offline_device_row(persisted: dict[str, Any]) -> dict[str, Any]:
    row = public_device(persisted)
    owner_id = str(row.get("owner_user_id") or "")
    owner_name = str(row.get("owner_name") or "").strip()
    if owner_id and not owner_name:
        pub = auth_store.public_user_by_id(owner_id)
        if pub:
            owner_name = str(pub.get("name") or pub.get("username") or "")
    return {
        "sn": row.get("sn") or "",
        "node_id": row.get("current_scout_id") or "",
        "type": _type_of(str(row.get("platform") or "")),
        "model": row.get("model") or "",
        "ip": "",
        "status": "offline",
        "role": "node",
        "channels": row.get("channels") if isinstance(row.get("channels"), dict) else {},
        "platform": row.get("platform") or "",
        "last_online": row.get("updated_at") or "",
        "password_configured": bool(lock_password_public(row.get("sn") or "").get("password_configured")),
        "owner_user_id": owner_id,
        "owner_name": owner_name,
        "first_scout_id": row.get("first_scout_id") or "",
        "current_scout_id": row.get("current_scout_id") or "",
    }


def _iso(ts: Any) -> str:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(float(ts), timezone.utc).astimezone().isoformat()
    text = str(ts or "").strip()
    return text


def _offline_row(snap: dict[str, Any]) -> dict[str, Any]:
    nid = str(snap.get("node_id") or "")
    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for persisted in stored_devices().values():
        sn = str(persisted.get("sn") or "")
        if not sn or sn in seen:
            continue
        if str(persisted.get("current_scout_id") or "") != nid:
            continue
        devices.append(_offline_device_row(persisted))
        seen.add(sn)
    return {
        "node_id": nid,
        "scout_id": nid,
        "hostname": snap.get("hostname") or "",
        "platform": snap.get("platform") or "",
        "arch": snap.get("arch") or "",
        "studio_id": snap.get("studio_id") or "",
        "owner_user_id": snap.get("owner_user_id") or "",
        "scout_version": snap.get("scout_version") or "",
        "alive": False,
        "busy": False,
        "draining": False,
        "active_runs": [],
        "executors": {},
        "devices": devices,
        "device_count": len(devices) or int(snap.get("device_count") or 0),
        "last_seen": snap.get("last_seen") or 0,
        "last_heartbeat": _iso(snap.get("last_seen") or snap.get("updated_at") or ""),
        "last_seen_ago_sec": None,
        "status": "offline",
        "online": False,
    }


def ui_nodes(registry: Optional[NodeRegistry] = None) -> list[dict[str, Any]]:
    reg = registry or get_registry()
    live = {n.node_id: n for n in reg.nodes()}
    stored = stored_nodes()
    ids = sorted(set(live) | set(stored))
    out: list[dict[str, Any]] = []
    for nid in ids:
        session = live.get(nid)
        snap = stored.get(nid) or {}
        if session is None:
            out.append(_offline_row(snap))
            continue
        devices = ui_devices_for_node(session, registry=reg)
        out.append({
            **session.brief(),
            "studio_id": session.studio_id or snap.get("studio_id") or "",
            "owner_user_id": session.owner_user_id or snap.get("owner_user_id") or "",
            "hostname": session.hostname or snap.get("hostname") or "",
            "status": "online" if session.alive else "offline",
            "online": session.alive,
            "device_count": len(session.devices),
            "last_heartbeat": _iso(session.last_seen),
            "devices": devices,
        })
    return out


def ui_devices_for_node(node: NodeSession, registry: Optional[NodeRegistry] = None) -> list[dict[str, Any]]:
    nid = node.node_id
    live = [d for d in ui_devices(registry) if d.get("node_id") == nid or d.get("current_scout_id") == nid]
    seen = {str(d.get("sn") or "") for d in live}
    for persisted in stored_devices().values():
        sn = str(persisted.get("sn") or "")
        if not sn or sn in seen:
            continue
        if str(persisted.get("current_scout_id") or "") != nid:
            continue
        live.append(_offline_device_row(persisted))
        seen.add(sn)
    return live


def _append_unique_devices(bucket: dict[str, Any], incoming: list[dict[str, Any]]) -> None:
    devices = list(bucket.get("devices") or [])
    seen = {str(d.get("sn") or "") for d in devices}
    for dev in incoming:
        sn = str(dev.get("sn") or "")
        if not sn or sn in seen:
            continue
        devices.append(dev)
        seen.add(sn)
    bucket["devices"] = devices
    bucket["device_count"] = len(devices)


def _empty_studio_row(
    sid: str,
    *,
    owner_user_id: str = "",
    owner_name: str = "",
    first_seen: str = "",
    updated_at: str = "",
) -> dict[str, Any]:
    uid = str(owner_user_id or "")
    pub = auth_store.public_user_by_id(uid) if uid else None
    return {
        "studio_id": sid,
        "owner_user_id": uid,
        "owner_name": owner_name or (pub or {}).get("name") or (pub or {}).get("username") or "",
        "scout_count": 0,
        "device_count": 0,
        "devices": [],
        "first_seen": first_seen,
        "updated_at": updated_at,
    }


def ui_assets(registry: Optional[NodeRegistry] = None) -> dict[str, Any]:
    nodes = ui_nodes(registry)
    for row in nodes:
        uid = str(row.get("owner_user_id") or "")
        pub = auth_store.public_user_by_id(uid) if uid else None
        if pub:
            row["owner_name"] = pub.get("name") or pub.get("username") or ""
        if not row.get("devices"):
            nid = str(row.get("node_id") or "")
            extras = [
                _offline_device_row(persisted)
                for persisted in stored_devices().values()
                if str(persisted.get("current_scout_id") or "") == nid
            ]
            row["devices"] = extras
        row["device_count"] = len(row.get("devices") or [])

    studio_rows: dict[str, dict[str, Any]] = {}
    for sid, snap in stored_studios().items():
        studio_rows[sid] = _empty_studio_row(
            sid,
            owner_user_id=str(snap.get("owner_user_id") or ""),
            owner_name=str(snap.get("owner_name") or ""),
            first_seen=str(snap.get("first_seen") or ""),
            updated_at=str(snap.get("updated_at") or ""),
        )

    live_by_sn: dict[str, dict[str, Any]] = {}
    for row in nodes:
        sid = str(row.get("studio_id") or "").strip()
        if sid:
            bucket = studio_rows.setdefault(sid, _empty_studio_row(
                sid,
                owner_user_id=str(row.get("owner_user_id") or ""),
                owner_name=str(row.get("owner_name") or ""),
            ))
            bucket["scout_count"] += 1
            if row.get("owner_name") and not bucket.get("owner_name"):
                bucket["owner_name"] = row.get("owner_name")
        for dev in row.get("devices") or []:
            sn = str(dev.get("sn") or "")
            if sn:
                live_by_sn[sn] = dev

    claimed: set[str] = set()
    for persisted in stored_devices().values():
        sn = str(persisted.get("sn") or "").strip()
        if not sn or sn in claimed:
            continue
        sid = str(persisted.get("current_studio_id") or "").strip()
        if not sid:
            for row in nodes:
                if any(str(d.get("sn") or "") == sn for d in (row.get("devices") or [])):
                    sid = str(row.get("studio_id") or "").strip()
                    if sid:
                        break
        if not sid:
            continue
        bucket = studio_rows.setdefault(sid, _empty_studio_row(
            sid,
            owner_user_id=str(persisted.get("owner_user_id") or ""),
            owner_name=str(persisted.get("owner_name") or ""),
        ))
        _append_unique_devices(bucket, [live_by_sn.get(sn) or _offline_device_row(persisted)])
        claimed.add(sn)

    for row in nodes:
        sid = str(row.get("studio_id") or "").strip()
        if not sid:
            continue
        leftover = [d for d in (row.get("devices") or []) if str(d.get("sn") or "") not in claimed]
        if leftover:
            _append_unique_devices(studio_rows[sid], leftover)
            claimed.update(str(d.get("sn") or "") for d in leftover if d.get("sn"))

    studios = sorted(studio_rows.values(), key=lambda x: str(x.get("studio_id") or ""))
    device_sns = set()
    for row in nodes:
        for dev in row.get("devices") or []:
            if dev.get("sn"):
                device_sns.add(dev["sn"])
    for persisted in stored_devices().values():
        if persisted.get("sn"):
            device_sns.add(persisted["sn"])
    return {
        "scout_count": len(nodes),
        "studio_count": len(studios),
        "device_count": len(device_sns),
        "scouts": nodes,
        "studios": studios,
    }
