"""手机号自动开号：按应用×环境的起始位递增（默认 17000000000），租号失败或未注册时写入号池。"""
from __future__ import annotations

import threading
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services.gmail_alias_lease import (
    _norm_alias_slot,
    ensure_ctx_env_surface,
    merge_alias_slots,
    precondition_wants_unregistered,
    requirements_want_unregistered,
)

TAG = "PhonePoolLease"
_DEFAULT_PHONE_START = "17000000000"
_LOCKS: dict[str, threading.Lock] = {}


def _project_lock(project_id: str) -> threading.Lock:
    pid = str(project_id or "").strip()
    if pid not in _LOCKS:
        _LOCKS[pid] = threading.Lock()
    return _LOCKS[pid]


def normalize_channel_phone_seq(
    raw: Any,
    channels: list[dict],
    env_keys: set[str],
    *,
    prev: Any = None,
) -> dict[str, dict[str, dict[str, str]]]:
    src = raw if isinstance(raw, dict) else {}
    prev_map = prev if isinstance(prev, dict) else {}
    ch_ids = {str(c.get("id") or "") for c in channels if c.get("id")}
    out: dict[str, dict[str, dict[str, str]]] = {}
    for cid, per_env in src.items():
        if cid not in ch_ids or not isinstance(per_env, dict):
            continue
        row: dict[str, dict[str, str]] = {}
        prev_per = prev_map.get(cid) if isinstance(prev_map.get(cid), dict) else {}
        for ek, slot in per_env.items():
            if ek not in env_keys:
                continue
            prev_slot = prev_per.get(ek) if isinstance(prev_per, dict) else None
            row[ek] = merge_alias_slots(slot, prev_slot)
        if row:
            out[cid] = row
    return out


def get_channel_phone_seq(
    env_doc: dict | None,
    *,
    env_surface: str,
    env_profile: str,
    apply_phone_default: bool = False,
) -> dict[str, str]:
    doc = env_doc if isinstance(env_doc, dict) else {}
    surface = str(env_surface or "").strip()
    env_key = str(env_profile or "test").strip() or "test"
    ch_map = doc.get("channel_phone_seq")
    slot: dict[str, Any] = {}
    if surface and isinstance(ch_map, dict):
        per = ch_map.get(surface)
        if isinstance(per, dict):
            slot = per.get(env_key) or {}
    out = _norm_alias_slot(slot)
    if out.get("start"):
        return out
    if not apply_phone_default:
        return out
    from mino_nexus.services.otp_resolve import (
        login_kind_from_secrets,
        resolve_effective_secrets,
    )

    secrets = resolve_effective_secrets(doc, env_profile=env_key, env_surface=surface)
    if login_kind_from_secrets(secrets) != "phone":
        return out
    return _norm_alias_slot({"start": _DEFAULT_PHONE_START, "next": _DEFAULT_PHONE_START})


def phone_pool_lease_enabled(env_doc: dict | None, ctx: Any) -> bool:
    from mino_nexus.services.otp_resolve import (
        login_kind_from_secrets,
        resolve_effective_secrets,
    )

    doc = env_doc if isinstance(env_doc, dict) else {}
    env_profile = str(getattr(ctx, "env_profile", "") or "test").strip() or "test"
    surface = ensure_ctx_env_surface(ctx, doc)
    secrets = resolve_effective_secrets(doc, env_profile=env_profile, env_surface=surface)
    if login_kind_from_secrets(secrets) != "phone":
        return False
    slot = get_channel_phone_seq(
        doc,
        env_surface=surface,
        env_profile=env_profile,
        apply_phone_default=True,
    )
    return bool(slot.get("start"))


def should_provision_phone_seq(
    requirements: dict[str, Any],
    *,
    prompt: str = "",
    env_doc: dict | None,
    ctx: Any,
) -> bool:
    if not (
        requirements_want_unregistered(requirements)
        or precondition_wants_unregistered(prompt)
    ):
        return False
    return phone_pool_lease_enabled(env_doc, ctx)


