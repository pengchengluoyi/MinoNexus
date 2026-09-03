"""扩展包控制台：只读能力目录 YAML。写入与真机试跑尚未搬迁。"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from mino_nexus.catalog import registry as catalog
from mino_nexus.catalog.exec_classes import CAP_CLASS, EXEC_KINDS, KIND_META
from mino_nexus.http_util import ok
from mino_nexus.routers.deps import current_session

router = APIRouter(prefix="/packs", tags=["Packs"])

PACK_KINDS = ("recovery", "knowledge", "oracle")
KINDS = EXEC_KINDS + PACK_KINDS + ("capability",)
_NOT_READY = {
    "oracle": "判定类目尚未落地",
}
_TAB_LABELS = {
    **KIND_META,
    "recovery": {"label": "恢复", "desc": "系统/设备异常怎么处置"},
    "knowledge": {"label": "知识", "desc": "这个应用的业务判据"},
    "oracle": {"label": "判定", "desc": "能不能测、怎么判、多严"},
}
_TAB_ORDER = EXEC_KINDS + PACK_KINDS


def _health() -> dict[str, Any]:
    errs = [{"kind": e.kind, "path": e.path, "message": e.message} for e in catalog.list_load_errors()]
    return {"error_count": len(errs), "errors": errs, "by_kind": {}}


def _cap_row(cap) -> dict[str, Any]:
    impls = [
        {
            "id": i.id,
            "executor": i.executor,
            "requires_caps": list(i.requires_caps or []),
            "cost": getattr(i, "cost", 5),
            "has_low_level": bool(getattr(i, "low_level", None)),
        }
        for i in (cap.implementations or [])
    ]
    exec_class = CAP_CLASS.get(cap.id, "generic")
    desc = (cap.description or "").strip().splitlines()
    return {
        "uid": f"builtin/capability/{cap.id}",
        "kind": exec_class,
        "id": cap.id,
        "title": cap.display_name or cap.id,
        "enabled": True,
        "lifecycle": "active",
        "provider": getattr(cap, "provider", "") or "platform",
        "owner": getattr(cap, "owner", "") or "@platform",
        "root": "builtin",
        "scope": {
            "platforms": list(cap.platforms or []),
            "app_ids": [],
            "visible_to": list(getattr(cap, "visible_to", []) or ["case", "system"]),
        },
        "when": "",
        "summary": desc[0][:120] if desc else "",
        "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
        "source_path": str(getattr(cap, "source_path", "") or ""),
        "detail": {
            "category": cap.category,
            "event_kind": cap.event_kind,
            "needs_vlm": cap.needs_vlm,
            "trigger_phrases": list(cap.trigger_phrases or []),
            "implementations": impls,
            "pure_declarative": False,
            "has_python_branch": False,
            "origin": "yaml",
            "exec_class": exec_class,
        },
    }


def _learned_knowledge_dir():
    from mino_nexus.paths import data_dir

    return data_dir() / "packs" / "learned" / "knowledge" / "entries"


def _knowledge_row(raw: dict[str, Any], source_path: str = "") -> dict[str, Any]:
    kid = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or kid or "未命名")
    content = str(raw.get("content") or "").strip()
    review = str(raw.get("review_status") or "approved").strip().lower()
    lifecycle = "active" if review == "approved" else ("draft" if review == "pending" else "deprecated")
    desc = content.splitlines()
    return {
        "uid": f"learned/knowledge/{kid}",
        "kind": "knowledge",
        "id": kid,
        "title": title,
        "enabled": raw.get("enabled", True) is not False,
        "lifecycle": lifecycle,
        "provider": "learned",
        "owner": "@learned",
        "root": "learned",
        "scope": {
            "platforms": [],
            "app_ids": [str(a) for a in (raw.get("app_ids") or []) if str(a).strip()],
            "visible_to": ["case"],
        },
        "when": str(raw.get("category") or "").strip(),
        "summary": (desc[0] if desc else title)[:120],
        "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
        "source_path": source_path,
        "detail": {
            "origin": "yaml" if source_path else "settings",
            "category": raw.get("category") or "",
            "tags": list(raw.get("tags") or []),
            "review_status": review,
        },
    }


def _list_learned_knowledge() -> list[dict[str, Any]]:
    """只读：~/.mino-nexus/packs/learned/knowledge，没有则回落到 settings.json 已导入的知识。"""
    by_id: dict[str, dict[str, Any]] = {}
    folder = _learned_knowledge_dir()
    if folder.is_dir():
        try:
            import yaml  # type: ignore
        except ImportError:
            yaml = None
        if yaml is not None:
            for path in sorted(folder.glob("*.yaml")) + sorted(folder.glob("*.yml")):
                try:
                    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                kid = str(raw.get("id") or path.stem).strip()
                if not kid:
                    continue
                raw["id"] = kid
                by_id[kid] = _knowledge_row(raw, str(path))
    if not by_id:
        from mino_nexus import settings_store as ss

        for item in ss.list_testing_knowledge():
            kid = str(item.get("id") or "").strip()
            if kid:
                by_id[kid] = _knowledge_row(item)
    return list(by_id.values())


def _recovery_row(rule) -> dict[str, Any]:
    return {
        "uid": f"builtin/recovery/{rule.id}",
        "kind": "recovery",
        "id": rule.id,
        "title": getattr(rule, "title", "") or rule.id,
        "enabled": bool(getattr(rule, "enabled", True)),
        "lifecycle": getattr(rule, "lifecycle", "") or "active",
        "provider": getattr(rule, "provider", "") or "platform",
        "owner": getattr(rule, "owner", "") or "@platform",
        "root": "builtin",
        "scope": {
            "platforms": list(getattr(rule, "platforms", []) or []),
            "app_ids": [],
            "visible_to": ["system"],
        },
        "when": getattr(rule, "when", "") or "",
        "summary": (getattr(rule, "summary", "") or getattr(rule, "title", "") or "")[:120],
        "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
        "source_path": str(getattr(rule, "source_path", "") or ""),
        "detail": {"origin": "yaml"},
    }


def _collect(kind: str) -> list[dict[str, Any]]:
    if kind == "recovery":
        return [_recovery_row(r) for r in catalog.list_recovery_rules(enabled_only=False)]
    if kind == "knowledge":
        return _list_learned_knowledge()
    if kind in _NOT_READY:
        return []
    caps = catalog.list_capabilities()
    if kind == "capability":
        return [_cap_row(c) for c in caps]
    return [_cap_row(c) for c in caps if CAP_CLASS.get(c.id, "generic") == kind]


@router.get("/kinds")
def list_kinds(_sess: dict = Depends(current_session)):
    out = []
    for k in _TAB_ORDER:
        meta = _TAB_LABELS[k]
        rows = _collect(k)
        out.append({
            "kind": k, **meta,
            "count": len(rows),
            "ready": k not in _NOT_READY,
            "not_ready_reason": _NOT_READY.get(k, ""),
        })
    return ok({"kinds": out, "health": _health()})


@router.get("/health")
def packs_health(_sess: dict = Depends(current_session)):
    return ok(_health())


@router.post("/reload")
def reload_packs(_sess: dict = Depends(current_session)):
    info = catalog.reload()
    return ok({"reload": info, "health": _health()}, msg="已重载")


@router.get("/roots")
def list_roots(_sess: dict = Depends(current_session)):
    from mino_nexus.catalog.loader import find_plugin_root
    from mino_nexus.paths import data_dir

    builtin_n = len(catalog.list_capabilities()) + len(catalog.list_recovery_rules(enabled_only=False))
    learned_rows = _list_learned_knowledge()
    learned_dir = data_dir() / "packs" / "learned"
    roots = [
        {"root": "app", "rank": 0, "label": "应用私有", "desc": "尚未开放写入", "writable": False, "count": 0, "path": str(data_dir() / "packs" / "apps")},
        {"root": "team", "rank": 1, "label": "团队共享", "desc": "尚未开放写入", "writable": False, "count": 0, "path": str(data_dir() / "packs" / "team")},
        {"root": "builtin", "rank": 2, "label": "仓库内置", "desc": "随 Nexus 版本发布，只读", "writable": False, "count": builtin_n, "path": str(find_plugin_root())},
        {"root": "learned", "rank": 3, "label": "自动学习", "desc": "只读副本，不提供写 YAML", "writable": False, "count": len(learned_rows), "path": str(learned_dir)},
    ]
    return ok({"roots": roots, "precedence": "app > team > builtin > learned"})


@router.get("")
def list_packs(
    kind: str = Query(""),
    q: str = Query(""),
    provider: str = Query(""),
    lifecycle: str = Query(""),
    root: str = Query(""),
    _sess: dict = Depends(current_session),
):
    kinds = [kind] if kind else list(_TAB_ORDER)
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise HTTPException(status_code=400, detail=f"未知 kind: {bad}")
    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    not_ready: dict[str, str] = {}
    for k in kinds:
        rows = _collect(k)
        counts[k] = len(rows)
        if not rows and k in _NOT_READY:
            not_ready[k] = _NOT_READY[k]
        items.extend(rows)
    kw = q.strip().lower()
    if kw:
        items = [r for r in items if kw in " ".join([
            r["id"], r.get("title", ""), r.get("when", ""), r.get("summary", ""),
        ]).lower()]
    if provider:
        items = [r for r in items if r.get("provider") == provider]
    if lifecycle:
        items = [r for r in items if r.get("lifecycle") == lifecycle]
    if root:
        items = [r for r in items if r.get("root") == root]
    return ok({
        "items": items,
        "total": len(items),
        "counts": counts,
        "not_ready": not_ready,
        "health": _health(),
        "fixture": False,
    })


@router.post("/{uid:path}/dry-run")
def dry_run(uid: str, execute: int = Query(0), _sess: dict = Depends(current_session)):
    if execute:
        raise HTTPException(status_code=400, detail="控制台只做规则预演，不能真的执行。")
    parts = [p for p in uid.split("/") if p]
    entry_id = parts[-1] if parts else uid
    row = next((r for r in _collect("capability") + _collect("recovery") if r["id"] == entry_id or r["uid"] == uid), None)
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
def get_pack(uid: str, with_yaml: int = Query(1), _sess: dict = Depends(current_session)):
    if not str(uid or "").strip("/"):
        return list_packs(kind="", q="", provider="", lifecycle="", root="", _sess=_sess)
    parts = [p for p in uid.split("/") if p]
    if len(parts) < 3:
        raise HTTPException(status_code=400, detail="uid 形如 <root>/<kind>/<id>")
    kind, entry_id = parts[-2], parts[-1]
    lookup = kind if kind in KINDS else "capability"
    row: Optional[dict[str, Any]] = next((r for r in _collect(lookup) if r["id"] == entry_id), None)
    if row is None and kind == "capability":
        row = next((r for r in _collect("capability") if r["id"] == entry_id), None)
    if row is None:
        # builtin/capability/tap_element 的 kind 段是 capability，条目实际按 exec_class 分 Tab
        for k in KINDS:
            row = next((r for r in _collect(k) if r["id"] == entry_id), None)
            if row:
                break
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到 {kind}/{entry_id}")
    if with_yaml and row.get("source_path"):
        try:
            with open(row["source_path"], "r", encoding="utf-8") as f:
                row["raw_yaml"] = f.read()[:20000]
        except OSError as exc:
            row["raw_yaml"] = ""
            row["raw_yaml_error"] = str(exc)
    return ok({"item": row})
