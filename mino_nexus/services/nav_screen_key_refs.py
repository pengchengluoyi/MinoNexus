"""NavFSM 屏幕 key_ref 读写（state.meta.key_ref）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services.case_key_registry import default_screen_key_ref


def list_screen_rows(app_id: str) -> list[dict[str, Any]]:
    from mino_nexus.services import nav_fsm_store as store

    app = str(app_id or "").strip()
    if not app:
        return []
    doc = store.load(app) or {}
    rows: list[dict[str, Any]] = []
    for st in doc.get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "").strip()
        if not sid:
            continue
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        label = str(st.get("label") or st.get("name") or sid).strip()
        ref = str(meta.get("key_ref") or "").strip() or default_screen_key_ref(sid)
        rows.append(
            {
                "state_id": sid,
                "label": label,
                "key_ref": ref,
                "dsl": f"【导航:{sid}】",
                "role": str(st.get("role") or ""),
            }
        )
    rows.sort(key=lambda r: str(r.get("state_id") or ""))
    return rows


def patch_screen_key_refs(
    app_id: str,
    items: list[dict[str, Any]],
    *,
    updated_by: str = "",
) -> list[dict[str, Any]]:
    from mino_nexus.services import nav_fsm_store as store

    app = str(app_id or "").strip()
    if not app or not items:
        return list_screen_rows(app)
    doc = store.read_raw(app) or store.load(app) or {}
    if not isinstance(doc, dict):
        doc = {}
    states = [dict(s) for s in (doc.get("states") or []) if isinstance(s, dict)]
    by_id = {str(it.get("state_id") or "").strip(): it for it in items if isinstance(it, dict)}
    for st in states:
        sid = str(st.get("id") or "").strip()
        patch = by_id.get(sid)
        if not patch:
            continue
        meta = dict(st.get("meta") or {})
        ref = str(patch.get("key_ref") or "").strip()
        if ref:
            meta["key_ref"] = ref
        st["meta"] = meta
    doc["states"] = states
    store.save(app, doc, updated_by=updated_by or "screen_key_ref")
    return list_screen_rows(app)
