"""用例结束写回 facet 转移；探针观测写入 ctx。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services.account_lease import persist_account_facets
from mino_nexus.services.resource_pool import (
    account_facet_values_for_match,
    apply_facet_updates,
    infer_pass_effects,
    observe_session_from_probe,
)

TAG = "FacetCommit"


def record_session_probe(ctx: Any, session_block: str) -> None:
    obs = observe_session_from_probe(session_block)
    if not obs:
        return
    lease = getattr(ctx, "resource_lease", None) or {}
    aid = str(lease.get("account_id") or "").strip()
    if not aid:
        return
    store = getattr(ctx, "account_probe_facets", None)
    if not isinstance(store, dict):
        store = {}
        ctx.account_probe_facets = store
    store[aid] = {**store.get(aid, {}), **obs}


def commit_case_facet_effects(
    ctx: Any,
    case: dict[str, Any] | None,
    *,
    status: str,
) -> None:
    if str(status or "").lower() not in ("pass", "done"):
        return
    lease = getattr(ctx, "resource_lease", None) or {}
    pid = str(lease.get("project_id") or "").strip()
    aid = str(lease.get("account_id") or "").strip()
    if not pid or not aid:
        return
    picked = getattr(ctx, "picked_account", None) or {}
    current = account_facet_values_for_match(picked if isinstance(picked, dict) else {})
    pre = str(
        (case or {}).get("precondition")
        or (case or {}).get("precondition_raw")
        or ""
    )
    expected = str((case or {}).get("expected") or (case or {}).get("expected_raw") or "")
    meta = (case or {}).get("meta") if isinstance((case or {}).get("meta"), dict) else {}
    effects = meta.get("facet_effects") if isinstance(meta.get("facet_effects"), dict) else {}
    if not effects:
        effects = infer_pass_effects(pre, expected)
    if not effects:
        return
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.account_facet_schema import facets_for_storage
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    env_doc = ps.project_env(pid)
    ext_keys = frozenset(
        str(d.get("key") or "")
        for d in merged_pool_field_defs(env_doc)
        if str(d.get("key") or "")
    )
    updated, errors = apply_facet_updates(
        current, effects, source="case_pass", extension_keys=ext_keys
    )
    if errors:
        SLog.w(TAG, f"facet transition blocked: {errors[:3]}")
    stored = facets_for_storage({**current, **updated}, merged_pool_field_defs(env_doc))
    persist_account_facets(pid, aid, stored)
    updated = stored
    if isinstance(picked, dict):
        picked["facets"] = updated
        ctx.picked_account = picked
    lease_out = dict(lease)
    lease_out["facets"] = updated
    ctx.resource_lease = lease_out
