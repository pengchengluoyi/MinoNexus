"""SQLite-backed resource admission; UI checks are informative, not locks."""
from __future__ import annotations

from mino_nexus.models.device_reservation import DeviceReservation


class ResourceBusy(ValueError):
    def __init__(self, sn: str, run_id: str):
        super().__init__("设备或共享桌面繁忙，请等待当前任务结束")
        self.sn, self.busy_task_id = sn, run_id


def resource_group(row: dict, *, headless=True) -> str:
    sn, platform = str(row.get("sn") or ""), str(row.get("platform") or row.get("type") or "")
    node = str(row.get("node_id") or row.get("current_scout_id") or "")
    if platform == "desktop" or (platform == "web" and not headless):
        return f"desktop:{node}:console"
    return f"web:{sn}" if platform == "web" else f"device:{sn}"


def _recorded_target(db, payload: dict, sn: str) -> dict:
    from mino_nexus.models.device import Device
    row = dict((payload.get("device_targets") or {}).get(sn) or {})
    stored = db.get(Device, sn) if sn else None
    row.setdefault("sn", sn)
    if not row.get("platform"):
        row["platform"] = (payload.get("platforms_by_sn") or {}).get(sn) or (stored.platform if stored else payload.get("platform"))
    if not row.get("node_id"):
        row["node_id"] = stored.node_id if stored else ""
    if not row["node_id"] and row.get("platform") == "desktop" and str(sn).startswith("desktop:"):
        row["node_id"] = str(sn).split(":")[1]
    return row


def busy_for(row: dict, *, headless=True) -> str:
    from mino_nexus.core.database import session_scope
    group = resource_group(row, headless=headless)
    from mino_nexus.models.run import AppRegressionRun
    with session_scope() as db:
        if group.startswith("web:"):
            reservations = db.query(DeviceReservation).filter(DeviceReservation.resource_key.like(group + ":lane:%")).all()
        else:
            found = db.get(DeviceReservation, group)
            reservations = [found] if found else []
        owners = {r.run_id for r in reservations}
        # Historical tasks without reservation rows remain visible as busy.
        for run in db.query(AppRegressionRun).filter(AppRegressionRun.status.in_(("running", "queued"))):
            payload = run.payload if isinstance(run.payload, dict) else {}
            for sn in payload.get("sns") or [payload.get("sn") or run.sn]:
                target = _recorded_target(db, payload, sn)
                if resource_group(target, headless=payload.get("playwright_headless", True)) == group:
                    owners.add(run.run_id)
                    break
        return next(iter(owners), "") if len(owners) >= (4 if group.startswith("web:") else 1) else ""


def admit_in_transaction(db, doc: dict) -> None:
    """Caller holds BEGIN IMMEDIATE and persists the run in the same transaction."""
    from mino_nexus.models.run import AppRegressionRun
    rid = str(doc["run_id"])
    if doc.get("status") not in ("running", "queued"):
        db.query(DeviceReservation).filter_by(run_id=rid).delete()
        return
    previous = db.get(AppRegressionRun, rid)
    if previous is not None:
        return
    # Existing runs made before this upgrade also participate in admission.
    live = db.query(AppRegressionRun).filter(AppRegressionRun.status.in_(("running", "queued"))).all()
    acquired = set()
    for sn in doc.get("sns") or [doc.get("sn")]:
        if not sn:
            continue
        row = _recorded_target(db, doc, sn)
        group = resource_group(row, headless=doc.get("playwright_headless", True))
        if group in acquired:
            continue
        acquired.add(group)
        legacy_count = 0
        legacy_id = ""
        for run in live:
            payload = run.payload if isinstance(run.payload, dict) else {}
            for other_sn in payload.get("sns") or [payload.get("sn")]:
                old_row = _recorded_target(db, payload, other_sn)
                if resource_group(old_row, headless=payload.get("playwright_headless", True)) == group:
                    legacy_count += 1
                    legacy_id = run.run_id
                    break
        if legacy_count >= (4 if group.startswith("web:") else 1):
            raise ResourceBusy(sn, legacy_id)
        if group.startswith("web:"):
            occupied = {r.resource_key for r in db.query(DeviceReservation).filter(DeviceReservation.resource_key.like(group + ":lane:%"))}
            free = next((f"{group}:lane:{i}" for i in range(4) if f"{group}:lane:{i}" not in occupied), "")
            if not free:
                raise ResourceBusy(sn, "")
            group = free
        else:
            occupied = db.get(DeviceReservation, group)
            if occupied and occupied.run_id != rid:
                raise ResourceBusy(sn, occupied.run_id)
        db.add(DeviceReservation(resource_key=group, run_id=rid))


def owns_desktop(run_id: str, node_id: str) -> bool:
    from mino_nexus.core.database import session_scope
    with session_scope() as db:
        row = db.get(DeviceReservation, f"desktop:{node_id}:console")
        return bool(row and row.run_id == run_id)
