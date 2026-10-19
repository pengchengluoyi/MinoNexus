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


def mark_web_run_devices(
    run_id: str,
    session: str,
    *,
    source: str,
    package: str = "",
    doc: dict[str, Any] | None = None,
) -> None:
    """网页作用域标记只供展示；启动 unknown，结束 logged_out。不改账号 facets。"""
    from mino_nexus.runtime.run_context import is_web_slot
    from mino_nexus.services.session_match import normalize_device_session

    rid = str(run_id or "").strip()
    sess = normalize_device_session(session)
    if sess not in ("unknown", "guest", "logged_out"):
        return
    body = doc if isinstance(doc, dict) else {}
    if not body and rid:
        from mino_nexus.services import run_store

        body = run_store.get(rid) or {}
    pkg = str(package or body.get("package") or "").strip()
    if not pkg:
        return
    platforms = body.get("platforms_by_sn") if isinstance(body.get("platforms_by_sn"), dict) else {}
    sns = [str(x).strip() for x in (body.get("sns") or []) if str(x).strip()]
    head = str(body.get("sn") or "").strip()
    if head and head not in sns:
        sns.insert(0, head)
    task_plat = str(body.get("platform") or "").strip().lower()
    for sn in sns:
        plat = str(platforms.get(sn) or "").strip().lower()
        if not is_web_slot(sn, plat or task_plat):
            continue
        upsert_session(
            sn,
            pkg,
            session=sess,
            clear_binding=True,
            stale=sess == "unknown",
            stale_reason="browser_context_unobserved" if sess == "unknown" else "",
            lease_run_id=rid[:80],
            source=str(source or "")[:32],
        )
        SLog.i(TAG, f"web device session={sess} sn={sn[:8]} source={source}")


def emit_device_logout(
    ctx: Any,
    *,
    package_id: str = "",
    source: str = "logout",
    stale_reason: str = "",
    sync_account: bool = True,
) -> None:
    from mino_nexus.loop.observe.session_persist import mark_session_dirty
    mark_session_dirty(ctx, reason="logout")
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
        # 租约账号不等于此设备实际退出的账号，禁止凭租约推断关联。
        sync_aid = bound if bound and bound == lease_aid else ""
        if pid and sync_aid:
            _sync_account_session(pid, sync_aid, "logged_out", source=source, ctx=ctx)
    SLog.i(TAG, f"device_logout sn={sn[:8]} pkg={pkg} source={source}")


def emit_clear_app_cache(ctx: Any, *, package_id: str = "") -> None:
    from mino_nexus.loop.observe.session_persist import mark_session_dirty
    from mino_nexus.services.resource_transition_engine import record_transition

    mark_session_dirty(ctx, reason="cache_cleared_requires_observation")
    sn, pkg = _ctx_sn(ctx), _ctx_package(ctx, package_id)
    if sn and pkg:
        upsert_session(sn, pkg, session="unknown", stale=True,
            stale_reason="cache_cleared_requires_observation", source="clear_app_cache",
            lease_run_id=str(getattr(ctx, "run_id", "") or "")[:80])
    # No account-facet update: cache removal proves neither logout nor reset
    # of server-side business data. Persist a fresh-observation requirement.
    record_transition(ctx, "clear_app_cache", source="clear_app_cache",
        meta={"package_id": pkg, "cleanup_scope": "cache_only", "session_verified": False})


def emit_inspect_session(ctx: Any, row: dict[str, Any] | None, *, package_id: str = "") -> None:
    if not isinstance(row, dict) or not row.get("ok"):
        return
    from mino_nexus.services.login_evidence import normalize_observation
    row = normalize_observation(row)
    from mino_nexus.services.session_match import normalize_device_session

    session = normalize_device_session(row.get("session") or "")
    sn = _ctx_sn(ctx)
    pkg = _ctx_package(ctx, package_id)
    if not sn or not pkg:
        return
    identity = str(row.get("identity") or "").strip()
    pid, lease_aid = _lease_account(ctx)
    evidence = getattr(ctx, "login_evidence", None) or {}
    from mino_nexus.services.login_evidence import evidence_current
    submitted = (evidence.get("account_binding_source") == "submitted_account"
                 and evidence_current(ctx, evidence))
    identity_matches_lease = identity.lower() == "match" or submitted
    bound = lease_aid if session == "logged_in" and lease_aid and identity_matches_lease else None
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
    identity_hint = ("本次提交账号绑定；UI 身份未知" if submitted
                     else _format_identity_hint(identity, ident_label))
    previously_bound = str((get_session(sn, pkg) or {}).get("bound_account_id") or "")
    upsert_session(
        sn,
        pkg,
        session=session,
        bound_account_id=bound,
        clear_binding=not bool(bound),
        identity_hint=identity_hint,
        stale=session not in {"logged_in", "logged_out", "guest"},
        stale_reason="observation_unknown" if session not in {"logged_in", "logged_out", "guest"} else "",
        lease_run_id=str(getattr(ctx, "run_id", "") or "")[:80],
        source="inspect_session",
    )
    from mino_nexus.services.resource_transition_engine import record_transition

    if session == "logged_in":
        record_transition(
            ctx,
            "inspect_session_logged_in",
            source="inspect_session",
            meta={"session": session, "identity": identity, "package_id": pkg,
                  "account_binding_source": evidence.get("account_binding_source", "none")},
        )
        if lease_aid and identity_matches_lease:
            setattr(ctx, "_login_flow_transition_emitted", True)
    elif session in ("logged_out", "guest"):
        record_transition(
            ctx,
            "inspect_session_logged_out",
            source="inspect_session",
            meta={"session": session, "package_id": pkg},
        )
    if session == "logged_in" and lease_aid and identity_matches_lease:
        _sync_account_session(pid, lease_aid, "logged_in", source="probe", ctx=ctx)
    elif pid and lease_aid and previously_bound == lease_aid and session in ("logged_out", "guest"):
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


def _apply_device_login(ctx: Any, *, package_id: str = "", source: str = "flow_block") -> bool:
    from mino_nexus.services.login_evidence import can_commit
    if not can_commit(ctx) or (package_id and package_id != _ctx_package(ctx)):
        return False
    sn = _ctx_sn(ctx)
    pkg = _ctx_package(ctx, package_id)
    pid, lease_aid = _lease_account(ctx)
    evidence = getattr(ctx, "login_evidence", None) or {}
    submission_bound = evidence.get("account_binding_source") in {"submitted_account", "login_submission"}
    bound_aid = lease_aid if submission_bound or evidence.get("identity") == "match" else ""
    if not sn or not pkg:
        return
    upsert_session(
        sn,
        pkg,
        session="logged_in",
        bound_account_id=bound_aid or None,
        identity_hint=("本次登录提交账号绑定；未核验 UI 身份" if submission_bound
            else _format_identity_hint("match") if bound_aid else ""),
        stale=False,
        stale_reason="",
        lease_run_id=str(getattr(ctx, "run_id", "") or "")[:80],
        source=str(source or "flow_block")[:32],
    )
    if pid and bound_aid:
        _sync_account_session(pid, bound_aid, "logged_in", source=source, ctx=ctx)
    return True


def emit_login_complete(ctx: Any, *, package_id: str = "") -> bool:
    """Commit verified authentication/identity only, never business-context success."""
    from mino_nexus.services.login_evidence import can_commit
    if not can_commit(ctx) or (package_id and package_id != _ctx_package(ctx)):
        return False
    return _apply_device_login(ctx, package_id=package_id, source="flow_block")


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
