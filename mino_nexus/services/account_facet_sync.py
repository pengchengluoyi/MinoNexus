"""号池模板状态（facets + 账号参数字段）写回与资源日志审计。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services import project_store as ps
from mino_nexus.services.account_facet_schema import facets_for_storage
from mino_nexus.services.account_pool_templates import merged_pool_field_defs
from mino_nexus.services.pool_account_store import list_accounts, persist_env_strip_test_accounts_if_needed
from mino_nexus.services.project_env import account_ident, save_one_test_account
from mino_nexus.services.resource_allocation_log import (
    ACTION_FACET_RESTORE,
    ACTION_FACET_UPDATE,
    append_allocation_log,
)
from mino_nexus.services.resource_pool import account_facet_values_for_match, apply_facet_updates

TAG = "FacetSync"
SNAPSHOT_SCHEMA = "account_template_state_v1"

_SCALAR_FIELDS = (
    "env",
    "display_name",
    "phone",
    "email",
    "username",
    "password",
    "otp",
    "note",
)


@dataclass
class FacetLogContext:
    source: str = "manual"
    run_id: str = ""
    case_id: str = ""
    sn: str = ""
    package_id: str = ""
    env: str = ""
    message: str = ""
    skip_log: bool = False
    restored_from_log_id: int | None = None


def log_context_from_run_ctx(ctx: Any, *, source: str, case_id: str = "") -> FacetLogContext:
    lease = getattr(ctx, "resource_lease", None) or {}
    env = str(getattr(ctx, "env_profile", "") or "").strip()
    if not env and isinstance(lease, dict):
        env = str(lease.get("env") or "").strip()
    cid = str(case_id or getattr(ctx, "case_id", "") or "").strip()
    if not cid and isinstance(lease, dict):
        cid = str(lease.get("case_id") or "").strip()
    return FacetLogContext(
        source=str(source or "run")[:32],
        run_id=str(getattr(ctx, "run_id", "") or "")[:80],
        case_id=cid[:80],
        sn=str(getattr(ctx, "sn", "") or "")[:64],
        package_id=str(getattr(ctx, "target_package", "") or "")[:120],
        env=env[:32],
    )


def _scalar_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _SCALAR_FIELDS:
        if key not in row:
            continue
        val = row.get(key)
        if val is None:
            out[key] = ""
        elif key == "password":
            out[key] = str(val)
        else:
            out[key] = str(val).strip()
    return out


def _build_log_detail(
    *,
    before_facets: dict[str, str],
    after_facets: dict[str, str],
    before_fields: dict[str, Any],
    after_fields: dict[str, Any],
    updates: dict[str, str],
    log_ctx: FacetLogContext,
    project_id: str,
    account_id: str,
) -> dict[str, Any]:
    changed_facet_keys = sorted(
        k for k in set(before_facets) | set(after_facets) if before_facets.get(k) != after_facets.get(k)
    )
    changed_field_keys = sorted(
        k for k in set(before_fields) | set(after_fields) if before_fields.get(k) != after_fields.get(k)
    )
    recover_fields = {k: before_fields.get(k, "") for k in set(before_fields) | set(changed_field_keys)}
    return {
        "schema": SNAPSHOT_SCHEMA,
        "source": log_ctx.source,
        "updates": dict(updates),
        "changed_facet_keys": changed_facet_keys,
        "changed_field_keys": changed_field_keys,
        "before": {"facets": dict(before_facets), "fields": dict(before_fields)},
        "after": {"facets": dict(after_facets), "fields": dict(after_fields)},
        "recover": {
            "project_id": project_id,
            "account_id": account_id,
            "facets": dict(before_facets),
            "fields": recover_fields,
        },
        "restored_from_log_id": log_ctx.restored_from_log_id,
    }


def apply_account_template_state(
    project_id: str,
    account_id: str,
    *,
    facet_updates: dict[str, Any] | None = None,
    stored_facets: dict[str, str] | None = None,
    row_patch: dict[str, Any] | None = None,
    log_ctx: FacetLogContext | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """写回号池账号模板状态；有变更时追加 resource_allocation_logs。"""
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    if not pid or not aid:
        return None, ["缺少 project_id 或 account_id"]
    ctx = log_ctx or FacetLogContext()
    try:
        env_doc = ps.project_env(pid)
    except KeyError:
        return None, [f"项目不存在: {pid}"]
    defs = merged_pool_field_defs(env_doc)
    ext_keys = frozenset(str(d.get("key") or "") for d in defs if str(d.get("key") or ""))
    row = next((a for a in list_accounts(pid, env_doc) if str(a.get("id") or "") == aid), None)
    if not row:
        return None, [f"账号不存在: {aid}"]
    before_facets = facets_for_storage(account_facet_values_for_match(row), defs)
    before_fields = _scalar_snapshot(row)
    current = dict(before_facets)
    errors: list[str] = []
    updates_applied: dict[str, str] = {}
    if stored_facets is not None:
        after_facets = facets_for_storage(dict(stored_facets), defs)
    else:
        raw_updates = facet_updates if isinstance(facet_updates, dict) else {}
        if raw_updates:
            updated, errors = apply_facet_updates(
                current,
                raw_updates,
                source=ctx.source,
                extension_keys=ext_keys,
                field_defs=defs,
            )
            updates_applied = {
                str(k): str(v).strip().lower()
                for k, v in raw_updates.items()
                if str(k).strip() and str(v or "").strip()
            }
            merged = {**current, **updated}
            after_facets = facets_for_storage(merged, defs)
        else:
            after_facets = before_facets
    patch = dict(row_patch or {})
    save_payload: dict[str, Any] = {}
    if after_facets != before_facets:
        save_payload["facets"] = after_facets
    for key in _SCALAR_FIELDS:
        if key not in patch:
            continue
        save_payload[key] = patch[key]
    if not save_payload:
        return row, errors
    try:
        saved = save_one_test_account(env_doc, save_payload, project_id=pid, account_id=aid)
    except ValueError as exc:
        return None, [str(exc)]
    persist_env_strip_test_accounts_if_needed(pid, env_doc)
    ps.save_project_env(pid, env_doc)
    after_facets = facets_for_storage(account_facet_values_for_match(saved), defs)
    after_fields = _scalar_snapshot(saved)
    if not ctx.skip_log and (
        after_facets != before_facets or after_fields != before_fields
    ):
        action = ACTION_FACET_RESTORE if ctx.restored_from_log_id else ACTION_FACET_UPDATE
        msg = ctx.message.strip()
        if not msg:
            parts: list[str] = []
            if after_facets != before_facets:
                n = len(
                    {k for k in set(before_facets) | set(after_facets) if before_facets.get(k) != after_facets.get(k)}
                )
                parts.append(f"模板字段 {n} 项")
            if after_fields != before_fields:
                parts.append("参数字段已更新")
            msg = " · ".join(parts) or "模板状态已更新"
        append_allocation_log(
            project_id=pid,
            action=action,
            message=msg[:2000],
            env=str(ctx.env or saved.get("env") or "")[:32],
            run_id=ctx.run_id,
            case_id=ctx.case_id,
            sn=ctx.sn,
            package_id=ctx.package_id,
            account_id=aid,
            account_ident=account_ident(saved),
            detail=_build_log_detail(
                before_facets=before_facets,
                after_facets=after_facets,
                before_fields=before_fields,
                after_fields=after_fields,
                updates=updates_applied,
                log_ctx=ctx,
                project_id=pid,
                account_id=aid,
            ),
        )
    return saved, errors


def persist_account_facets_with_log(
    project_id: str,
    account_id: str,
    facets: dict[str, str],
    *,
    log_ctx: FacetLogContext | None = None,
) -> None:
    apply_account_template_state(
        project_id,
        account_id,
        stored_facets=dict(facets or {}),
        log_ctx=log_ctx,
    )


def restore_account_from_allocation_log(project_id: str, log_id: int) -> tuple[dict[str, Any] | None, str]:
    pid = str(project_id or "").strip()
    lid = int(log_id or 0)
    if not pid or lid <= 0:
        return None, "参数无效"
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.resource_ops import ResourceAllocationLog

    with session_scope() as db:
        row = (
            db.query(ResourceAllocationLog)
            .filter(ResourceAllocationLog.project_id == pid, ResourceAllocationLog.id == lid)
            .one_or_none()
        )
        if row is None:
            return None, "日志不存在"
        detail = row.detail_json if isinstance(row.detail_json, dict) else {}
        recover = detail.get("recover") if isinstance(detail.get("recover"), dict) else {}
        before = detail.get("before") if isinstance(detail.get("before"), dict) else {}
        facets = recover.get("facets") if isinstance(recover.get("facets"), dict) else before.get("facets")
        fields = recover.get("fields") if isinstance(recover.get("fields"), dict) else before.get("fields")
        if not isinstance(facets, dict):
            return None, "该日志不含可恢复的快照"
        aid = str(recover.get("account_id") or row.account_id or "").strip()
        if not aid:
            return None, "缺少 account_id"
        log_ctx = FacetLogContext(
            source="log_restore",
            run_id=str(row.run_id or ""),
            case_id=str(row.case_id or ""),
            sn=str(row.sn or ""),
            package_id=str(row.package_id or ""),
            env=str(row.env or ""),
            message=f"从资源日志 #{lid} 恢复模板状态",
            restored_from_log_id=lid,
        )
    saved, errors = apply_account_template_state(
        pid,
        aid,
        stored_facets={str(k): str(v).strip().lower() for k, v in facets.items() if str(k).strip()},
        row_patch=fields if isinstance(fields, dict) else None,
        log_ctx=log_ctx,
    )
    if not saved:
        return None, errors[0] if errors else "恢复失败"
    if errors:
        SLog.w(TAG, f"restore log={lid} warnings: {errors[:2]}")
    return saved, ""


def resolve_task_project_id(task_id: str) -> tuple[str, str, str]:
    """返回 (project_id, app_id, error)。"""
    from mino_nexus.services import run_store

    tid = str(task_id or "").strip()
    doc = run_store.get(tid)
    if not doc:
        return "", "", f"任务不存在: {tid}"
    app_id = str(doc.get("app_id") or "").strip()
    if not app_id:
        return "", "", "任务缺少 app_id"
    try:
        app = ps.require_app(app_id)
    except KeyError:
        return "", app_id, f"未知应用 {app_id}"
    project_id = str(app.get("project_id") or "").strip()
    if not project_id:
        return "", app_id, "应用未归属项目"
    return project_id, app_id, ""
