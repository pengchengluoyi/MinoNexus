"""手机号自动开号：按应用×环境的起始位递增（默认 17000000000），租号失败或未注册时写入号池。"""
from __future__ import annotations

from typing import Any

from mino_nexus.resources.gmail_alias_lease import (
    _norm_alias_slot,
    ensure_ctx_env_surface,
    merge_alias_slots,
    precondition_wants_unregistered,
    requirements_want_unregistered,
)

_DEFAULT_PHONE_START = "17000000000"
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
    from mino_nexus.resources.otp_resolve import (
        login_kind_from_secrets,
        resolve_effective_secrets,
    )

    secrets = resolve_effective_secrets(doc, env_profile=env_key, env_surface=surface)
    if login_kind_from_secrets(secrets) != "phone":
        return out
    return _norm_alias_slot({"start": _DEFAULT_PHONE_START, "next": _DEFAULT_PHONE_START})


def phone_pool_lease_enabled(env_doc: dict | None, ctx: Any) -> bool:
    from mino_nexus.resources.otp_resolve import (
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
    from mino_nexus.resources.accounts.requirement_compile import PROFILE_SHAPE_FACETS

    wants_empty_shape = any(
        isinstance(clause, dict) and str(clause.get("facet") or "") in PROFILE_SHAPE_FACETS
        and str(clause.get("op") or "eq") == "eq" and str(clause.get("value") or "") == "no"
        for clause in requirements.get("all") or []
    )
    wants_new = (requirements_want_unregistered(requirements)
                 or precondition_wants_unregistered(prompt) or wants_empty_shape)
    return (wants_new and phone_pool_lease_enabled(env_doc, ctx)
            and _fresh_requirements_match(requirements, env_doc or {}))


def _channel_stem(env_doc: dict, surface: str) -> str:
    for ch in env_doc.get("channels") or []:
        if not isinstance(ch, dict):
            continue
        if str(ch.get("id") or "") == str(surface or ""):
            stem = str(ch.get("app_identifier") or ch.get("alias") or ch.get("id") or "").strip()
            return stem[:40] or str(surface)
    return str(surface or "")[:40]


def fresh_phone_facets(env_doc: dict[str, Any]) -> dict[str, str]:
    """补池初始值来自 fresh_user 合同；不复制请求中的任意业务事实。"""
    from mino_nexus.resources.accounts.pool_templates import merged_pool_field_defs
    from mino_nexus.resources.accounts.requirement_compile import PROFILE_SHAPE_FACETS
    from mino_nexus.resources.resource_pool import facets_from_profile

    facets = facets_from_profile("fresh_user")
    # 兼容现有形象字段：新用户尚未配置形象；这属于补池初始值，非 UI 核验证据。
    for field in merged_pool_field_defs(env_doc):
        if str(field.get("key") or "") not in PROFILE_SHAPE_FACETS:
            continue
        values = {str(option.get("value") or "") for option in field.get("options") or []
                  if isinstance(option, dict)}
        if "no" in values:
            facets[str(field["key"])] = "no"
    return facets


def _fresh_requirements_match(requirements: dict[str, Any], env_doc: dict[str, Any]) -> bool:
    from mino_nexus.resources.accounts.pool_templates import merged_pool_field_defs
    from mino_nexus.resources.resource_pool import match_requirements

    definitions = {str(field["key"]): field for field in merged_pool_field_defs(env_doc) if field.get("key")}
    return match_requirements(fresh_phone_facets(env_doc), requirements, field_defs=definitions)[0]


def phone_seq_account_id(ctx: Any, *, project_id: str, run_id: str) -> str:
    """同一执行单元重复申请/进程恢复复用一条补池记录；不同设备不共用。"""
    import hashlib
    import json
    from mino_nexus.platform.identity import require_user_id

    owner = require_user_id()
    rid = str(run_id or "").strip()
    sn = str(getattr(ctx, "sn", "") or "").strip()
    if not rid or not sn:
        return ""
    identity = [owner, str(project_id), str(getattr(ctx, "app_id", "") or ""), rid,
                str(getattr(ctx, "case_id", "") or ""), sn,
                str(getattr(ctx, "env_surface", "") or ""),
                str(getattr(ctx, "env_profile", "") or "test")]
    return "acc_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


def provision_phone_seq_account(
    ctx: Any,
    *,
    project_id: str,
    env_doc: dict[str, Any],
    requirements: dict[str, Any],
    run_id: str,
) -> dict[str, Any] | None:
    """原子保留序号、写入新号与审计；重复申请不消耗下一个号码。"""
    import copy
    import time
    from datetime import datetime, timezone
    from sqlalchemy import text
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.pool_account import PoolAccount, PoolAccountFacet
    from mino_nexus.models.project import Project
    from mino_nexus.models.resource_ops import ResourceAllocationLog
    from mino_nexus.resources import project_store as ps
    from mino_nexus.resources.pool_account_store import _account_to_dict, _facets_for_account

    env_profile = str(getattr(ctx, "env_profile", "") or "test").strip() or "test"
    if requirements.get("env") and requirements["env"] != env_profile:
        return None
    if not phone_pool_lease_enabled(env_doc, ctx) or not _fresh_requirements_match(requirements, env_doc):
        return None
    surface = ensure_ctx_env_surface(ctx, env_doc)
    pid = str(project_id or "").strip()
    # 在开启写事务前使用现有项目授权，不能仅凭调用参数选择另一个项目。
    ps.require_project(pid)
    aid = phone_seq_account_id(ctx, project_id=pid, run_id=run_id)
    if not aid:
        return None
    initial_facets = fresh_phone_facets(env_doc)
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        existing = db.get(PoolAccount, aid)
        if existing is not None:
            if existing.project_id != pid or existing.env != env_profile:
                raise ValueError("phone_seq_request_scope_changed")
            return _account_to_dict(existing, _facets_for_account(db, pid, aid))
        project = db.get(Project, pid)
        if project is None:
            raise ValueError("phone_seq_project_missing")
        doc = copy.deepcopy(project.env or {})
        if surface not in {str(c.get("id") or "") for c in doc.get("channels") or []}:
            raise ValueError("phone_seq_channel_missing")
        if env_profile not in {str(e.get("key") or "") for e in doc.get("environments") or []}:
            raise ValueError("phone_seq_environment_missing")
        slot = get_channel_phone_seq(doc, env_surface=surface, env_profile=env_profile)
        start = slot.get("start") or _DEFAULT_PHONE_START
        number = max(int(start), int(slot.get("next") or start))
        # 老号可能来自导入/其他渠道；不能把已存在手机号重新宣称为新用户。
        for _ in range(1000):
            phone = str(number)
            if len(phone) != 11:
                raise ValueError("phone_seq_number_out_of_range")
            collision = db.query(PoolAccount.id).filter_by(project_id=pid, env=env_profile, phone=phone).first()
            if collision is None:
                break
            number += 1
        else:
            raise ValueError("phone_seq_collision_limit")
        channel_map = dict(doc.get("channel_phone_seq") or {})
        per_env = dict(channel_map.get(surface) or {})
        per_env[env_profile] = {"start": start, "next": str(number + 1)}
        channel_map[surface] = per_env
        doc["channel_phone_seq"] = channel_map
        project.env = doc
        now = int(time.time())
        account = PoolAccount(id=aid, project_id=pid, env=env_profile, phone=phone,
            display_name=f"{_channel_stem(doc, surface)}+{phone}"[:80], profile_id="fresh_user",
            note=f"phone_seq auto {surface}+{phone}", lease={}, locked=0,
            created_at=now, updated_at=now)
        db.add(account)
        for key, value in initial_facets.items():
            db.add(PoolAccountFacet(project_id=pid, account_id=aid, facet_key=key, facet_value=value))
        db.add(ResourceAllocationLog(project_id=pid, action="phone_seq_provision",
            message="手机号新账号补池", env=env_profile, run_id=run_id,
            case_id=str(getattr(ctx, "case_id", "") or "")[:80],
            sn=str(getattr(ctx, "sn", "") or "")[:64], account_id=aid, account_ident=phone,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            detail_json={"surface": surface, "profile": "fresh_user", "initial_facets": initial_facets,
                         "business_state_verified": False, "source": "phone_seq_provision.v2"}))
        db.flush()
        return _account_to_dict(account, initial_facets)


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
