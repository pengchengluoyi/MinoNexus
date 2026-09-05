"""扩展包控制台：读 catalog_entries；管理员可写 builtin。"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from mino_nexus.catalog import registry as catalog
from mino_nexus.catalog.exec_classes import ALL_KINDS, KIND_META
from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session, require_packs_writer

router = APIRouter(prefix="/packs", tags=["Packs"])

KINDS = ALL_KINDS
_TAB_LABELS = dict(KIND_META)
_TAB_ORDER = ALL_KINDS
_WRITE_KINDS = ALL_KINDS


class PackWriteBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    root: str = "builtin"
    kind: str = ""
    id: str = ""
    display_name: str = ""
    title: str = ""
    description: str = ""
    enabled: bool = True
    lifecycle: str = "active"
    provider: str = ""
    owner: str = ""
    platforms: list[str] = Field(default_factory=list)
    visible_to: list[str] = Field(default_factory=list)
    category: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    sort_order: int = 0


class PackPatchBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: Optional[bool] = None
    lifecycle: Optional[str] = None
    status: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    payload: Optional[dict[str, Any]] = None


def _status_patch(body: PackPatchBody) -> dict[str, Any]:
    raw = str(body.status or body.lifecycle or "").strip().lower()
    if raw in {"pending", "draft", "review"}:
        return {"lifecycle": "draft", "enabled": True}
    if raw in {"deprecated", "disabled"}:
        return {"lifecycle": "deprecated", "enabled": False}
    if raw in {"active", "enabled"}:
        return {"lifecycle": "active", "enabled": True}
    out: dict[str, Any] = {}
    if body.lifecycle is not None:
        out["lifecycle"] = body.lifecycle
    if body.enabled is not None:
        out["enabled"] = body.enabled
        if body.enabled is False and "lifecycle" not in out:
            out["lifecycle"] = "deprecated"
        if body.enabled is True and "lifecycle" not in out:
            out["lifecycle"] = "active"
    return out


def _find_item(uid: str, kind: str, entry_id: str) -> Optional[dict[str, Any]]:
    lookup = kind if kind in KINDS else ""
    row = next((r for r in _collect(lookup) if r["id"] == entry_id or r["uid"] == uid), None)
    if row is None:
        for k in KINDS:
            row = next((r for r in _collect(k) if r["id"] == entry_id or r["uid"] == uid), None)
            if row:
                break
    return row


def _health() -> dict[str, Any]:
    errs = [{"kind": e.kind, "path": e.path, "message": e.message} for e in catalog.list_load_errors()]
    return {"error_count": len(errs), "errors": errs, "by_kind": {}}


def _human_title(display_name: str, entry_id: str, description: str) -> str:
    name = (display_name or "").strip()
    if name and name != entry_id:
        return name
    line = (description or "").strip().splitlines()[0].strip() if description else ""
    for sep in ("（", "("):
        if sep in line:
            line = line.split(sep, 1)[0].strip()
            break
    return (line[:40] if line else entry_id)


def _ui_status(row: dict[str, Any]) -> str:
    if row.get("overridden_by") or row.get("lifecycle") == "deprecated" or row.get("enabled") is False:
        return "deprecated"
    if row.get("lifecycle") in {"draft", "review", "pending"}:
        return "pending"
    return "active"


def _pack_row_from_entry(row) -> dict[str, Any]:
    payload = dict(row.payload_json or {})
    desc_lines = (row.description or "").strip().splitlines()
    desc0 = desc_lines[0][:120] if desc_lines else ""
    kind = str(row.kind or "")
    enabled = bool(row.enabled)
    lifecycle = str(row.lifecycle or "active")
    platforms = list(row.platforms_json or [])
    visible_to = list(row.visible_to_json or [])
    return {
        "uid": f"builtin/{kind}/{row.id}",
        "kind": kind,
        "id": row.id,
        "title": _human_title(row.display_name or "", row.id, row.description or ""),
        "display_name": row.display_name or "",
        "description": row.description or "",
        "enabled": enabled,
        "lifecycle": lifecycle,
        "status": _ui_status({"enabled": enabled, "lifecycle": lifecycle}),
        "provider": row.provider or "",
        "owner": row.owner or "",
        "category": row.category or "",
        "exec_class": row.exec_class or "",
        "sort_order": int(row.sort_order or 0),
        "platforms": platforms,
        "visible_to": visible_to,
        "scope": {
            "platforms": platforms,
            "app_ids": [],
            "visible_to": visible_to,
        },
        "when": str(payload.get("when") or ""),
        "summary": desc0 or str(payload.get("when") or "")[:120],
        "payload": payload,
    }


def _collect(kind: str) -> list[dict[str, Any]]:
    if not kind:
        return []
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.catalog import CatalogEntry

    ensure_db()
    db = SessionLocal()
    try:
        rows = (
            db.query(CatalogEntry)
            .filter_by(kind=kind)
            .order_by(CatalogEntry.sort_order, CatalogEntry.id)
            .all()
        )
        return [_pack_row_from_entry(r) for r in rows]
    finally:
        db.close()


@router.get("/kinds")
def list_kinds(_sess: dict = Depends(current_session)):
    out = []
    for k in _TAB_ORDER:
        meta = _TAB_LABELS[k]
        rows = _collect(k)
        out.append({
            "kind": k, **meta,
            "count": len(rows),
            "ready": True,
            "writable": True,
            "not_ready_reason": "",
        })
    return ok({"kinds": out, "health": _health()})


@router.get("/health")
def packs_health(_sess: dict = Depends(current_session)):
    return ok(_health())


@router.post("/reload")
def reload_packs(_sess: dict = Depends(current_session)):
    info = catalog.reload()
    return ok({"reload": info, "health": _health()}, msg="已重载")


def _category_keys(row: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    cat = str(row.get("category") or "")
    if cat:
        keys.append(cat)
    payload = row.get("payload") or {}
    mode = str(payload.get("mode") or "")
    if mode == "deterministic":
        keys.append("deterministic")
    elif mode:
        keys.append("advise")
    return keys


@router.get("")
def list_packs(
    kind: str = Query(""),
    q: str = Query(""),
    lifecycle: str = Query(""),
    platform: str = Query(""),
    category: str = Query(""),
    _sess: dict = Depends(current_session),
):
    kinds = [str(kind).strip()] if kind else list(_TAB_ORDER)
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise HTTPException(status_code=400, detail=f"未知 kind: {bad}")
    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for k in kinds:
        rows = _collect(k)
        counts[k] = len(rows)
        items.extend(rows)
    kw = q.strip().lower()
    if kw:
        def _blob(r: dict[str, Any]) -> str:
            payload = r.get("payload") or {}
            return " ".join([
                r.get("id") or "",
                r.get("title") or "",
                r.get("display_name") or "",
                r.get("description") or "",
                r.get("when") or "",
                r.get("summary") or "",
                r.get("category") or "",
                str(payload.get("available_when") or ""),
                " ".join(str(x) for x in (payload.get("provides") or [])),
            ]).lower()
        items = [r for r in items if kw in _blob(r)]
    if lifecycle:
        want = "pending" if lifecycle in {"pending", "draft", "review"} else (
            "deprecated" if lifecycle in {"deprecated", "disabled"} else lifecycle
        )
        items = [r for r in items if _ui_status(r) == want]
    if platform:
        def _plat_ok(r: dict[str, Any]) -> bool:
            plats = ((r.get("scope") or {}).get("platforms") or [])
            if not plats:
                return True
            return platform in plats
        items = [r for r in items if _plat_ok(r)]
    if category:
        items = [r for r in items if category in _category_keys(r)]
    return ok({
        "items": items,
        "total": len(items),
        "counts": counts,
        "not_ready": {},
        "health": _health(),
        "fixture": False,
    })


def _parse_uid(uid: str) -> tuple[str, str, str]:
    parts = [p for p in str(uid or "").split("/") if p]
    if len(parts) < 3:
        raise HTTPException(status_code=400, detail="uid 形如 <root>/<kind>/<id>")
    return parts[0], parts[-2], parts[-1]


def _guard_write(kind: str) -> None:
    kind = str(kind or "").strip()
    if kind not in _WRITE_KINDS:
        raise HTTPException(status_code=400, detail=f"未知 kind: {kind}")


@router.post("")
def create_pack(body: PackWriteBody, sess: dict = Depends(current_session)):
    require_packs_writer(sess)
    kind = str(body.kind or "").strip()
    _guard_write(kind)
    from mino_nexus.catalog.writer import CatalogWriteError, upsert

    try:
        saved = upsert(kind, body.id, body.model_dump(), create_only=True)
    except CatalogWriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok({"item": saved}, msg="已创建")


@router.post("/create")
def create_pack_alias(body: PackWriteBody, sess: dict = Depends(current_session)):
    return create_pack(body, sess)


@router.put("/{uid:path}")
def put_pack(uid: str, body: PackWriteBody, sess: dict = Depends(current_session)):
    require_packs_writer(sess)
    _, kind, entry_id = _parse_uid(uid)
    _guard_write(kind)
    from mino_nexus.catalog.writer import CatalogWriteError, upsert

    payload = body.model_dump()
    if not payload.get("id"):
        payload["id"] = entry_id
    try:
        saved = upsert(kind, entry_id, payload)
    except CatalogWriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok({"item": _find_item(uid, kind, entry_id)}, msg="已保存")


@router.patch("/{uid:path}")
def patch_pack(uid: str, body: PackPatchBody, sess: dict = Depends(current_session)):
    require_packs_writer(sess)
    _, kind, entry_id = _parse_uid(uid)
    _guard_write(kind)
    from mino_nexus.catalog.writer import CatalogWriteError, patch

    payload = {**body.model_dump(exclude_unset=True), **_status_patch(body)}
    try:
        patch(kind, entry_id, payload)
    except CatalogWriteError as exc:
        status = 404 if "未找到" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return ok({"item": _find_item(uid, kind, entry_id)}, msg="已更新")


@router.post("/{uid:path}/lifecycle")
def set_pack_lifecycle(uid: str, body: PackPatchBody, sess: dict = Depends(current_session)):
    return patch_pack(uid, body, sess)


@router.delete("/{uid:path}")
def delete_pack(uid: str, sess: dict = Depends(current_session)):
    require_packs_writer(sess)
    _, kind, entry_id = _parse_uid(uid)
    _guard_write(kind)
    from mino_nexus.catalog.writer import CatalogWriteError, soft_delete

    try:
        saved = soft_delete(kind, entry_id)
    except CatalogWriteError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ok({"item": saved}, msg="已停用")


@router.post("/{uid:path}/dry-run")
def dry_run(uid: str, execute: int = Query(0), _sess: dict = Depends(current_session)):
    if execute:
        raise HTTPException(status_code=400, detail="控制台只做规则预演，不能真的执行。")
    parts = [p for p in uid.split("/") if p]
    entry_id = parts[-1] if parts else uid
    row = next(
        (r for k in KINDS for r in _collect(k) if r["id"] == entry_id or r["uid"] == uid),
        None,
    )
    if row is None:
        return ok({
            "matched": False,
            "match_reasons": ["目录里没有这条"],
            "planned_actions": [],
            "execute": 0,
            "source": "catalog",
        })
    return ok({
        "matched": True,
        "match_reasons": ["能力目录已声明该条目"],
        "planned_actions": [{"capability": row["id"], "blocked_by_forbid": ""}],
        "execute": 0,
        "source": "catalog",
    })


@router.get("/{uid:path}")
def get_pack(uid: str, _sess: dict = Depends(current_session)):
    if not str(uid or "").strip("/"):
        return list_packs(kind="", q="", lifecycle="", platform="", category="", _sess=_sess)
    parts = [p for p in uid.split("/") if p]
    if len(parts) < 3:
        raise HTTPException(status_code=400, detail="uid 形如 <root>/<kind>/<id>")
    kind, entry_id = parts[-2], parts[-1]
    row = _find_item(uid, kind, entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到 {kind}/{entry_id}")
    return ok({"item": row})
