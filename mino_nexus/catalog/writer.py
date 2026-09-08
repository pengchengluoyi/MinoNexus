"""写入 catalog_entries。kind 原样落库：prep / do / check / generic / recovery。"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from mino_nexus.catalog.exec_classes import ALL_KINDS, CAPABILITY_KINDS
from mino_nexus.catalog.models import Capability, RecoveryRule


class CatalogWriteError(ValueError):
    pass


def resolve_kind(kind: str) -> str:
    raw = str(kind or "").strip()
    if raw not in ALL_KINDS:
        raise CatalogWriteError(f"不支持写入 kind={kind}")
    return raw


def _target_kind(url_kind: str, body: dict[str, Any]) -> str:
    url_ck = resolve_kind(url_kind)
    raw = str(body.get("kind") or "").strip()
    if not raw or raw == url_ck:
        return url_ck
    return resolve_kind(raw)


def upsert(kind: str, entry_id: str, body: dict[str, Any], *, create_only: bool = False) -> dict[str, Any]:
    from mino_nexus.catalog import registry as catalog
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    url_ck = resolve_kind(kind)
    target_ck = _target_kind(url_ck, body)
    eid = str(entry_id or body.get("id") or "").strip()
    if not eid:
        raise CatalogWriteError("缺少 id")
    fields = _prepare(target_ck, eid, body)
    _validate(target_ck, fields)
    with session_scope() as db:
        row = db.query(CatalogEntry).filter_by(kind=url_ck, id=eid).first()
        if row is None and not create_only:
            row = db.query(CatalogEntry).filter_by(kind=target_ck, id=eid).first()
        if row is None:
            exists = db.query(CatalogEntry).filter_by(kind=target_ck, id=eid).first()
            if exists:
                raise CatalogWriteError(f"{target_ck}/{eid} 已存在")
            db.add(CatalogEntry(**fields))
        elif create_only:
            raise CatalogWriteError(f"{target_ck}/{eid} 已存在")
        else:
            if target_ck != row.kind:
                conflict = (
                    db.query(CatalogEntry)
                    .filter_by(kind=target_ck, id=eid)
                    .filter(CatalogEntry.pk != row.pk)
                    .first()
                )
                if conflict:
                    raise CatalogWriteError(f"{target_ck}/{eid} 已存在")
            for key, val in fields.items():
                setattr(row, key, val)
    catalog.reload()
    errs = [e.message for e in catalog.list_load_errors() if eid in (e.message or "")]
    if errs:
        raise CatalogWriteError(errs[0])
    return {"kind": target_ck, "id": eid}


def patch(kind: str, entry_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.catalog import registry as catalog
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    url_ck = resolve_kind(kind)
    target_ck = _target_kind(url_ck, body)
    eid = str(entry_id or "").strip()
    with session_scope() as db:
        row = db.query(CatalogEntry).filter_by(kind=url_ck, id=eid).first()
        if row is None:
            row = db.query(CatalogEntry).filter_by(kind=target_ck, id=eid).first()
        if row is None:
            raise CatalogWriteError(f"未找到 {url_ck}/{eid}")
        fields = _row_fields(row)
        if target_ck != row.kind:
            conflict = (
                db.query(CatalogEntry)
                .filter_by(kind=target_ck, id=eid)
                .filter(CatalogEntry.pk != row.pk)
                .first()
            )
            if conflict:
                raise CatalogWriteError(f"{target_ck}/{eid} 已存在")
            fields["kind"] = target_ck
        if "enabled" in body and body["enabled"] is not None:
            fields["enabled"] = bool(body["enabled"])
        if body.get("lifecycle"):
            fields["lifecycle"] = str(body["lifecycle"])
        if "display_name" in body and body["display_name"] is not None:
            fields["display_name"] = str(body["display_name"])
        if "description" in body and body["description"] is not None:
            fields["description"] = str(body["description"])
        if "provider" in body and body["provider"] is not None:
            fields["provider"] = str(body["provider"])
        if "owner" in body and body["owner"] is not None:
            fields["owner"] = str(body["owner"])
        if "category" in body and body["category"] is not None:
            fields["category"] = str(body["category"])
        if "sort_order" in body and body["sort_order"] is not None:
            fields["sort_order"] = int(body["sort_order"] or 0)
        if "platforms" in body and body["platforms"] is not None:
            fields["platforms_json"] = list(body["platforms"] or [])
        elif isinstance(body.get("scope"), dict) and "platforms" in (body.get("scope") or {}):
            fields["platforms_json"] = list((body.get("scope") or {}).get("platforms") or [])
        if "visible_to" in body and body["visible_to"] is not None:
            fields["visible_to_json"] = list(body["visible_to"] or [])
        elif isinstance(body.get("scope"), dict) and "visible_to" in (body.get("scope") or {}):
            fields["visible_to_json"] = list((body.get("scope") or {}).get("visible_to") or [])
        if isinstance(body.get("payload"), dict):
            fields["payload_json"] = _clean_payload(dict(body["payload"]))
        _validate(target_ck, fields)
        for key, val in fields.items():
            setattr(row, key, val)
    catalog.reload()
    return {"kind": target_ck, "id": eid}


def soft_delete(kind: str, entry_id: str) -> dict[str, Any]:
    from mino_nexus.catalog import registry as catalog
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    ck = resolve_kind(kind)
    eid = str(entry_id or "").strip()
    with session_scope() as db:
        row = db.query(CatalogEntry).filter_by(kind=ck, id=eid).first()
        if row is None:
            raise CatalogWriteError(f"未找到 {ck}/{eid}")
        row.enabled = False
        row.lifecycle = "deprecated"
    catalog.reload()
    return {"kind": ck, "id": eid, "lifecycle": "deprecated", "enabled": False}


def _row_fields(row) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "id": row.id,
        "display_name": row.display_name or "",
        "description": row.description or "",
        "enabled": bool(row.enabled),
        "lifecycle": row.lifecycle or "active",
        "provider": row.provider or "",
        "owner": row.owner or "",
        "platforms_json": list(row.platforms_json or []),
        "visible_to_json": list(row.visible_to_json or []),
        "category": row.category or "",
        "exec_class": "",
        "sort_order": int(row.sort_order or 0),
        "payload_json": dict(row.payload_json or {}),
    }


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload or {})
    out.pop("trigger_phrases", None)
    impls = out.get("implementations")
    if isinstance(impls, list):
        cleaned = []
        for it in impls:
            if not isinstance(it, dict):
                continue
            row = dict(it)
            row.pop("cost", None)
            row.pop("requires_caps", None)
            cleaned.append(row)
        out["implementations"] = cleaned
    return out


def _prepare(kind: str, eid: str, body: dict[str, Any]) -> dict[str, Any]:
    payload = dict(body.get("payload") or {})
    for key in (
        "event_kind", "needs_vlm", "params", "ui", "implementations",
        "when", "mode", "match", "actions", "verify", "forbid", "prompt_snippet",
        "priority", "max_attempts", "evidence_notes", "caller", "returns",
    ):
        if key in body and key not in payload:
            payload[key] = body[key]
    platforms = body.get("platforms")
    if platforms is None and isinstance(body.get("scope"), dict):
        platforms = (body.get("scope") or {}).get("platforms")
    visible = body.get("visible_to")
    if visible is None and isinstance(body.get("scope"), dict):
        visible = (body.get("scope") or {}).get("visible_to")
    return {
        "kind": kind,
        "id": eid,
        "display_name": str(body.get("display_name") or body.get("title") or eid),
        "description": str(body.get("description") or body.get("when") or ""),
        "enabled": body.get("enabled", True) is not False,
        "lifecycle": str(body.get("lifecycle") or "active"),
        "provider": str(body.get("provider") or ""),
        "owner": str(body.get("owner") or ""),
        "platforms_json": list(platforms or []),
        "visible_to_json": list(visible or []),
        "category": str(body.get("category") or ""),
        "exec_class": "",
        "sort_order": int(body.get("sort_order") or 0),
        "payload_json": _clean_payload(payload),
    }


def _validate(kind: str, fields: dict[str, Any]) -> None:
    payload = dict(fields.get("payload_json") or {})
    eid = fields["id"]
    try:
        if kind in CAPABILITY_KINDS:
            impls = list(payload.get("implementations") or [])
            if not impls:
                return
            Capability(
                id=eid,
                kind=kind,
                display_name=fields.get("display_name") or eid,
                event_kind=str(payload.get("event_kind") or eid),
                category=fields.get("category") or "uncategorized",
                description=fields.get("description") or "",
                platforms=list(fields.get("platforms_json") or []),
                needs_vlm=bool(payload.get("needs_vlm")),
                implementations=impls,
                visible_to=list(fields.get("visible_to_json") or ["case", "system"]),
                params=list(payload.get("params") or []) if isinstance(payload.get("params"), list) else [],
                ui=payload.get("ui") if isinstance(payload.get("ui"), dict) else {},
            )
            return
        if kind == "recovery":
            RecoveryRule(
                id=eid,
                title=fields.get("display_name") or eid,
                enabled=bool(fields.get("enabled", True)),
                provider=fields.get("provider") or "",
                owner=fields.get("owner") or "",
                lifecycle=fields.get("lifecycle") or "active",
                priority=int(payload.get("priority") or 0),
                when=str(payload.get("when") or ""),
                match=payload.get("match") or {},
                mode=str(payload.get("mode") or "advise"),
                actions=list(payload.get("actions") or []),
                verify=payload.get("verify") or {},
                forbid=payload.get("forbid") or {},
                prompt_snippet=str(payload.get("prompt_snippet") or ""),
                max_attempts=int(payload.get("max_attempts") or 1),
                platforms=list(fields.get("platforms_json") or []),
                evidence_notes=list(payload.get("evidence_notes") or []),
            )
            return
    except ValidationError as exc:
        raise CatalogWriteError(str(exc)) from exc
    raise CatalogWriteError(f"不支持写入 kind={kind}")
