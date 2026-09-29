"""测试账号租用：Requirement DSL 选号、run 级租约、TTL、幂等释放。"""
from __future__ import annotations

import os
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services import project_store as ps
from mino_nexus.services.project_env import (
    account_ident,
    account_label,
    list_test_accounts,
    pick_test_accounts,
    resolve_surface_id,
    save_test_accounts,
)
from mino_nexus.services.resource_pool import (
    ACQUIRE_WAIT_MS_DEFAULT,
    DEFAULT_LEASE_TTL_SEC,
    account_facets,
    compile_requirements_from_case_need,
    format_facets_brief,
    lease_expired,
    new_lease_record,
    sleep_wait,
)

TAG = "AccountLease"

# agent 循环 / prep 内租号必须 fail-fast；长等待只在 run 开跑时做一次
INTERACTIVE_ACQUIRE_WAIT_MS = 0


def _acquire_wait_ms() -> int:
    raw = str(os.environ.get("MINO_RESOURCE_ACQUIRE_WAIT_MS") or "").strip()
    if raw.isdigit():
        return max(0, int(raw))
    return ACQUIRE_WAIT_MS_DEFAULT


def _lease_ttl_sec() -> int:
    raw = str(os.environ.get("MINO_RESOURCE_LEASE_TTL_SEC") or "").strip()
    if raw.isdigit():
        return max(60, int(raw))
    return DEFAULT_LEASE_TTL_SEC


def _requirements_from_ctx(
    ctx: Any,
    params: dict[str, Any],
    *,
    need_facets: dict[str, Any] | None,
    ai_reasoning: str = "",
    env_doc: dict | None = None,
) -> dict[str, Any]:
    from mino_nexus.services.account_pool_templates import (
        augment_requirements_with_template,
        merge_need_requirements,
    )

    env_profile = str(getattr(ctx, "env_profile", "") or "").strip()
    param_tid = str(
        params.get("template_id") or params.get("account_template_id") or ""
    ).strip()
    need = dict(need_facets) if isinstance(need_facets, dict) else {}
    if param_tid:
        need["template_id"] = param_tid
    if isinstance(params.get("requirements"), dict):
        need["requirements"] = merge_need_requirements(
            need.get("requirements") if isinstance(need.get("requirements"), dict) else {},
            params.get("requirements"),
        )

    if need.get("need_account") or need.get("template_id") or need.get("prompt") or need.get("session"):
        req = compile_requirements_from_case_need(need, env=env_profile, env_doc=env_doc)
        if param_tid:
            req = augment_requirements_with_template(req, param_tid, env_doc)
        from mino_nexus.services.account_requirement_compile import augment_requirements_from_precondition

        return augment_requirements_from_precondition(
            req, str(need.get("prompt") or ""), env_doc
        )

    pre = str(params.get("precondition") or ai_reasoning or "").strip()
    from mino_nexus.services.resource_pool import compile_requirements_from_text

    req = compile_requirements_from_text(pre, env=env_profile)
    tid = param_tid or str(need.get("template_id") or "").strip()
    if tid:
        req = augment_requirements_with_template(req, tid, env_doc)
    from mino_nexus.services.account_requirement_compile import augment_requirements_from_precondition

    return augment_requirements_from_precondition(req, pre, env_doc)


def format_accounts_brief(row: dict[str, Any]) -> str:
    if not row:
        return ""
    ident = account_ident(row) or "未填号码"
    facets = account_facets(row)
    facet_text = format_facets_brief(facets)
    env = str(row.get("env") or "").strip() or "-"
    reason = str(row.get("reason") or "").strip()
    note = str(row.get("note") or "").strip()
    bits = [f"已租测试账号 {ident}", f"环境 {env}", f"状态 {facet_text}"]
    if note:
        bits.append(f"备注 {note[:80]}")
    if reason:
        bits.append(reason)
    return "；".join(bits)


