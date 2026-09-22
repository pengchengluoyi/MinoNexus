"""项目视角：设备 × 被测包 机态矩阵（含未登记占位行）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services.case_resource_claim import project_package_ids
from mino_nexus.services.device_app_session_store import list_sessions
from mino_nexus.services.ui_devices import ui_devices
from mino_nexus.runtime.run_context import version_string_looks_invalid


def _enrich_session_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    registered = bool(out.get("registered"))
    sess = str(out.get("session") or "unknown").strip().lower()
    if not registered:
        out["session_display"] = "未观测"
    elif sess == "unknown":
        out["session_display"] = "未知（待 inspect）"
    elif sess == "logged_in":
        out["session_display"] = "已登录"
    elif sess in ("logged_out", "guest"):
        out["session_display"] = "未登录/游客"
    else:
        out["session_display"] = sess

    ver = str(out.get("app_version") or "").strip()
    if ver and version_string_looks_invalid(ver):
        out["app_version_display"] = "无效（误写入，可重跑用例刷新）"
        out["app_version_bad"] = True
    else:
        out["app_version_display"] = ver or "—"

    hint = str(out.get("identity_hint") or "").strip()
    bound = str(out.get("bound_account_id") or "").strip()
    if hint:
        out["identity_display"] = hint
    elif bound:
        out["identity_display"] = f"已绑定 {bound[:20]}"
    elif registered:
        out["identity_display"] = "未核对"
    else:
        out["identity_display"] = "—"

    stale_reason = str(out.get("stale_reason") or "").strip()
    if stale_reason:
        out["stale_note"] = stale_reason
    elif out.get("stale"):
        out["stale_note"] = "机态可能过期"
    else:
        out["stale_note"] = ""
    return out


def _device_sns(*, online_only: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in ui_devices() or []:
        sn = str(d.get("sn") or d.get("id") or "").strip()
        if not sn:
            continue
        online = bool(d.get("online"))
        if online_only and not online:
            continue
        rows.append(
            {
                "sn": sn,
                "platform": str(d.get("platform") or d.get("type") or "").strip().lower(),
                "label": str(d.get("label") or d.get("name") or sn)[:80],
                "online": online,
            }
        )
    return rows


def list_project_device_app_matrix(
    project_id: str,
    *,
    app_id: str = "",
    sn: str = "",
    package_id: str = "",
    limit: int = 500,
    include_offline_devices: bool = True,
) -> dict[str, Any]:
    """合并 DB 登记 + 项目包名 × 在线设备 占位行。"""
    pid = str(project_id or "").strip()
    pkgs = project_package_ids(pid, app_id=app_id) if pid else []
    p_filter = str(package_id or "").strip()
    if p_filter:
        pkgs = [p_filter]
    sn_filter = str(sn or "").strip()

    db_rows = list_sessions(
        sn=sn_filter,
        package_id=p_filter if p_filter else "",
        package_ids=pkgs if pkgs and not p_filter else None,
        limit=limit,
    )
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in db_rows:
        key = (str(row.get("sn") or ""), str(row.get("package_id") or ""))
        if key[0] and key[1]:
            by_key[key] = {**row, "registered": True}

    devices = _device_sns(online_only=not include_offline_devices)
    if not devices and include_offline_devices:
        devices = _device_sns(online_only=False)
    if sn_filter:
        devices = [d for d in devices if d["sn"] == sn_filter]

    pkg_list = pkgs or []
    if not pkg_list and not by_key:
        # 无 App 配置时仍展示已有 DB 行
        for row in db_rows:
            key = (str(row.get("sn") or ""), str(row.get("package_id") or ""))
            by_key[key] = {**row, "registered": True}

    def _placeholder(sn_val: str, pkg: str, dev: dict[str, Any] | None) -> None:
        key = (sn_val, pkg)
        if key in by_key:
            if dev:
                by_key[key].setdefault("device_label", dev.get("label") or "")
                by_key[key].setdefault("device_platform", dev.get("platform") or "")
                by_key[key].setdefault("device_online", dev.get("online"))
            return
        by_key[key] = {
            "sn": sn_val,
            "package_id": pkg,
            "app_version": "",
            "session": "unknown",
            "bound_account_id": "",
            "identity_hint": "",
            "stale": False,
            "stale_reason": "",
            "lease_run_id": "",
            "observed_at": "",
            "updated_at": "",
            "registered": False,
            "device_label": (dev or {}).get("label") or ("" if sn_val else "无在线设备"),
            "device_platform": (dev or {}).get("platform") or "",
            "device_online": (dev or {}).get("online") if dev else False,
        }

    if devices and pkg_list:
        for dev in devices:
            for pkg in pkg_list:
                _placeholder(dev["sn"], pkg, dev)
    elif pkg_list:
        for pkg in pkg_list:
            _placeholder("", pkg, None)
    elif devices:
        for dev in devices:
            for row in db_rows:
                if str(row.get("sn") or "") == dev["sn"]:
                    key = (dev["sn"], str(row.get("package_id") or ""))
                    if key[1]:
                        _placeholder(key[0], key[1], dev)

    out = list(by_key.values())
    out = [_enrich_session_row(r) for r in out]
    out.sort(key=lambda r: (str(r.get("sn") or ""), str(r.get("package_id") or "")))
    if len(out) > limit:
        out = out[:limit]
    return {
        "sessions": out,
        "count": len(out),
        "project_packages": pkgs,
        "devices": [d["sn"] for d in devices],
    }
