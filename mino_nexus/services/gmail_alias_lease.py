"""Gmail +别名 自动开号：按应用×环境的起始位递增，租号失败或未注册需求时写入号池。"""
from __future__ import annotations

import re
import threading
from typing import Any

_PRE_UNREG = re.compile(r"未注册|新用户")

from mino_nexus.core.log import SLog

TAG = "GmailAliasLease"
_DEFAULT_GMAIL_ALIAS_START = "10000"
_LOCKS: dict[str, threading.Lock] = {}
_DIGITS = re.compile(r"\d+")


def _project_lock(project_id: str) -> threading.Lock:
    pid = str(project_id or "").strip()
    if pid not in _LOCKS:
        _LOCKS[pid] = threading.Lock()
    return _LOCKS[pid]


def _norm_alias_slot(raw: Any) -> dict[str, str]:
    src = raw if isinstance(raw, dict) else {}
    start = _DIGITS.findall(str(src.get("start") or ""))
    start_s = (start[0] if start else "")[:16]
    nxt_parts = _DIGITS.findall(str(src.get("next") or ""))
    next_s = (nxt_parts[0] if nxt_parts else "")[:16]
    if not next_s and start_s:
        next_s = start_s
    return {"start": start_s, "next": next_s}


def merge_alias_slots(
    incoming: Any,
    prev: Any = None,
) -> dict[str, str]:
    """保存时合并：用户可改 start；next 只递增，不因改 start 而低于已发号位。"""
    out = _norm_alias_slot(incoming)
    p = _norm_alias_slot(prev)
    if not out.get("start") and p.get("start"):
        out["start"] = p["start"]
    try:
        n_in = int(out.get("next") or out.get("start") or 0)
        n_prev = int(p.get("next") or p.get("start") or 0)
        if n_prev > n_in:
            out["next"] = str(n_prev)
        elif not out.get("next") and out.get("start"):
            out["next"] = out["start"]
    except ValueError:
        if p.get("next") and not out.get("next"):
            out["next"] = p["next"]
    return out