def _login_kind_for_ctx(ctx: Any, env_doc: dict[str, Any], *, prompt: str = "") -> str:
    from mino_nexus.services.otp_resolve import login_kind_from_secrets, resolve_effective_secrets

    env_profile = str(getattr(ctx, "env_profile", "") or "").strip() or "test"
    surface = str(getattr(ctx, "env_surface", "") or "").strip()
    if not surface:
        surface = resolve_surface_id(
            env_doc,
            platform=str(getattr(ctx, "platform", "") or "android"),
            target_id=str(getattr(ctx, "target_package", "") or ""),
            prompt=str(prompt or "").strip(),
            env_profile=env_profile,
        )
    secrets = resolve_effective_secrets(
        env_doc,
        env_profile=env_profile,
        env_surface=surface,
    )
    return login_kind_from_secrets(secrets)


def _row_matches_ctx_login_kind(
    row: dict[str, Any] | None,
    ctx: Any,
    env_doc: dict[str, Any],
    *,
    prompt: str = "",
) -> bool:
    from mino_nexus.services.otp_resolve import account_row_matches_login_kind

    kind = _login_kind_for_ctx(ctx, env_doc, prompt=prompt)
    return account_row_matches_login_kind(row, kind)


def _lease_unavailable_hint(
    env_doc: dict[str, Any],
    *,
    project_id: str,
    requirements: dict[str, Any],
    run_id: str,
    ctx: Any | None = None,
    prompt: str = "",
) -> str:
    """租号失败时附一句可操作的池内统计（不改变选号逻辑）。"""
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs
    from mino_nexus.services.resource_pool import account_facet_values_for_match, match_requirements

    defs = {
        str(d.get("key") or ""): d
        for d in merged_pool_field_defs(env_doc)
        if str(d.get("key") or "")
    }
    rid = str(run_id or "").strip()
    match_n = 0
    busy = 0
    locked_n = 0
    for row in list_test_accounts(env_doc, project_id=project_id):
        facets = account_facet_values_for_match(row)
        ok, _, _ = match_requirements(facets, requirements, field_defs=defs)
        if not ok:
            continue
        match_n += 1
        if bool(row.get("locked")):
            locked_n += 1
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        other = str(lease.get("run_id") or "").strip()
        if other and other != rid:
            busy += 1
    if match_n == 0:
        return "；号池内 0 个账号满足当前 facet 硬约束（检查形象/环境标注或补号）"
    free = match_n - busy - locked_n
    if free <= 0:
        return f"；满足约束 {match_n} 个，可用 {free}（占用 {busy}，锁定 {locked_n}）"
    if ctx is not None:
        from mino_nexus.services.otp_resolve import account_row_matches_login_kind

        kind = _login_kind_for_ctx(ctx, env_doc, prompt=prompt)
        if kind == "email":
            email_ok = 0
            email_free = 0
            rid = str(run_id or "").strip()
            for row in list_test_accounts(env_doc, project_id=project_id):
                facets = account_facet_values_for_match(row)
                ok, _, _ = match_requirements(facets, requirements, field_defs=defs)
                if not ok or not account_row_matches_login_kind(row, "email"):
                    continue
                email_ok += 1
                lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
                other = str(lease.get("run_id") or "").strip()
                if bool(row.get("locked")) or (other and other != rid):
                    continue
                email_free += 1
            if email_ok == 0:
                return "；环境 login.kind=email，但号池内无填写 email 的账号（勿用手机号账号顶替）"
            if email_free == 0:
                return f"；有 {email_ok} 个邮箱账号满足 facet，但均被占用或锁定"
    return ""


def _purge_expired_leases(project_id: str) -> None:
    from mino_nexus.services.pool_account_store import set_account_lease

    doc = ps.project_env(project_id)
    for row in list_test_accounts(doc, project_id=project_id):
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        if not str(lease.get("run_id") or "").strip():
            continue
        if lease_expired(lease):
            set_account_lease(project_id, str(row.get("id") or ""), {})


