"""项目级号池模板：在 Console 全局模板之上追加本项目模板与字段。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services.account_pool_templates import (
    ACCOUNT_BASIC_FACET_KEYS,
    CATEGORIES,
    _apply_extension_addons,
    _base_extensions,
    _builtin_ids,
    _norm_template,
    get_custom_templates_from_settings,
    get_extension_addons_from_settings,
    list_templates_catalog,
    merge_template_catalog,
    normalize_template_extensions,
    project_enabled_templates,
)


def _empty_block() -> dict[str, Any]:
    return {"templates": [], "extension_addons": {}}


def get_project_pool_local(env_doc: dict | None) -> dict[str, Any]:
    doc = env_doc if isinstance(env_doc, dict) else {}
    raw = doc.get("account_pool_local")
    if not isinstance(raw, dict):
        return _empty_block()
    templates = [x for x in (raw.get("templates") or []) if isinstance(x, dict)]
    addons_raw = raw.get("extension_addons")
    addons: dict[str, list[dict]] = {}
    if isinstance(addons_raw, dict):
        for tid, rows in addons_raw.items():
            key = str(tid or "").strip()
            if key and isinstance(rows, list):
                addons[key] = [x for x in rows if isinstance(x, dict)]
    return {"templates": templates, "extension_addons": addons}


def save_project_pool_local(env_doc: dict, block: dict[str, Any] | None) -> dict[str, Any]:
    doc = dict(env_doc or {})
    src = block if isinstance(block, dict) else {}
    seen: set[str] = set(_builtin_ids())
    seen.update(str(t.get("id") or "") for t in get_custom_templates_from_settings())
    local_templates: list[dict] = []
    for row in src.get("templates") or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("id") or "").strip()
        if tid in _builtin_ids():
            continue
        if not tid.startswith("ptpl_"):
            tid = f"ptpl_{tid}" if tid else f"ptpl_{len(local_templates)+1}"
        row = {**row, "id": tid[:48], "builtin": False, "enabled": bool(row.get("enabled", True))}
        n = _norm_template(row, seen)
        if n:
            n["builtin"] = False
            if len(n.get("facet_extensions") or []) < 2:
                n["facet_extensions"] = normalize_template_extensions(
                    (n.get("facet_extensions") or []) + _base_extensions()
                )
            local_templates.append(n)
    addons_out: dict[str, list[dict]] = {}
    for tid, rows in (src.get("extension_addons") or {}).items():
        key = str(tid or "").strip()
        if not key or not isinstance(rows, list):
            continue
        normed = normalize_template_extensions(rows)
        normed = [e for e in normed if str(e.get("key") or "") not in ACCOUNT_BASIC_FACET_KEYS]
        if normed:
            addons_out[key] = normed
    doc["account_pool_local"] = {"templates": local_templates, "extension_addons": addons_out}
    return doc


def list_templates_for_project(env_doc: dict | None) -> list[dict[str, Any]]:
    """全局启用模板 + 项目追加字段 + 项目本地模板。"""
    doc = env_doc if isinstance(env_doc, dict) else {}
    local = get_project_pool_local(doc)
    enabled = set(project_enabled_templates(doc))
    global_rows = merge_template_catalog(
        get_custom_templates_from_settings(),
        get_extension_addons_from_settings(),
    )
    out: list[dict[str, Any]] = []
    for tpl in global_rows:
        tid = str(tpl.get("id") or "")
        if tid not in enabled:
            continue
        row = _apply_extension_addons(dict(tpl), local["extension_addons"].get(tid))
        out.append(row)
    seen = {str(t.get("id") or "") for t in out}
    for row in local["templates"]:
        n = _norm_template(row, seen)
        if n and n.get("enabled", True):
            n["builtin"] = False
            n["project_local"] = True
            out.append(n)
            seen.add(str(n.get("id") or ""))
    return out


def get_template_in_project(template_id: str, env_doc: dict | None = None) -> dict[str, Any] | None:
    tid = str(template_id or "").strip()
    if not tid:
        return None
    if env_doc is not None:
        for row in list_templates_for_project(env_doc):
            if str(row.get("id")) == tid:
                return row
    from mino_nexus.services.account_pool_templates import get_template

    return get_template(tid)


def project_pool_payload(env_doc: dict | None) -> dict[str, Any]:
    doc = env_doc if isinstance(env_doc, dict) else {}
    local = get_project_pool_local(doc)
    catalog = list_templates_catalog()
    return {
        "categories": CATEGORIES,
        "templates": list_templates_for_project(doc),
        "enabled_template_ids": project_enabled_templates(doc),
        "local": local,
        "global_templates": [t for t in catalog.get("templates") or [] if t.get("enabled", True)],
        "starter_extensions": catalog.get("starter_extensions") or _base_extensions(),
        "account_health_field": catalog.get("account_health_field"),
    }
