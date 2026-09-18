"""解析通用逻辑块 + 应用 override。"""
from __future__ import annotations

from typing import Any

from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID, NavFlowBlockCatalog


def load_block_row(*, app_id: str, block_id: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import SessionLocal

    db = SessionLocal()
    try:
        app = str(app_id or "").strip()
        bid = str(block_id or "").strip()
        if app and app != GLOBAL_APP_ID:
            row = (
                db.query(NavFlowBlockCatalog)
                .filter(NavFlowBlockCatalog.app_id == app, NavFlowBlockCatalog.block_id == bid)
                .first()
            )
            if row is not None and int(row.enabled or 0):
                return _row_dict(row)
        row = (
            db.query(NavFlowBlockCatalog)
            .filter(
                NavFlowBlockCatalog.app_id == GLOBAL_APP_ID,
                NavFlowBlockCatalog.block_id == bid,
            )
            .first()
        )
        if row is None or not int(row.enabled or 0):
            return None
        return _row_dict(row)
    finally:
        db.close()


def _row_dict(row: NavFlowBlockCatalog) -> dict[str, Any]:
    return {
        "app_id": str(row.app_id or ""),
        "block_id": str(row.block_id or ""),
        "block_origin": str(row.block_origin or ""),
        "display_name": str(row.display_name or ""),
        "steps_json": list(row.steps_json or []),
        "version": str(row.version or "v1"),
    }


def _overrides_for_app(app_id: str) -> list[dict[str, Any]]:
    app = str(app_id or "").strip()
    if not app:
        return []
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    doc = calib.read_draft(app) or store.load(app, version="v1") or {}
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    raw = meta.get("flow_block_overrides")
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def list_catalog(*, app_id: str = "") -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal
    from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID

    db = SessionLocal()
    try:
        q = db.query(NavFlowBlockCatalog).filter(NavFlowBlockCatalog.enabled == 1)
        app = str(app_id or "").strip()
        if app and app != GLOBAL_APP_ID:
            q = q.filter(NavFlowBlockCatalog.app_id.in_([GLOBAL_APP_ID, app]))
        else:
            q = q.filter(NavFlowBlockCatalog.app_id == GLOBAL_APP_ID)
        rows = q.order_by(NavFlowBlockCatalog.block_id).all()
        return [_row_dict(r) for r in rows]
    finally:
        db.close()


def get_overrides(app_id: str) -> list[dict[str, Any]]:
    return list(_overrides_for_app(app_id))


def save_overrides(app_id: str, overrides: list[dict[str, Any]], *, updated_by: str = "") -> list[dict[str, Any]]:
    app = str(app_id or "").strip()
    if not app:
        return []
    clean = [x for x in overrides if isinstance(x, dict)]
    from mino_nexus.services import nav_calibration_store as calib

    doc = calib.read_draft(app) or {}
    meta = dict(doc.get("meta") or {})
    meta["flow_block_overrides"] = clean
    doc["meta"] = meta
    calib.save_draft(app, doc, updated_by=updated_by or "flow_block_overrides")
    return clean


def is_global_display_flow_block(block: dict[str, Any]) -> bool:
    bid = str(block.get("flow_block_id") or "").strip()
    origin = str(block.get("block_origin") or "").strip()
    if origin == "global_catalog":
        return True
    if bid.startswith("fb.global."):
        return True
    return False


def strip_global_flow_blocks_from_doc(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Studio 架构图：去掉通用逻辑块分段，避免与应用 flow_blocks 混淆。"""
    if not isinstance(doc, dict):
        return {}
    out = dict(doc)
    meta = dict(out.get("meta") or {})
    blocks = meta.get("flow_blocks")
    if isinstance(blocks, list):
        kept = [b for b in blocks if isinstance(b, dict) and not is_global_display_flow_block(b)]
        meta["flow_blocks"] = kept
        if kept:
            meta["flow_block_display_count"] = len(
                [b for b in kept if len(b.get("state_ids") or []) >= 2]
            )
        else:
            meta.pop("flow_block_display_count", None)
    layout = meta.get("studio_layout")
    if isinstance(layout, dict):
        states = layout.get("states")
        if isinstance(states, dict):
            for sid, pos in list(states.items()):
                if not isinstance(pos, dict):
                    continue
                bid = str(pos.get("flow_block_id") or "")
                if bid.startswith("fb.global."):
                    pos = dict(pos)
                    pos["flow_block_id"] = ""
                    states[sid] = pos
    out["meta"] = meta
    return out


def resolve_effective_steps(*, app_id: str, block_id: str) -> list[dict[str, Any]]:
    base = load_block_row(app_id=GLOBAL_APP_ID, block_id=block_id)
    if not base:
        return []
    steps = [dict(s) for s in (base.get("steps_json") or []) if isinstance(s, dict)]
    skip: set[str] = set()
    for ov in _overrides_for_app(app_id):
        if str(ov.get("global_block_id") or "") != block_id:
            continue
        for sid in ov.get("skip_step_ids") or []:
            skip.add(str(sid))
        repl = ov.get("steps")
        if isinstance(repl, list) and repl:
            steps = [dict(s) for s in repl if isinstance(s, dict)]
    if skip:
        steps = [s for s in steps if str(s.get("id") or "") not in skip]
    return steps