def _pick_row(
    env_doc: dict[str, Any],
    *,
    project_id: str = "",
    requirements: dict[str, Any],
    env_profile: str,
    platform: str,
    target_id: str,
    run_id: str,
    holder_sn: str = "",
    prompt: str = "",
    observed_by_account: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    surface = resolve_surface_id(
        env_doc,
        platform=platform,
        target_id=target_id,
        prompt=prompt,
        env_profile=env_profile,
    )
    ranked = pick_test_accounts(
        list_test_accounts(env_doc, project_id=project_id),
        prompt=prompt,
        env=env_profile,
        surface=surface,
        channels=env_doc.get("channels") or [],
        platform=platform,
        target_id=target_id,
        env_doc=env_doc,
        requirements=requirements,
        run_id=run_id,
        holder_sn=holder_sn,
        observed_by_account=observed_by_account,
    )
    from mino_nexus.services.otp_resolve import (
        filter_accounts_for_login_kind,
        login_kind_from_secrets,
        resolve_effective_secrets,
    )

    secrets = resolve_effective_secrets(
        env_doc,
        env_profile=env_profile,
        env_surface=surface,
    )
    kind = login_kind_from_secrets(secrets)
    ranked = filter_accounts_for_login_kind(ranked, kind=kind)
    rid = str(run_id or "").strip()
    for row in ranked:
        if bool(row.get("locked")):
            continue
        score = int(row.get("score") or 0)
        if score < 0:
            continue
        return dict(row)
    return None


def _mark_leased(
    project_id: str,
    account_id: str,
    run_id: str,
    *,
    case_id: str = "",
    sn: str = "",
    env: str = "",
) -> bool:
    from mino_nexus.services.pool_account_store import try_claim_account_lease

    doc = ps.project_env(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    row = next((r for r in rows if str(r.get("id") or "") == account_id), None)
    existing = row.get("lease") if isinstance(row, dict) and isinstance(row.get("lease"), dict) else {}
    prev_run = str(existing.get("run_id") or "").strip()
    prev_case = str(existing.get("case_id") or "").strip()
    cid = str(case_id or "").strip()

    rec = new_lease_record(
        run_id,
        ttl_sec=_lease_ttl_sec(),
        case_id=cid,
        sn=str(sn or "")[:64],
    )
    ok = try_claim_account_lease(project_id, account_id, rec)
    if not ok:
        SLog.w(
            TAG,
            f"claim lease failed project={project_id[:8]} account={account_id[:8]} "
            f"run={run_id[:12]} sn={str(sn or '')[:12]}",
        )
        return False

    from mino_nexus.services.resource_allocation_log import append_allocation_log
    from mino_nexus.services.project_env import account_ident as _ident_fn

    try:
        ident = _ident_fn(row) if row else account_id[:12]
    except Exception:
        ident = account_id[:12]

    same_run = prev_run == str(run_id or "").strip() and prev_run
    should_log = not same_run or (cid and cid != prev_case) or (cid and not prev_case)
    if should_log:
        phase = "run" if not cid else "case"
        append_allocation_log(
            project_id=project_id,
            action="lease_claim",
            message=f"租号成功 {ident}" + (f" ({phase})" if phase == "run" and not cid else ""),
            env=str(env or "")[:32],
            run_id=run_id,
            case_id=cid,
            sn=sn,
            account_id=account_id,
            account_ident=ident,
            detail={"score_reason": "claim", "lease_phase": phase},
        )
    return True


def _clear_leased(project_id: str, account_id: str, run_id: str) -> None:
    """幂等释放：仅当 lease.run_id 匹配时清空。"""
    from mino_nexus.services.pool_account_store import set_account_lease

    doc = ps.project_env(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    row = next((r for r in rows if str(r.get("id") or "") == str(account_id)), None)
    if not row:
        return
    lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
    if str(lease.get("run_id") or "") != str(run_id or ""):
        return
    from mino_nexus.services.resource_allocation_log import append_allocation_log
    from mino_nexus.services.project_env import account_ident as _ident_fn

    rid = str(run_id or "").strip()
    aid = str(account_id or "").strip()
    ident = _ident_fn(row) if row else aid[:12]
    append_allocation_log(
        project_id=project_id,
        action="lease_release",
        message=f"释放租约 {ident}",
        run_id=rid,
        case_id=str(lease.get("case_id") or "")[:80],
        sn=str(lease.get("sn") or "")[:64],
        account_id=aid,
        account_ident=ident,
        detail={"reason": "clear_leased"},
    )
    set_account_lease(project_id, account_id, {})


def renew_run_lease(ctx: Any) -> bool:
    lease = getattr(ctx, "resource_lease", None) or {}
    if not isinstance(lease, dict) or not lease.get("account_id"):
        return False
    pid = str(lease.get("project_id") or "").strip()
    aid = str(lease.get("account_id") or "").strip()
    rid = str(lease.get("run_id") or getattr(ctx, "run_id", "") or "").strip()
    if not pid or not aid or not rid:
        return False
    try:
        _mark_leased(pid, aid, rid, case_id=str(getattr(ctx, "case_id", "") or "")[:64], sn=str(getattr(ctx, "sn", "") or ""), env=str(getattr(ctx, "env_profile", "") or ""))
    except Exception as exc:
        SLog.w(TAG, f"renew lease failed: {exc!r}")
        return False
    return True


def apply_lease_to_ctx(ctx: Any, row: dict[str, Any], *, project_id: str) -> None:
    from mino_nexus.services.gmail_alias_lease import enrich_gmail_alias_account_row

    src = row if isinstance(row, dict) else {}
    row = enrich_gmail_alias_account_row(src, ctx)
    if (
        project_id
        and str(row.get("email") or "").strip() != str(src.get("email") or "").strip()
        and "gmail_alias auto" in str(row.get("note") or "")
    ):
        try:
            from mino_nexus.services import project_store as ps
            from mino_nexus.services.project_env import normalize_project_env, save_one_test_account

            doc = normalize_project_env(ps.project_env(project_id))
            save_one_test_account(doc, row, project_id=project_id, account_id=str(row.get("id") or ""))
        except Exception:
            pass
    ident = account_ident(row)
    facets = account_facets(row)
    lease_meta = {
        "account_id": str(row.get("id") or ""),
        "project_id": str(project_id or ""),
        "run_id": str(getattr(ctx, "run_id", "") or ""),
        "account_ident": ident,
        "env": str(row.get("env") or ""),
        "facets": facets,
        "profile_id": str(row.get("profile_id") or ""),
        "score": int(row.get("score") or 0),
        "reason": str(row.get("reason") or ""),
    }
    ctx.resource_lease = lease_meta
    ctx.picked_account = {
        "id": lease_meta["account_id"],
        "account_id": lease_meta["account_id"],
        "display_name": str(row.get("display_name") or ""),
        # ident：展示用（同 account_ident），登录填表只读 email/phone 字段
        "ident": ident,
        "phone": str(row.get("phone") or ""),
        "email": str(row.get("email") or ""),
        "username": str(row.get("username") or ""),
        "password": str(row.get("password") or ""),
        "otp": str(row.get("otp") or row.get("sms_code") or ""),
        "facets": facets,
        "profile_id": lease_meta["profile_id"],
        "env": lease_meta["env"],
        "label": account_label(row),
    }
    ctx.accounts_brief = format_accounts_brief(row)
    ctx.resource_env = {
        **(getattr(ctx, "resource_env", None) or {}),
        "project_id": project_id,
        "account_id": lease_meta["account_id"],
    }


def row_satisfies_requirements(
    row: dict[str, Any],
    requirements: dict[str, Any],
    env_doc: dict[str, Any],
    *,
    observed: dict[str, str] | None = None,
) -> bool:
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs
    from mino_nexus.services.resource_pool import account_facet_values_for_match, match_requirements

    if not isinstance(row, dict) or not isinstance(requirements, dict):
        return False
    defs = {
        str(d.get("key") or ""): d
        for d in merged_pool_field_defs(env_doc)
        if str(d.get("key") or "")
    }
    facets = account_facet_values_for_match(row)
    ok, score, _ = match_requirements(
        facets,
        requirements,
        observed=observed,
        field_defs=defs,
    )
    return bool(ok and score >= 0)


def restore_lease_for_run(ctx: Any, run_id: str) -> tuple[dict[str, Any] | None, str]:
    """run 内后续用例：从号池找回本 run 已占用的账号。"""
    app_id = str(getattr(ctx, "app_id", "") or "").strip()
    if not app_id:
        return None, "缺少 app_id"
    try:
        app = ps.require_app(app_id)
    except KeyError:
        return None, f"未知应用 {app_id}"
    project_id = str(app.get("project_id") or "").strip()
    if not project_id:
        return None, "无项目"
    _purge_expired_leases(project_id)
    env_doc = ps.project_env(project_id)
    rid = str(run_id or "").strip()
    want_case = str(getattr(ctx, "case_id", "") or "").strip()
    want_sn = str(getattr(ctx, "sn", "") or "").strip()
    for row in list_test_accounts(env_doc, project_id=project_id):
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        if str(lease.get("run_id") or "").strip() != rid:
            continue
        if lease_expired(lease):
            continue
        leased_case = str(lease.get("case_id") or "").strip()
        if want_case and leased_case and leased_case != want_case:
            continue
        lease_sn = str(lease.get("sn") or "").strip()
        if want_sn and lease_sn and lease_sn != want_sn:
            continue
        apply_lease_to_ctx(ctx, row, project_id=project_id)
        renew_run_lease(ctx)
        return row, ""
    return None, "本 run 无活跃租约"


def persist_account_facets(
    project_id: str,
    account_id: str,
    facets: dict[str, str],
    *,
    log_ctx: Any = None,
) -> None:
    from mino_nexus.services.account_facet_sync import (
        FacetLogContext,
        persist_account_facets_with_log,
    )

    ctx = log_ctx if isinstance(log_ctx, FacetLogContext) else None
    persist_account_facets_with_log(project_id, account_id, facets, log_ctx=ctx)


def lease_for_context(
    ctx: Any,
    params: dict[str, Any] | None,
    *,
    ai_reasoning: str = "",
    need_facets: dict[str, Any] | None = None,
    observed_by_account: dict[str, dict[str, str]] | None = None,
    wait_ms: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """从 ctx.app_id 对应项目号池租号。成功返回 (row, '')，失败返回 (None, reason)。"""
    run_id = str(getattr(ctx, "run_id", "") or "").strip()

    app_id = str(getattr(ctx, "app_id", "") or "").strip()
    if not app_id:
        return None, "缺少 app_id，无法定位号池"

    try:
        app = ps.require_app(app_id)
    except KeyError:
        return None, f"未知应用 {app_id}"

    project_id = str(app.get("project_id") or "").strip()
    if not project_id:
        return None, "应用未归属项目，号池不可用"

    try:
        env_doc = ps.project_env(project_id)
    except KeyError:
        return None, f"项目 {project_id} 不存在"

    requirements = _requirements_from_ctx(
        ctx,
        params or {},
        need_facets=need_facets,
        ai_reasoning=ai_reasoning,
        env_doc=env_doc,
    )
    from mino_nexus.services.resource_pool import relax_pool_session_for_device_login

    requirements = relax_pool_session_for_device_login(requirements)
    if not (requirements.get("all") or requirements.get("prefer")):
        return None, "缺少租号需求（用例前置 / requirements）"

    prompt = str((params or {}).get("precondition") or ai_reasoning or "").strip()
    from mino_nexus.services.account_ident_parse import (
        account_row_matches_hints,
        extract_account_ident_hints,
    )

    ident_hints = extract_account_ident_hints(prompt)

    obs_for_row: dict[str, str] | None = None
    if isinstance(observed_by_account, dict) and observed_by_account:
        obs_for_row = next(iter(observed_by_account.values()), None)

    from mino_nexus.services.gmail_alias_lease import (
        ensure_ctx_env_surface,
        gmail_alias_lease_enabled,
        provision_gmail_alias_account,
        should_provision_gmail_alias,
        try_provision_after_lease_miss as try_provision_gmail_after_miss,
    )
    from mino_nexus.services.phone_pool_lease import (
        phone_pool_lease_enabled,
        provision_phone_seq_account,
        should_provision_phone_seq,
        try_provision_after_lease_miss as try_provision_phone_after_miss,
    )

    ensure_ctx_env_surface(ctx, env_doc)
    env_profile = str(getattr(ctx, "env_profile", "") or "").strip()
    platform = str(getattr(ctx, "platform", "") or "android")
    target_id = str(getattr(ctx, "target_package", "") or "")
    holder_sn = str(getattr(ctx, "sn", "") or "").strip()

    if should_provision_gmail_alias(requirements, prompt=prompt, env_doc=env_doc, ctx=ctx):
        alias_row = provision_gmail_alias_account(
            ctx,
            project_id=project_id,
            env_doc=env_doc,
            requirements=requirements,
            run_id=run_id,
        )
        if alias_row:
            account_id = str(alias_row.get("id") or "")
            if account_id and run_id and _mark_leased(
                project_id,
                account_id,
                run_id,
                case_id=str(getattr(ctx, "case_id", "") or "")[:64],
                sn=holder_sn,
                env=env_profile,
            ):
                apply_lease_to_ctx(ctx, alias_row, project_id=project_id)
                SLog.i(
                    TAG,
                    f"leased gmail alias (fresh) {account_ident(alias_row)} run={run_id[:12]}",
                )
                return alias_row, ""
    if should_provision_phone_seq(requirements, prompt=prompt, env_doc=env_doc, ctx=ctx):
        phone_row = provision_phone_seq_account(
            ctx,
            project_id=project_id,
            env_doc=env_doc,
            requirements=requirements,
            run_id=run_id,
        )
        if phone_row:
            account_id = str(phone_row.get("id") or "")
            if account_id and run_id and _mark_leased(
                project_id,
                account_id,
                run_id,
                case_id=str(getattr(ctx, "case_id", "") or "")[:64],
                sn=holder_sn,
                env=env_profile,
            ):
                apply_lease_to_ctx(ctx, phone_row, project_id=project_id)
                SLog.i(
                    TAG,
                    f"leased phone seq (fresh) {account_ident(phone_row)} run={run_id[:12]}",
                )
                return phone_row, ""

    restored, _rerr = restore_lease_for_run(ctx, run_id)
    if restored and ident_hints and not account_row_matches_hints(restored, ident_hints):
        SLog.i(
            TAG,
            f"run lease ident mismatch run={run_id[:12]} "
            f"account={account_ident(restored)} hints={ident_hints[:2]}; re-pick",
        )
        _clear_leased(project_id, str(restored.get("id") or ""), run_id)
        restored = None
    if restored and not _row_matches_ctx_login_kind(restored, ctx, env_doc, prompt=prompt):
        SLog.i(
            TAG,
            f"run lease login-kind mismatch run={run_id[:12]} "
            f"account={account_ident(restored)} kind={_login_kind_for_ctx(ctx, env_doc, prompt=prompt)}; re-pick",
        )
        _clear_leased(project_id, str(restored.get("id") or ""), run_id)
        release_ctx_lease(ctx)
        restored = None
    if restored:
        aid = str(restored.get("id") or "")
        if should_provision_gmail_alias(
            requirements, prompt=prompt, env_doc=env_doc, ctx=ctx
        ) and "gmail_alias auto" not in str(restored.get("note") or ""):
            _clear_leased(project_id, aid, run_id)
            release_ctx_lease(ctx)
            restored = None
        elif should_provision_phone_seq(
            requirements, prompt=prompt, env_doc=env_doc, ctx=ctx
        ) and "phone_seq auto" not in str(restored.get("note") or ""):
            _clear_leased(project_id, aid, run_id)
            release_ctx_lease(ctx)
            restored = None
        else:
            obs = observed_by_account.get(aid) if isinstance(observed_by_account, dict) and aid else obs_for_row
            if row_satisfies_requirements(restored, requirements, env_doc, observed=obs):
                if ident_hints and not account_row_matches_hints(restored, ident_hints):
                    SLog.i(
                        TAG,
                        f"restored account fails ident hints account={account_ident(restored)}",
                    )
                    _clear_leased(project_id, aid, run_id)
                    release_ctx_lease(ctx)
                elif not _row_matches_ctx_login_kind(restored, ctx, env_doc, prompt=prompt):
                    _clear_leased(project_id, aid, run_id)
                    release_ctx_lease(ctx)
                else:
                    return restored, ""
            else:
                SLog.i(
                    TAG,
                    f"run lease mismatch run={run_id[:12]} account={account_ident(restored)}; re-pick for case",
                )
                release_ctx_lease(ctx)

    accounts = list_test_accounts(env_doc, project_id=project_id)
    if not accounts and not gmail_alias_lease_enabled(env_doc, ctx) and not phone_pool_lease_enabled(
        env_doc, ctx
    ):
        return None, "号池为空，请先在项目里添加测试账号"

    wait_budget = wait_ms if wait_ms is not None else _acquire_wait_ms()
    deadline = time.monotonic() + (wait_budget / 1000.0) if wait_budget > 0 else time.monotonic()

    row: dict[str, Any] | None = None
    while True:
        _purge_expired_leases(project_id)
        candidate = _pick_row(
            env_doc,
            project_id=project_id,
            requirements=requirements,
            env_profile=env_profile,
            platform=platform,
            target_id=target_id,
            run_id=run_id,
            holder_sn=holder_sn,
            prompt=prompt,
            observed_by_account=observed_by_account,
        )
        if candidate:
            account_id = str(candidate.get("id") or "")
            if account_id and run_id:
                claimed = _mark_leased(
                    project_id,
                    account_id,
                    run_id,
                    case_id=str(getattr(ctx, "case_id", "") or "")[:64],
                    sn=holder_sn,
                    env=env_profile,
                )
                if claimed:
                    row = candidate
                    break
            else:
                row = candidate
                break
        if wait_budget <= 0 or time.monotonic() >= deadline:
            break
        sleep_wait(int((deadline - time.monotonic()) * 1000))

    if not row:
        alias_row = try_provision_gmail_after_miss(
            ctx,
            project_id=project_id,
            env_doc=env_doc,
            requirements=requirements,
            run_id=run_id,
        )
        if not alias_row:
            alias_row = try_provision_phone_after_miss(
                ctx,
                project_id=project_id,
                env_doc=env_doc,
                requirements=requirements,
                run_id=run_id,
            )
        if alias_row:
            account_id = str(alias_row.get("id") or "")
            if account_id and run_id and _mark_leased(
                project_id,
                account_id,
                run_id,
                case_id=str(getattr(ctx, "case_id", "") or "")[:64],
                sn=holder_sn,
                env=env_profile,
            ):
                apply_lease_to_ctx(ctx, alias_row, project_id=project_id)
                SLog.i(
                    TAG,
                    f"leased gmail alias after miss {account_ident(alias_row)} run={run_id[:12]}",
                )
                return alias_row, ""
        hint = _lease_unavailable_hint(
            env_doc,
            project_id=project_id,
            requirements=requirements,
            run_id=run_id,
            ctx=ctx,
            prompt=prompt,
        )
        base = "没有满足 Requirement 的可用账号（可能都被占用或状态不符）"
        from mino_nexus.services.resource_allocation_log import append_allocation_log

        append_allocation_log(
            project_id=project_id,
            action="lease_fail",
            message=f"{base}{hint}"[:500],
            env=env_profile,
            run_id=run_id,
            case_id=str(getattr(ctx, "case_id", "") or ""),
            sn=holder_sn,
            package_id=target_id,
            detail={"requirements": requirements},
        )
        return None, f"{base}{hint}"

    apply_lease_to_ctx(ctx, row, project_id=project_id)
    SLog.i(TAG, f"leased {account_ident(row)} for run={run_id[:12]} score={row.get('score')}")
    return row, ""


def ensure_case_account_lease(
    run_id: str,
    *,
    app_id: str,
    env_profile: str = "test",
    platform: str = "android",
    target_package: str = "",
    case: dict[str, Any] | None = None,
    scene: dict[str, Any] | None = None,
    plugin_user_id: str = "",
) -> tuple[bool, str]:
    """按当前用例前置租号；与同 run 已有租约冲突时释放并重新选号。"""
    from mino_nexus.loop.session_ensure import account_need_from_case

    from mino_nexus.runtime.session_gate import ensure_case_scene

    from mino_nexus.services import project_store as ps

    project_id = ""
    app_key = str(app_id or "").strip()
    if app_key:
        try:
            app_row = ps.require_app(app_key)
            project_id = str(app_row.get("project_id") or "").strip()
        except KeyError:
            project_id = ""
    env_doc = ps.project_env(project_id) if project_id else None
    merged_scene = ensure_case_scene(
        case or {},
        scene if isinstance(scene, dict) else None,
        env_doc=env_doc,
        target_package=str(target_package or ""),
        env_profile=str(env_profile or "test"),
    )
    if isinstance(case, dict):
        case["case_scene"] = merged_scene
    need = account_need_from_case(case, merged_scene)
    if not need.get("need_account"):
        return True, ""
    rid = str(run_id or "").strip()
    cid = str((case or {}).get("case_id") or "")[:24]
    device_sn = str((case or {}).get("sn") or "").strip()
    from mino_nexus.services.gmail_alias_lease import ensure_ctx_env_surface

    ctx = SimpleNamespace(
        run_id=rid,
        case_id=cid,
        sn=device_sn,
        app_id=str(app_id or "").strip(),
        env_profile=str(env_profile or "test").strip(),
        platform=str(platform or "android").strip(),
        target_package=str(target_package or "").strip(),
        plugin_user_id=str(plugin_user_id or "").strip(),
    )
    if env_doc:
        ensure_ctx_env_surface(ctx, env_doc)
    prompt = str(need.get("prompt") or "").strip()
    row, err = lease_for_context(
        ctx,
        {"precondition": prompt} if prompt else {},
        need_facets=need,
        wait_ms=min(_acquire_wait_ms(), 15_000),
    )
    if row:
        SLog.i(
            TAG,
            f"case lease ok run={rid[:12]} case={cid} account={account_ident(row)}",
        )
        return True, ""
    reason = err or "租号失败"
    SLog.w(TAG, f"case lease failed run={rid[:12]} case={cid}: {reason}")
    return False, reason


def ensure_run_account_lease(
    run_id: str,
    *,
    app_id: str,
    env_profile: str = "test",
    platform: str = "android",
    target_package: str = "",
    case: dict[str, Any] | None = None,
    scene: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """兼容旧调用：等同 ensure_case_account_lease。"""
    return ensure_case_account_lease(
        run_id,
        app_id=app_id,
        env_profile=env_profile,
        platform=platform,
        target_package=target_package,
        case=case,
        scene=scene,
    )


def release_ctx_lease(ctx: Any) -> None:
    """幂等释放当前 ctx 上的租约。"""
    lease = getattr(ctx, "resource_lease", None) or {}
    if not isinstance(lease, dict) or not lease.get("account_id"):
        return
    pid = str(lease.get("project_id") or "").strip()
    aid = str(lease.get("account_id") or "").strip()
    rid = str(lease.get("run_id") or getattr(ctx, "run_id", "") or "").strip()
    if not pid or not aid:
        return
    try:
        _clear_leased(pid, aid, rid)
    except Exception as exc:
        SLog.w(TAG, f"release lease failed: {exc!r}")
    ctx.resource_lease = {}
    ctx.picked_account = {}
    ctx.accounts_brief = ""


def release_run_lease(run_id: str, *, app_id: str = "") -> None:
    """run 结束：按 run_id 清空号池占用（兜底，幂等）。"""
    from mino_nexus.services.pool_account_store import clear_leases_for_run

    rid = str(run_id or "").strip()
    if not rid:
        return
    project_ids: list[str] = []
    if app_id:
        try:
            app = ps.require_app(app_id)
            pid = str(app.get("project_id") or "").strip()
            if pid:
                project_ids.append(pid)
        except KeyError:
            pass
    if not project_ids:
        for proj in ps.list_projects():
            pid = str(proj.get("id") or "")
            if pid:
                project_ids.append(pid)
    seen: set[str] = set()
    total = 0
    for pid in project_ids:
        if pid in seen:
            continue
        seen.add(pid)
        try:
            n = clear_leases_for_run(pid, rid)
            if n:
                total += n
                SLog.i(TAG, f"released {n} lease(s) run={rid[:12]} project={pid[:8]}")
        except Exception as exc:
            SLog.w(TAG, f"release_run_lease project={pid[:8]}: {exc!r}")