def normalize_channel_gmail_alias(
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


def get_channel_gmail_alias(
    env_doc: dict | None,
    *,
    env_surface: str,
    env_profile: str,
    apply_email_default: bool = False,
) -> dict[str, str]:
    doc = env_doc if isinstance(env_doc, dict) else {}
    surface = str(env_surface or "").strip()
    env_key = str(env_profile or "test").strip() or "test"
    ch_map = doc.get("channel_gmail_alias")
    slot: dict[str, Any] = {}
    if surface and isinstance(ch_map, dict):
        per = ch_map.get(surface)
        if isinstance(per, dict):
            slot = per.get(env_key) or {}
    out = _norm_alias_slot(slot)
    if out.get("start"):
        return out
    if not apply_email_default:
        return out
    from mino_nexus.services.otp_resolve import (
        login_kind_from_secrets,
        resolve_effective_secrets,
    )

    secrets = resolve_effective_secrets(doc, env_profile=env_key, env_surface=surface)
    if login_kind_from_secrets(secrets) != "email":
        return out
    otp = secrets.get("otp") if isinstance(secrets.get("otp"), dict) else {}
    mode = str(otp.get("mode") or "auto").strip().lower()
    if mode not in ("gmail", "auto"):
        return out
    return _norm_alias_slot({"start": _DEFAULT_GMAIL_ALIAS_START, "next": _DEFAULT_GMAIL_ALIAS_START})


def gmail_plus_address(base_inbox: str, tag: str) -> str:
    """插件收件箱本体 + 数字别名，如 qahi3d@gmail.com + 10003 → qahi3d+10003@gmail.com。"""
    base = str(base_inbox or "").strip().lower()
    tag_s = str(tag or "").strip()
    if not base or not tag_s or "@" not in base:
        return ""
    local, domain = base.rsplit("@", 1)
    local = local.split("+")[0].strip()
    domain = domain.strip()
    if not local or not domain:
        return ""
    return f"{local}+{tag_s}@{domain}"


_TAG_FROM_ALIAS_NOTE = re.compile(r"\+(\d+)\s*$")


def tag_from_gmail_alias_note(note: str) -> str:
    m = _TAG_FROM_ALIAS_NOTE.search(str(note or "").strip())
    return m.group(1) if m else ""


def enrich_gmail_alias_account_row(
    row: dict[str, Any],
    ctx: Any,
    *,
    env_doc: dict | None = None,
) -> dict[str, Any]:
    """号池行 email 必须用插件收件箱 + 别名；纠正误写成「应用名+别名@域」的历史数据。"""
    src = row if isinstance(row, dict) else {}
    note = str(src.get("note") or "")
    if "gmail_alias auto" not in note:
        return src
    tag = tag_from_gmail_alias_note(note)
    if not tag:
        return src
    from mino_nexus.services.otp_resolve import resolve_gmail_inbox

    doc = env_doc if isinstance(env_doc, dict) else {}
    if not doc and ctx is not None:
        pid = str(getattr(ctx, "project_id", "") or "").strip()
        if pid:
            try:
                from mino_nexus.services import project_store as ps
                from mino_nexus.services.project_env import normalize_project_env

                doc = normalize_project_env(ps.project_env(pid))
            except Exception:
                doc = {}
    inbox = resolve_gmail_inbox(doc, ctx)
    email = gmail_plus_address(inbox, tag)
    if not email:
        return src
    out = dict(src)
    out["email"] = email
    out["display_name"] = email[:80]
    return out


def _channel_stem(env_doc: dict, surface: str) -> str:
    for ch in env_doc.get("channels") or []:
        if not isinstance(ch, dict):
            continue
        if str(ch.get("id") or "") == str(surface or ""):
            stem = str(ch.get("app_identifier") or ch.get("alias") or ch.get("id") or "").strip()
            return stem[:40] or str(surface)
    return str(surface or "")[:40]


def precondition_wants_unregistered(prompt: str) -> bool:
    return bool(_PRE_UNREG.search(str(prompt or "")))


def ensure_ctx_env_surface(ctx: Any, env_doc: dict | None) -> str:
    from mino_nexus.services.project_env import resolve_surface_id

    surface = str(getattr(ctx, "env_surface", "") or "").strip()
    if surface:
        return surface
    doc = env_doc if isinstance(env_doc, dict) else {}
    env_profile = str(getattr(ctx, "env_profile", "") or "test").strip() or "test"
    surface = resolve_surface_id(
        doc,
        platform=str(getattr(ctx, "platform", "") or "android"),
        target_id=str(getattr(ctx, "target_package", "") or ""),
        prompt="",
        env_profile=env_profile,
    )
    if surface and ctx is not None:
        setattr(ctx, "env_surface", surface)
    return surface


def should_provision_gmail_alias(
    requirements: dict[str, Any],
    *,
    prompt: str = "",
    env_doc: dict | None,
    ctx: Any,
) -> bool:
    from mino_nexus.services.otp_resolve import plugin_user_id_from_ctx

    if not (
        requirements_want_unregistered(requirements)
        or precondition_wants_unregistered(prompt)
    ):
        return False
    return gmail_alias_lease_enabled(
        env_doc,
        ctx,
        plugin_user_id=plugin_user_id_from_ctx(ctx),
    )


def requirements_want_unregistered(requirements: dict[str, Any]) -> bool:
    if not isinstance(requirements, dict):
        return False
    clauses = list(requirements.get("all") or []) + list(requirements.get("prefer") or [])
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        facet = str(clause.get("facet") or clause.get("key") or "").strip().lower()
        if facet != "lifecycle":
            continue
        val = str(clause.get("value") or "").strip().lower()
        opts = str(clause.get("options") or val).lower()
        if val == "unregistered" or "unregistered" in opts:
            return True
    return False


def gmail_alias_lease_enabled(
    env_doc: dict | None,
    ctx: Any,
    *,
    plugin_user_id: str = "",
) -> bool:
    from mino_nexus.services.otp_resolve import (
        login_kind_from_secrets,
        resolve_effective_secrets,
        resolve_gmail_inbox,
    )

    doc = env_doc if isinstance(env_doc, dict) else {}
    env_profile = str(getattr(ctx, "env_profile", "") or "test").strip() or "test"
    surface = ensure_ctx_env_surface(ctx, doc)
    secrets = resolve_effective_secrets(doc, env_profile=env_profile, env_surface=surface)
    if login_kind_from_secrets(secrets) != "email":
        return False
    otp = secrets.get("otp") if isinstance(secrets.get("otp"), dict) else {}
    mode = str(otp.get("mode") or "auto").strip().lower()
    if mode not in ("gmail", "auto"):
        return False
    inbox = resolve_gmail_inbox(doc, ctx)
    if not inbox or "@" not in inbox:
        return False
    slot = get_channel_gmail_alias(
        doc,
        env_surface=surface,
        env_profile=env_profile,
        apply_email_default=True,
    )
    return bool(slot.get("start"))


def _bump_alias_next(
    project_id: str,
    *,
    surface: str,
    env_profile: str,
) -> tuple[str, dict[str, Any]]:
    """返回 (tag_digits, updated_env_doc)。"""
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.project_env import normalize_project_env

    pid = str(project_id or "").strip()
    doc = normalize_project_env(ps.project_env(pid))
    slot = get_channel_gmail_alias(
        doc,
        env_surface=surface,
        env_profile=env_profile,
        apply_email_default=True,
    )
    start = slot.get("start") or ""
    if not start:
        raise ValueError("gmail alias start not configured")
    cur = slot.get("next") or start
    try:
        tag_int = int(cur)
    except ValueError:
        tag_int = int(start)
    tag_str = str(tag_int)
    next_str = str(tag_int + 1)
    ch_map = dict(doc.get("channel_gmail_alias") or {})
    per = dict(ch_map.get(surface) or {})
    per[env_profile] = {"start": start, "next": next_str}
    ch_map[surface] = per
    doc["channel_gmail_alias"] = ch_map
    ps.save_project_env(pid, doc)
    return tag_str, doc


def provision_gmail_alias_account(
    ctx: Any,
    *,
    project_id: str,
    env_doc: dict[str, Any],
    requirements: dict[str, Any],
    run_id: str,
    plugin_user_id: str = "",
) -> dict[str, Any] | None:
    from mino_nexus.services.account_pool_templates import new_account_id
    from mino_nexus.services.otp_resolve import plugin_user_id_from_ctx, resolve_gmail_inbox
    from mino_nexus.services.project_env import save_one_test_account

    uid = str(plugin_user_id or plugin_user_id_from_ctx(ctx) or "").strip()
    if not gmail_alias_lease_enabled(env_doc, ctx, plugin_user_id=uid):
        return None

    env_profile = str(getattr(ctx, "env_profile", "") or "test").strip() or "test"
    surface = ensure_ctx_env_surface(ctx, env_doc)
    inbox = resolve_gmail_inbox(env_doc, ctx)
    if not inbox:
        return None

    pid = str(project_id or "").strip()
    with _project_lock(pid):
        tag, fresh_doc = _bump_alias_next(pid, surface=surface, env_profile=env_profile)
        email = gmail_plus_address(inbox, tag)
        if not email:
            SLog.w(TAG, f"skip provision: invalid inbox={inbox[:48]!r} tag={tag}")
            return None
        display = email[:80]
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
            "email": email,
            "env": env_profile,
            "facets": facets,
            "note": f"gmail_alias auto {surface}+{tag}",
        }
        saved = save_one_test_account(fresh_doc, row, project_id=pid, account_id=aid)
        SLog.i(
            TAG,
            f"provisioned alias email={email} inbox={inbox[:48]} project={pid[:8]}",
        )
        from mino_nexus.services.resource_allocation_log import append_allocation_log

        append_allocation_log(
            project_id=pid,
            action="gmail_alias_provision",
            message=f"Gmail 别名开号 {display}",
            env=env_profile,
            run_id=str(run_id or ""),
            case_id=str(getattr(ctx, "case_id", "") or "")[:80],
            sn=str(getattr(ctx, "sn", "") or "")[:64],
            account_id=aid,
            account_ident=email,
            detail={"surface": surface, "tag": tag, "inbox": inbox[:64]},
        )
        return saved


def try_provision_after_lease_miss(
    ctx: Any,
    *,
    project_id: str,
    env_doc: dict[str, Any],
    requirements: dict[str, Any],
    run_id: str,
    force_unregistered: bool = False,
) -> dict[str, Any] | None:
    """池内无可用号或未注册硬约束时，尝试 +别名 开号。"""
    uid = ""
    try:
        uid = str(getattr(ctx, "plugin_user_id", "") or getattr(ctx, "user_id", "") or "")
    except Exception:
        uid = ""
    if not gmail_alias_lease_enabled(env_doc, ctx, plugin_user_id=uid):
        return None
    if not force_unregistered and not requirements_want_unregistered(requirements):
        # 仅「租不到」时也允许开新别名（邮箱登录 + 已配起始位）
        pass
    row = provision_gmail_alias_account(
        ctx,
        project_id=project_id,
        env_doc=env_doc,
        requirements=requirements,
        run_id=run_id,
        plugin_user_id=uid,
    )
    return row
