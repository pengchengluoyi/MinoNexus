"""解析通用逻辑块 + 应用 override。"""
from __future__ import annotations

from typing import Any

from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID, NavFlowBlockCatalog

# 文档 / catalog 示例里的短 id → 库内 block_id
_BLOCK_META_PREFIX = "mino.block_meta.v1:"


def _pack_block_description(text: str, *, case_ids: list[str] | None = None) -> str:
    ids = [str(x).strip() for x in (case_ids or []) if str(x).strip()]
    if not ids:
        return str(text or "")
    import json

    return _BLOCK_META_PREFIX + json.dumps({"text": str(text or ""), "case_ids": ids}, ensure_ascii=False)


def _unpack_block_description(desc: str) -> tuple[str, list[str]]:
    raw = str(desc or "")
    if not raw.startswith(_BLOCK_META_PREFIX):
        return raw, []
    import json

    try:
        doc = json.loads(raw[len(_BLOCK_META_PREFIX) :])
    except json.JSONDecodeError:
        return raw, []
    if not isinstance(doc, dict):
        return raw, []
    return str(doc.get("text") or ""), [str(x) for x in (doc.get("case_ids") or []) if str(x).strip()]


BLOCK_ID_ALIASES: dict[str, str] = {
    "login_email": "fb.global.login",
    "email_web_login": "fb.global.login",
    "fb.global.login.email_web": "fb.global.login",
    "login": "fb.global.login",
}


def normalize_block_id(block_id: str) -> str:
    bid = str(block_id or "").strip()
    if not bid:
        return ""
    return BLOCK_ID_ALIASES.get(bid, bid)