def _channel_stem(env_doc: dict, surface: str) -> str:
    for ch in env_doc.get("channels") or []:
        if not isinstance(ch, dict):
            continue
        if str(ch.get("id") or "") == str(surface or ""):
            stem = str(ch.get("app_identifier") or ch.get("alias") or ch.get("id") or "").strip()
            return stem[:40] or str(surface)
    return str(surface or "")[:40]


def _bump_phone_next(
    project_id: str,
    *,
    surface: str,
    env_profile: str,
) -> tuple[str, dict[str, Any]]:
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.project_env import normalize_project_env

    pid = str(project_id or "").strip()
    doc = normalize_project_env(ps.project_env(pid))
    slot = get_channel_phone_seq(
        doc,
        env_surface=surface,
        env_profile=env_profile,
        apply_phone_default=True,
    )
    start = slot.get("start") or ""
    if not start:
        raise ValueError("phone seq start not configured")
    cur = slot.get("next") or start
    try:
        num_int = int(cur)
    except ValueError:
        num_int = int(start)
    phone_str = str(num_int)
    next_str = str(num_int + 1)
    ch_map = dict(doc.get("channel_phone_seq") or {})
    per = dict(ch_map.get(surface) or {})
    per[env_profile] = {"start": start, "next": next_str}
    ch_map[surface] = per
    doc["channel_phone_seq"] = ch_map
    ps.save_project_env(pid, doc)
    return phone_str, doc


def provision_phone_seq_account(
    ctx: Any,
    *,
    project_id: str,
    env_doc: dict[str, Any],
    requirements: dict[str, Any],
    run_id: str,
) -> dict[str, Any] | None:
    from mino_nexus.services.account_pool_templates import new_account_id
    from mino_nexus.services.project_env import save_one_test_account

    if not phone_pool_lease_enabled(env_doc, ctx):
        return None

    env_profile = str(getattr(ctx, "env_profile", "") or "test").strip() or "test"
    surface = ensure_ctx_env_surface(ctx, env_doc)
    pid = str(project_id or "").strip()
    with _project_lock(pid):
        phone_str, fresh_doc = _bump_phone_next(pid, surface=surface, env_profile=env_profile)
        stem = _channel_stem(fresh_doc, surface)
        display = f"{stem}+{phone_str}"[:80]
        aid = new_account_id()
        facets = {
            "lifecycle": "unregistered",
            "session": "logged_out",
            "health": "available",
        }
        if requirements_want_unregistered(requirements):
            facets["lifecycle"] = "unregistered"
        row = {
            "id": aid,
            "account_id": aid,
            "display_name": display,
            "phone": phone_str,
            "env": env_profile,
            "facets": facets,
            "note": f"phone_seq auto {surface}+{phone_str}",
        }
        saved = save_one_test_account(fresh_doc, row, project_id=pid, account_id=aid)
        SLog.i(TAG, f"provisioned {display} phone={phone_str} project={pid[:8]}")
        from mino_nexus.services.resource_allocation_log import append_allocation_log

        append_allocation_log(
            project_id=pid,
            action="phone_seq_provision",
            message=f"手机号开号 {display}",
            env=env_profile,
            run_id=str(run_id or ""),
            case_id=str(getattr(ctx, "case_id", "") or "")[:80],
            sn=str(getattr(ctx, "sn", "") or "")[:64],
            account_id=aid,
            account_ident=phone_str,
            detail={"surface": surface, "phone": phone_str},
        )
        return saved


def try_provision_after_lease_miss(
    ctx: Any,
    *,
    project_id: str,
    env_doc: dict[str, Any],
    requirements: dict[str, Any],
    run_id: str,
) -> dict[str, Any] | None:
    if not phone_pool_lease_enabled(env_doc, ctx):
        return None
    return provision_phone_seq_account(
        ctx,
        project_id=project_id,
        env_doc=env_doc,
        requirements=requirements,
        run_id=run_id,
    )
