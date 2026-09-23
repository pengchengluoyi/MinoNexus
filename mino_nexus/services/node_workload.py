"""合并 Scout 心跳 device_workload 与 Nexus run_store 的在途 run。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.services.device_store import list_devices as stored_devices
from mino_nexus.services.node_registry import NodeSession, get_registry
from mino_nexus.runtime.run_context import WEB_PLAYWRIGHT_PARALLEL_LANES, is_web_slot
from mino_nexus.services.run_store import get, running_run_ids_for_sn


def build_node_workload(node: NodeSession) -> dict[str, Any]:
    scout_by_sn: dict[str, P.DeviceWorkload] = {
        w.sn: w for w in (node.device_workload or []) if str(w.sn or "").strip()
    }
    devices_out: list[dict[str, Any]] = []
    for sn in sorted(node.devices):
        dev = node.devices[sn]
        scout = scout_by_sn.get(sn)
        run_ids = running_run_ids_for_sn(sn)
        nexus_runs: list[dict[str, Any]] = []
        for rid in run_ids:
            doc = get(rid) or {}
            nexus_runs.append({
                "run_id": rid,
                "app_id": doc.get("app_id") or "",
                "status": doc.get("status") or "",
            })
        devices_out.append({
            "sn": sn,
            "platform": dev.platform,
            "scout_step": asdict(scout) if scout else None,
            "nexus_run_ids": run_ids,
            "nexus_runs": nexus_runs,
            "busy": bool(scout or run_ids),
            **(
                {
                    "web_parallel_active": len(run_ids),
                    "web_parallel_max": WEB_PLAYWRIGHT_PARALLEL_LANES,
                    "web_parallel_full": len(run_ids) >= WEB_PLAYWRIGHT_PARALLEL_LANES,
                }
                if is_web_slot(sn, dev.platform or "web")
                else {}
            ),
        })
    web_sn = next((d["sn"] for d in devices_out if is_web_slot(d["sn"], d.get("platform") or "web")), "")
    web_active = 0
    if web_sn:
        web_active = len(running_run_ids_for_sn(web_sn, limit=WEB_PLAYWRIGHT_PARALLEL_LANES + 1))
    return {
        "node_id": node.node_id,
        "alive": node.alive,
        "busy": node.busy,
        "active_runs": list(node.active_runs or []),
        "scout_version": node.scout_version or "",
        "web_playwright_parallel": {
            "sn": web_sn,
            "active": web_active,
            "max": WEB_PLAYWRIGHT_PARALLEL_LANES,
            "full": web_active >= WEB_PLAYWRIGHT_PARALLEL_LANES,
        },
        "devices": devices_out,
    }


def _offline_device_sns(node_id: str) -> list[str]:
    nid = str(node_id or "").strip()
    sns: list[str] = []
    for row in stored_devices().values():
        sn = str(row.get("sn") or "").strip()
        if not sn:
            continue
        if str(row.get("current_scout_id") or "") == nid:
            sns.append(sn)
    return sorted(set(sns))


def build_offline_workload(node_id: str) -> dict[str, Any]:
    sns = _offline_device_sns(node_id)
    devices_out: list[dict[str, Any]] = []
    for sn in sns:
        run_ids = running_run_ids_for_sn(sn)
        nexus_runs = [
            {
                "run_id": rid,
                "app_id": (get(rid) or {}).get("app_id") or "",
                "status": (get(rid) or {}).get("status") or "",
            }
            for rid in run_ids
        ]
        devices_out.append({
            "sn": sn,
            "platform": "",
            "scout_step": None,
            "nexus_run_ids": run_ids,
            "nexus_runs": nexus_runs,
            "busy": bool(run_ids),
        })
    return {
        "node_id": node_id,
        "alive": False,
        "busy": bool(devices_out),
        "active_runs": [],
        "scout_version": "",
        "devices": devices_out,
        "source": "offline",
    }


def workload_for_node_id(node_id: str) -> dict[str, Any] | None:
    nid = str(node_id or "").strip()
    if not nid:
        return None
    node = get_registry().get_node(nid)
    if node is not None:
        return build_node_workload(node)
    offline = build_offline_workload(nid)
    if offline.get("devices"):
        return offline
    return None
