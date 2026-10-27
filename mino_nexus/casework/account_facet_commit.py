"""用例结束写回 facet 转移；探针观测写入 ctx。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.resources.resource_pool import (
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
) -> dict[str, Any]:
    """每条用例结束（pass/fail/超时）分析轨迹并记录待确认的 facet 候选。

    返回一份摘要（``{"called", "reason", "effects", "source", "candidate", "error"}``）
    由调用方塞进 outcome —— 这条链以前静默失败，库里 ``account_fact_candidates``
    一直是 0 行，无法判断是没跑还是跑了没结果。
    """
    report: dict[str, Any] = {"called": True}
    lease = getattr(ctx, "resource_lease", None) or {}
    pid = str(lease.get("project_id") or "").strip()
    aid = str(lease.get("account_id") or "").strip()
    if not pid or not aid:
        report.update(reason="no_lease")
        return report
    from mino_nexus.resources.accounts.lease import current_lease_is_active
    if not current_lease_is_active(ctx):
        SLog.w(TAG, "facet commit skipped: account lease no longer belongs to this execution")
        report.update(reason="lease_inactive")
        return report
    picked = getattr(ctx, "picked_account", None) or {}
    current = account_facet_values_for_match(picked if isinstance(picked, dict) else {})
    meta = (case or {}).get("meta") if isinstance((case or {}).get("meta"), dict) else {}
    author_hints = meta.get("facet_effects") if isinstance(meta.get("facet_effects"), dict) else {}

    from mino_nexus.ai.account_facet import (
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
    if not effects and author_hints and str(status).lower() in {"pass", "done"}:
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
        report.update(reason="no_effects", detail=str(ai_reason or "")[:160], llm_ok=bool(llm_ok))
        return report
    report.update(effects={str(k): str(v) for k, v in effects.items()}, source=source)

    from mino_nexus.resources import project_store as ps
    from mino_nexus.resources.account_facet_schema import facets_for_storage
    from mino_nexus.resources.accounts.pool_templates import merged_pool_field_defs

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
        report.update(reason="no_change")
        return report
    # Inferences and author hints are candidates, never allocation facts.
    from mino_nexus.resources.account_fact_review import capture
    try:
        candidate = capture(ctx, stored, source=source, expected_before=current)
        if candidate:
            SLog.i(TAG, f"facet candidate recorded account={aid[:8]}")
            report.update(reason="candidate", candidate=True)
    except ValueError as exc:
        SLog.w(TAG, f"facet candidate skipped: {exc}")
        report.update(reason="candidate_rejected", error=str(exc)[:120])
    return report
