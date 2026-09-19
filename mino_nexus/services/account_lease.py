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


def _lease_unavailable_hint(
    env_doc: dict[str, Any],
    *,
    project_id: str,
    requirements: dict[str, Any],
    run_id: str,
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
        observed_by_account=observed_by_account,
    )
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
) -> None:
    from mino_nexus.services.pool_account_store import set_account_lease

    rec = new_lease_record(
        run_id,
        ttl_sec=_lease_ttl_sec(),
        case_id=str(case_id or "")[:64],
    )
    if not set_account_lease(project_id, account_id, rec):
        SLog.w(TAG, f"mark lease failed project={project_id[:8]} account={account_id[:8]}")


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
        _mark_leased(pid, aid, rid, case_id=str(getattr(ctx, "case_id", "") or "")[:64])
    except Exception as exc:
        SLog.w(TAG, f"renew lease failed: {exc!r}")
        return False
    return True


def apply_lease_to_ctx(ctx: Any, row: dict[str, Any], *, project_id: str) -> None:
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
    for row in list_test_accounts(env_doc, project_id=project_id):
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        if str(lease.get("run_id") or "").strip() != rid:
            continue
        if lease_expired(lease):
            continue
        leased_case = str(lease.get("case_id") or "").strip()
        if want_case and leased_case and leased_case != want_case:
            continue
        apply_lease_to_ctx(ctx, row, project_id=project_id)
        renew_run_lease(ctx)
        return row, ""
    return None, "本 run 无活跃租约"


def persist_account_facets(
    project_id: str,
    account_id: str,
    facets: dict[str, str],
) -> None:
    from mino_nexus.services.project_env import save_one_test_account

    doc = ps.project_env(project_id)
    save_one_test_account(
        doc,
        {"facets": dict(facets)},
        project_id=project_id,
        account_id=account_id,
    )
    ps.save_project_env(project_id, doc)


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

    restored, _rerr = restore_lease_for_run(ctx, run_id)
    if restored and ident_hints and not account_row_matches_hints(restored, ident_hints):
        SLog.i(
            TAG,
            f"run lease ident mismatch run={run_id[:12]} "
            f"account={account_ident(restored)} hints={ident_hints[:2]}; re-pick",
        )
        _clear_leased(project_id, str(restored.get("id") or ""), run_id)
        restored = None
    if restored:
        aid = str(restored.get("id") or "")
        obs = observed_by_account.get(aid) if isinstance(observed_by_account, dict) and aid else obs_for_row
        if row_satisfies_requirements(restored, requirements, env_doc, observed=obs):
            if ident_hints and not account_row_matches_hints(restored, ident_hints):
                SLog.i(
                    TAG,
                    f"restored account fails ident hints account={account_ident(restored)}",
                )
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
    if not accounts:
        return None, "号池为空，请先在项目里添加测试账号"

    env_profile = str(getattr(ctx, "env_profile", "") or "").strip()
    platform = str(getattr(ctx, "platform", "") or "android")
    target_id = str(getattr(ctx, "target_package", "") or "")

    wait_budget = wait_ms if wait_ms is not None else _acquire_wait_ms()
    deadline = time.monotonic() + (wait_budget / 1000.0) if wait_budget > 0 else time.monotonic()

    row: dict[str, Any] | None = None
    while True:
        _purge_expired_leases(project_id)
        row = _pick_row(
            env_doc,
            project_id=project_id,
            requirements=requirements,
            env_profile=env_profile,
            platform=platform,
            target_id=target_id,
            run_id=run_id,
            prompt=prompt,
            observed_by_account=observed_by_account,
        )
        if row:
            break
        if wait_budget <= 0 or time.monotonic() >= deadline:
            break
        sleep_wait(int((deadline - time.monotonic()) * 1000))

    if not row:
        hint = _lease_unavailable_hint(
            env_doc,
            project_id=project_id,
            requirements=requirements,
            run_id=run_id,
        )
        base = "没有满足 Requirement 的可用账号（可能都被占用或状态不符）"
        return None, f"{base}{hint}"

    account_id = str(row.get("id") or "")
    if account_id and run_id:
        try:
            _mark_leased(
                project_id,
                account_id,
                run_id,
                case_id=str(getattr(ctx, "case_id", "") or "")[:64],
            )
        except Exception as exc:
            SLog.w(TAG, f"mark lease failed: {exc!r}")

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
    ctx = SimpleNamespace(
        run_id=rid,
        case_id=cid,
        app_id=str(app_id or "").strip(),
        env_profile=str(env_profile or "test").strip(),
        platform=str(platform or "android").strip(),
        target_package=str(target_package or "").strip(),
    )
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
