"""能力 / 探测触发的测试资源状态转移。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services.account_facet_schema import facets_for_storage
from mino_nexus.services.account_facet_sync import apply_account_template_state, log_context_from_run_ctx
from mino_nexus.services.device_app_session_store import get_session, upsert_session
from mino_nexus.services.resource_pool import (
    account_facet_values_for_match,
    apply_facet_updates,
)

TAG = "ResourceTransition"


def _format_identity_hint(identity: str, account_ident_str: str = "") -> str:
    iden = str(identity or "").strip().lower()
    ident = str(account_ident_str or "").strip()
    if iden == "match":
        return f"身份一致{(' · ' + ident) if ident else ''}"
    if iden == "mismatch":
        return f"身份不一致{(' · ' + ident) if ident else ''}"
    if ident and iden in ("", "unknown"):
        return f"已绑定 {ident}（未 VLM 核对）"
    if iden in ("", "unknown"):
        return ""
    return str(identity or "")[:500]


def _ctx_sn(ctx: Any) -> str:
    return str(getattr(ctx, "sn", "") or "").strip()


def _ctx_package(ctx: Any, package_id: str = "") -> str:
    p = str(package_id or getattr(ctx, "target_package", "") or "").strip()
    return p


def _lease_account(ctx: Any) -> tuple[str, str]:
    lease = getattr(ctx, "resource_lease", None) or {}
    if not isinstance(lease, dict):
        return "", ""
    pid = str(lease.get("project_id") or "").strip()
    aid = str(lease.get("account_id") or "").strip()
    return pid, aid


def emit_device_logout(
    ctx: Any,
    *,
    package_id: str = "",
    source: str = "logout",
    stale_reason: str = "",
    sync_account: bool = True,
) -> None:
    sn = _ctx_sn(ctx)
    pkg = _ctx_package(ctx, package_id)
    if not sn or not pkg:
        return
    before = get_session(sn, pkg) or {}
    bound = str(before.get("bound_account_id") or "").strip()
    upsert_session(
        sn,
        pkg,
        session="logged_out",
        clear_binding=True,
        stale=bool(stale_reason),
        stale_reason=str(stale_reason or source)[:200],
        lease_run_id=str(getattr(ctx, "run_id", "") or "")[:80],
        source=str(source or "logout")[:32],
    )
    if sync_account:
        pid, lease_aid = _lease_account(ctx)
        sync_aid = bound or lease_aid
        if pid and sync_aid:
            _sync_account_session(pid, sync_aid, "logged_out", source=source, ctx=ctx)
    SLog.i(TAG, f"device_logout sn={sn[:8]} pkg={pkg} source={source}")


def emit_clear_app_cache(ctx: Any, *, package_id: str = "") -> None:
    from mino_nexus.services.resource_transition_engine import fire_transition

    fire_transition(
        ctx,
        "clear_app_cache",
        source="clear_app_cache",
        package_id=package_id,
        meta={"stale_reason": "clear_app_cache"},
    )


def emit_inspect_session(ctx: Any, row: dict[str, Any] | None, *, package_id: str = "") -> None:
    if not isinstance(row, dict) or not row.get("ok"):
        return
    session = str(row.get("session") or "").strip().lower()
    if session not in ("logged_in", "guest", "logged_out", "unknown"):
        return
    sn = _ctx_sn(ctx)
    pkg = _ctx_package(ctx, package_id)
    if not sn or not pkg:
        return
    identity = str(row.get("identity") or "").strip()
    pid, lease_aid = _lease_account(ctx)
    bound = lease_aid if session == "logged_in" and lease_aid else None
    ident_label = ""
    if lease_aid and pid:
        try:
            from mino_nexus.services import project_store as ps
            from mino_nexus.services.project_env import account_ident, list_test_accounts

            doc = ps.project_env(pid)
            acc = next(
                (
                    a
                    for a in list_test_accounts(doc, project_id=pid)
                    if str(a.get("id") or "") == lease_aid
                ),
                None,
            )
            if acc:
                ident_label = account_ident(acc)
        except Exception:
            ident_label = lease_aid[:16]
    identity_hint = _format_identity_hint(identity, ident_label)
    upsert_session(
        sn,
        pkg,
        session=session,
        bound_account_id=bound,
        clear_binding=session in ("logged_out", "guest"),
        identity_hint=identity_hint,
        stale=False,
        stale_reason="",
        lease_run_id=str(getattr(ctx, "run_id", "") or "")[:80],
        source="inspect_session",
    )
    from mino_nexus.services.resource_transition_engine import record_transition

    if session == "logged_in":
        record_transition(
            ctx,
            "inspect_session_logged_in",
            source="inspect_session",
            meta={"session": session, "identity": identity, "package_id": pkg},
        )
        if lease_aid:
            setattr(ctx, "_login_flow_transition_emitted", True)
    elif session in ("logged_out", "guest"):
        record_transition(
            ctx,
            "inspect_session_logged_out",
            source="inspect_session",
            meta={"session": session, "package_id": pkg},
        )
    if session == "logged_in" and lease_aid:
        _sync_account_session(pid, lease_aid, "logged_in", source="probe", ctx=ctx)
    elif pid and lease_aid and session in ("logged_out", "guest"):
        picked = getattr(ctx, "picked_account", None) or {}
        facets = account_facet_values_for_match(picked if isinstance(picked, dict) else {})
        cur = str(facets.get("session") or "").lower()
        if cur == "logged_in":
            _sync_account_session(
                pid,
                lease_aid,
                "logged_out" if session == "logged_out" else "guest",
                source="probe",
                ctx=ctx,
            )


def _apply_device_login(ctx: Any, *, package_id: str = "", source: str = "flow_block") -> None:
    sn = _ctx_sn(ctx)
    pkg = _ctx_package(ctx, package_id)
    pid, lease_aid = _lease_account(ctx)
    if not sn or not pkg:
        return
    upsert_session(
        sn,
        pkg,
        session="logged_in",
        bound_account_id=lease_aid or None,
        stale=False,
        stale_reason="",
        lease_run_id=str(getattr(ctx, "run_id", "") or "")[:80],
        source=str(source or "flow_block")[:32],
    )
    if pid and lease_aid:
        _sync_account_session(pid, lease_aid, "logged_in", source=source, ctx=ctx)


def emit_login_complete(ctx: Any, *, package_id: str = "") -> None:
    _apply_device_login(ctx, package_id=package_id, source="flow_block")


def _sync_account_session(
    project_id: str,
    account_id: str,
    session_val: str,
    *,
    source: str,
    ctx: Any | None = None,
) -> None:
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.account_facet_sync import apply_account_template_state, log_context_from_run_ctx
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    try:
        env_doc = ps.project_env(project_id)
    except KeyError:
        return
    defs = merged_pool_field_defs(env_doc)
    from mino_nexus.services.pool_account_store import list_accounts

    row = next(
        (a for a in list_accounts(project_id, env_doc) if str(a.get("id") or "") == account_id),
        None,
    )
    if not row:
        return
    current = account_facet_values_for_match(row)
    updates = {"session": session_val}
    if session_val == "logged_in":
        updates["lifecycle"] = "registered"
    ext_keys = frozenset(str(d.get("key") or "") for d in defs if str(d.get("key") or ""))
    updated, errors = apply_facet_updates(
        current, updates, source=source, extension_keys=ext_keys, field_defs=defs
    )
    if errors:
        SLog.w(TAG, f"account facet blocked: {errors[:2]}")
        return
    stored = facets_for_storage({**current, **updated}, defs)
    log_ctx = log_context_from_run_ctx(ctx, source=source) if ctx is not None else None
    apply_account_template_state(
        project_id,
        account_id,
        stored_facets=stored,
        log_ctx=log_ctx,
    )
