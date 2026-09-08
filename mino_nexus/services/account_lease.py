"""测试账号租用：从项目号池 pick → 写入 RunContext。"""
from __future__ import annotations

from datetime import datetime
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

TAG = "AccountLease"


def _prompt_from_ctx(ctx: Any, params: dict[str, Any], *, ai_reasoning: str = "") -> str:
    direct = str(params.get("tags_prompt") or params.get("prompt") or "").strip()
    if direct:
        return direct
    scene = getattr(ctx, "case_scene", None) or {}
    if isinstance(scene, dict):
        pre = str(scene.get("precondition") or "").strip()
        if pre:
            return pre
    return str(ai_reasoning or "").strip()


def format_accounts_brief(row: dict[str, Any]) -> str:
    if not row:
        return ""
    ident = account_ident(row) or "未填号码"
    tags = [str(t).strip() for t in (row.get("tags") or []) if str(t).strip()]
    tag_text = "、".join(tags[:6]) if tags else "无标签"
    env = str(row.get("env") or "").strip() or "-"
    reason = str(row.get("reason") or "").strip()
    note = str(row.get("note") or "").strip()
    bits = [f"已租测试账号 {ident}", f"环境 {env}", f"标签 {tag_text}"]
    if note:
        bits.append(f"备注 {note[:80]}")
    if reason:
        bits.append(reason)
    return "；".join(bits)


def _pick_row(
    env_doc: dict[str, Any],
    *,
    prompt: str,
    env_profile: str,
    platform: str,
    target_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    surface = resolve_surface_id(
        env_doc,
        platform=platform,
        target_id=target_id,
        prompt=prompt,
        env_profile=env_profile,
    )
    ranked = pick_test_accounts(
        list_test_accounts(env_doc),
        prompt=prompt,
        env=env_profile,
        surface=surface,
        channels=env_doc.get("channels") or [],
        platform=platform,
        target_id=target_id,
        env_doc=env_doc,
    )
    rid = str(run_id or "").strip()
    for row in ranked:
        if bool(row.get("locked")):
            continue
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        other_run = str(lease.get("run_id") or "").strip()
        if other_run and other_run != rid:
            continue
        if int(row.get("score") or 0) <= 0 and len(ranked) > 1:
            continue
        return dict(row)
    return None


def _mark_leased(project_id: str, account_id: str, run_id: str) -> None:
    doc = ps.project_env(project_id)
    rows = list_test_accounts(doc)
    changed = False
    for row in rows:
        if str(row.get("id") or "") != str(account_id):
            continue
        row["lease"] = {
            "run_id": str(run_id or ""),
            "leased_at": datetime.now().isoformat(timespec="seconds"),
        }
        changed = True
        break
    if changed:
        ps.save_project_env(project_id, save_test_accounts(doc, rows))


def _clear_leased(project_id: str, account_id: str, run_id: str) -> None:
    doc = ps.project_env(project_id)
    rows = list_test_accounts(doc)
    changed = False
    for row in rows:
        if str(row.get("id") or "") != str(account_id):
            continue
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        if str(lease.get("run_id") or "") != str(run_id or ""):
            continue
        row["lease"] = {}
        changed = True
        break
    if changed:
        ps.save_project_env(project_id, save_test_accounts(doc, rows))


def apply_lease_to_ctx(ctx: Any, row: dict[str, Any], *, project_id: str) -> None:
    ident = account_ident(row)
    lease_meta = {
        "account_id": str(row.get("id") or ""),
        "project_id": str(project_id or ""),
        "run_id": str(getattr(ctx, "run_id", "") or ""),
        "account_ident": ident,
        "env": str(row.get("env") or ""),
        "tags": list(row.get("tags") or []),
        "score": int(row.get("score") or 0),
        "reason": str(row.get("reason") or ""),
    }
    ctx.resource_lease = lease_meta
    ctx.picked_account = {
        "id": lease_meta["account_id"],
        "ident": ident,
        "phone": str(row.get("phone") or ""),
        "email": str(row.get("email") or ""),
        "username": str(row.get("username") or ""),
        "password": str(row.get("password") or ""),
        "tags": list(row.get("tags") or []),
        "env": lease_meta["env"],
        "label": account_label(row),
    }
    ctx.accounts_brief = format_accounts_brief(row)
    ctx.resource_env = {
        **(getattr(ctx, "resource_env", None) or {}),
        "project_id": project_id,
        "account_id": lease_meta["account_id"],
    }


def lease_for_context(
    ctx: Any,
    params: dict[str, Any] | None,
    *,
    ai_reasoning: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """从 ctx.app_id 对应项目号池租号。成功返回 (row, '')，失败返回 (None, reason)。"""
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

    accounts = list_test_accounts(env_doc)
    if not accounts:
        return None, "号池为空，请先在项目里添加测试账号"

    prompt = _prompt_from_ctx(ctx, params or {}, ai_reasoning=ai_reasoning)
    if not prompt:
        return None, "缺少租号描述（tags_prompt 或用例前置）"

    env_profile = str(getattr(ctx, "env_profile", "") or "").strip()
    platform = str(getattr(ctx, "platform", "") or "android")
    target_id = str(getattr(ctx, "target_package", "") or "")
    run_id = str(getattr(ctx, "run_id", "") or "")

    row = _pick_row(
        env_doc,
        prompt=prompt,
        env_profile=env_profile,
        platform=platform,
        target_id=target_id,
        run_id=run_id,
    )
    if not row:
        return None, f"没有匹配「{prompt[:60]}」的可用账号（可能都被占用或环境不符）"

    account_id = str(row.get("id") or "")
    if account_id and run_id:
        try:
            _mark_leased(project_id, account_id, run_id)
        except Exception as exc:
            SLog.w(TAG, f"mark lease failed: {exc!r}")

    apply_lease_to_ctx(ctx, row, project_id=project_id)
    SLog.i(TAG, f"leased {account_ident(row)} for run={run_id[:12]} score={row.get('score')}")
    return row, ""


def release_ctx_lease(ctx: Any) -> None:
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