def load_block_row(*, app_id: str, block_id: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import SessionLocal

    db = SessionLocal()
    try:
        app = str(app_id or "").strip()
        bid = normalize_block_id(block_id)
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
    from mino_nexus.navigation.atlas.nav_key_refs import default_block_key_ref

    bid = str(row.block_id or "")
    ref = str(getattr(row, "key_ref", "") or "").strip() or default_block_key_ref(bid)
    desc_text, case_ids = _unpack_block_description(str(row.description or ""))
    return {
        "app_id": str(row.app_id or ""),
        "block_id": bid,
        "block_origin": str(row.block_origin or ""),
        "display_name": str(row.display_name or ""),
        "description": desc_text,
        "case_ids": case_ids,
        "steps_json": list(row.steps_json or []),
        "version": str(row.version or "v1"),
        "enabled": bool(int(row.enabled or 0)),
        "key_ref": ref,
        "dsl": f"【块:{bid}】" if bid else "",
    }


def _overrides_for_app(app_id: str) -> list[dict[str, Any]]:
    app = str(app_id or "").strip()
    if not app:
        return []
    from mino_nexus.navigation.atlas import nav_calibration_store as calib
    from mino_nexus.navigation.atlas import nav_fsm_store as store

    try:
        doc = calib.read_draft(app) or store.load(app, version="v1") or {}
    except Exception:  # noqa: BLE001 - 覆盖校准不可读不能拖垮块目录
        doc = {}
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    raw = meta.get("flow_block_overrides")
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def list_catalog(*, app_id: str = "", channel: str = "") -> list[dict[str, Any]]:
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
        out = [_row_dict(r) for r in rows]
        ch = str(channel or "").strip().lower()
        if ch:
            out = [r for r in out if _block_matches_channel(r, ch)]
        return out
    finally:
        db.close()


def _block_matches_channel(row: dict[str, Any], channel: str) -> bool:
    bid = str(row.get("block_id") or "")
    if bid.startswith("fb.global.login.") or bid in ("fb.global.login", "fb.global.system_dialog"):
        return True
    if channel == "web":
        return "email_web" in bid or ".web" in bid
    if channel in ("android", "ios"):
        return "email_web" not in bid
    return True


def get_overrides(app_id: str) -> list[dict[str, Any]]:
    return list(_overrides_for_app(app_id))


def save_overrides(app_id: str, overrides: list[dict[str, Any]], *, updated_by: str = "") -> list[dict[str, Any]]:
    app = str(app_id or "").strip()
    if not app:
        return []
    clean = [x for x in overrides if isinstance(x, dict)]
    from mino_nexus.navigation.atlas import nav_calibration_store as calib

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


def upsert_global_block(
    *,
    block_id: str,
    display_name: str = "",
    description: str = "",
    steps_json: list[dict[str, Any]] | None = None,
    version: str = "v1",
    enabled: bool = True,
    key_ref: str = "",
) -> dict[str, Any]:
    raise ValueError("logic_write_stopped")
    from mino_nexus.core.database import SessionLocal

    from mino_nexus.navigation.atlas.nav_key_refs import default_block_key_ref

    bid = str(block_id or "").strip()
    if not bid:
        return {}
    resolved_ref = str(key_ref or "").strip() or default_block_key_ref(bid)
    db = SessionLocal()
    try:
        row = (
            db.query(NavFlowBlockCatalog)
            .filter(
                NavFlowBlockCatalog.app_id == GLOBAL_APP_ID,
                NavFlowBlockCatalog.block_id == bid,
            )
            .first()
        )
        if row is None:
            row = NavFlowBlockCatalog(
                app_id=GLOBAL_APP_ID,
                block_id=bid,
                block_origin="global_catalog",
                display_name=display_name or bid,
                description=description or "",
                steps_json=list(steps_json or []),
                version=version,
                enabled=1 if enabled else 0,
                key_ref=resolved_ref,
            )
            db.add(row)
        else:
            if display_name:
                row.display_name = display_name
            if description:
                row.description = description
            if steps_json is not None:
                row.steps_json = list(steps_json)
            if version:
                row.version = version
            row.enabled = 1 if enabled else 0
            if key_ref or not str(getattr(row, "key_ref", "") or "").strip():
                row.key_ref = resolved_ref
        db.commit()
        db.refresh(row)
        return _row_dict(row)
    finally:
        db.close()


def upsert_app_block(
    *,
    app_id: str,
    block_id: str,
    display_name: str = "",
    description: str = "",
    steps_json: list[dict[str, Any]] | None = None,
    version: str = "v1",
    enabled: bool = True,
    key_ref: str = "",
    block_origin: str = "app",
    case_ids: list[str] | None = None,
) -> dict[str, Any]:
    """App 私有块：从用例拓扑沉淀，不进 global 骨架。"""
    raise ValueError("logic_write_stopped")
    from mino_nexus.core.database import SessionLocal
    from mino_nexus.navigation.atlas.nav_key_refs import default_block_key_ref

    app = str(app_id or "").strip()
    bid = normalize_block_id(block_id)
    if not app or app == GLOBAL_APP_ID or not bid:
        return {}
    resolved_ref = str(key_ref or "").strip() or default_block_key_ref(bid)
    packed_desc = _pack_block_description(description, case_ids=case_ids)
    db = SessionLocal()
    try:
        row = (
            db.query(NavFlowBlockCatalog)
            .filter(NavFlowBlockCatalog.app_id == app, NavFlowBlockCatalog.block_id == bid)
            .first()
        )
        if row is None:
            row = NavFlowBlockCatalog(
                app_id=app, block_id=bid, block_origin=block_origin,
                display_name=display_name or bid, description=packed_desc,
                steps_json=list(steps_json or []), version=version,
                enabled=1 if enabled else 0, key_ref=resolved_ref,
            )
            db.add(row)
        else:
            if display_name:
                row.display_name = display_name
            if description or case_ids:
                row.description = packed_desc
            if steps_json is not None:
                row.steps_json = list(steps_json)
            if version:
                row.version = version
            if block_origin:
                row.block_origin = block_origin
            row.enabled = 1 if enabled else 0
            if key_ref or not str(getattr(row, "key_ref", "") or "").strip():
                row.key_ref = resolved_ref
        db.commit()
        db.refresh(row)
        return _row_dict(row)
    finally:
        db.close()


def delete_app_blocks(app_id: str, *, prefix: str = "fb.app.") -> int:
    """删除某个 App 的私有块（从用例拓扑重新沉淀时用）。"""
    from mino_nexus.core.database import SessionLocal
    from mino_nexus.models.nav_flow_block_catalog import NavFlowBlockCatalog

    app = str(app_id or "").strip()
    if not app:
        return 0
    db = SessionLocal()
    try:
        q = db.query(NavFlowBlockCatalog).filter(
            NavFlowBlockCatalog.app_id == app,
            NavFlowBlockCatalog.block_origin == "app",
        )
        if prefix:
            q = q.filter(NavFlowBlockCatalog.block_id.like(prefix + "%"))
        rows = q.all()
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)
    finally:
        db.close()


def resolve_effective_steps(*, app_id: str, block_id: str) -> list[dict[str, Any]]:
    app = str(app_id or "").strip()
    base = load_block_row(app_id=GLOBAL_APP_ID, block_id=normalize_block_id(block_id))
    if not base and app and app != GLOBAL_APP_ID:
        # App 私有块没有 global 基座，直接读 App 自己的步骤（规划 §5）。
        own = load_block_row(app_id=app, block_id=normalize_block_id(block_id))
        if own and str(own.get("app_id") or "") == app:
            return [dict(s) for s in (own.get("steps_json") or []) if isinstance(s, dict)]
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
