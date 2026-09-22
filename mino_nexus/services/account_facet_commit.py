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
    outcome: dict[str, Any] | None = None,
) -> None:
    """每条用例结束（pass/fail/超时）分析轨迹并写回号池 facet。"""
    lease = getattr(ctx, "resource_lease", None) or {}
    pid = str(lease.get("project_id") or "").strip()
    aid = str(lease.get("account_id") or "").strip()
    if not pid or not aid:
        return
    picked = getattr(ctx, "picked_account", None) or {}
    current = account_facet_values_for_match(picked if isinstance(picked, dict) else {})
    meta = (case or {}).get("meta") if isinstance((case or {}).get("meta"), dict) else {}
    author_hints = meta.get("facet_effects") if isinstance(meta.get("facet_effects"), dict) else {}

    from mino_nexus.services.account_facet_ai import (
        collect_probe_for_account,
        infer_facet_updates_from_run,
    )

    effects, ai_reason, llm_ok = infer_facet_updates_from_run(
        project_id=pid,
        case=case,
        status=status,
        outcome=outcome if isinstance(outcome, dict) else {},
        current_facets=current,
        probe_facets=collect_probe_for_account(ctx, aid),
        author_hints=author_hints,
    )
    source = "ai_case_end"
    if not effects and author_hints:
        effects = {str(k): str(v).strip().lower() for k, v in author_hints.items() if str(v).strip()}
        source = "manual"
    if not effects and not llm_ok:
        st = str(status or "").lower()
        if st in ("pass", "done"):
            pre = str(
                (case or {}).get("precondition")
                or (case or {}).get("precondition_raw")
                or ""
            )
            expected = (case or {}).get("expected") or (case or {}).get("expected_raw") or ""
            effects = infer_pass_effects(pre, expected)
            source = "case_pass"
    if not effects:
        if ai_reason:
            SLog.d(TAG, f"no facet write: {ai_reason[:120]}")
        return

    from mino_nexus.services import project_store as ps
    from mino_nexus.services.account_facet_schema import facets_for_storage
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    env_doc = ps.project_env(pid)
    defs = merged_pool_field_defs(env_doc)
    ext_keys = frozenset(
        str(d.get("key") or "")
        for d in defs
        if str(d.get("key") or "")
    )
    updated, errors = apply_facet_updates(
        current, effects, source=source, extension_keys=ext_keys, field_defs=defs
    )
    if errors:
        SLog.w(TAG, f"facet transition blocked: {errors[:3]}")
        if not updated or updated == current:
            return
    stored = facets_for_storage({**current, **updated}, merged_pool_field_defs(env_doc))
    if stored == facets_for_storage(current, merged_pool_field_defs(env_doc)):
        return
    from mino_nexus.services.account_facet_sync import log_context_from_run_ctx

    persist_account_facets(
        pid,
        aid,
        stored,
        log_ctx=log_context_from_run_ctx(ctx, source=source),
    )
    updated = stored
    if isinstance(picked, dict):
        picked["facets"] = updated
        ctx.picked_account = picked
    lease_out = dict(lease)
    lease_out["facets"] = updated
    ctx.resource_lease = lease_out
    SLog.i(TAG, f"facet persisted account={aid[:8]} keys={list(stored.keys())}")
