"""测试资源安全读模型 + MCP 预约/释放。

这个模块只服务「测试资源」模块的对外读模型：
- 账号列表/详情/预约返回明文账号、密码、OTP、手机号/邮箱；
- 设备台账去掉 session_key/session_generation 等会话凭据；
- 环境只返回 secret 槽位名，不返回值；
- 日志的 account_ident/detail/自由文本统一掩码；
- 取码按账号固定码、环境固定码、Gmail、人工顺序解析。

MCP 预约账号走号池已有的 lease 语义：一旦 claim，账号在资源视图里就是
occupied（即使 lease 过期也仍占用，直到显式释放），与 run 租号一致。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mino_nexus.core.redact import is_secret_key, mask_ident, redact, scrub_text, text
from mino_nexus.resources import project_store as ps
from mino_nexus.resources.accounts.lease import _clear_leased, _mark_leased, _lease_ttl_sec
from mino_nexus.resources.device_app_session_catalog import list_project_device_app_matrix
from mino_nexus.resources.project_env import (
    account_ident,
    list_test_accounts,
    public_test_accounts,
)
from mino_nexus.resources.resource_allocation_log import list_allocation_logs
from mino_nexus.resources.resource_pool import account_facets, lease_expired, lease_display
from mino_nexus.resources.ui_devices import ui_devices

MAX_PAGE = 200
DEFAULT_PAGE = 50


def _env_doc(project_id: str) -> dict[str, Any]:
    return ps.project_env(str(project_id or "").strip())


def _lease(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("lease") if isinstance(row, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _account_state(row: dict[str, Any]) -> str:
    if bool(row.get("locked")):
        return "locked"
    if str(_lease(row).get("run_id") or "").strip():
        return "occupied"
    return "free"


def _expires_at(lease: dict[str, Any]) -> str:
    return str(lease.get("expires_at") or "").strip()


def _lease_remaining_sec(lease: dict[str, Any]) -> int | None:
    raw = str(lease.get("expires_at") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _public_account(row: dict[str, Any], env_doc: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.resources.accounts.pool_templates import merged_pool_field_defs

    defs = merged_pool_field_defs(env_doc)
    # 账号接口按调用方要求返回可直接登录的明文账号/密码；不再做标识或密码掩码。
    base = public_test_accounts(
        [row], include_password=True, pool_field_defs=defs
    )[0]
    ident = str(account_ident(row) or "")
    display = str(base.get("display_name") or ident)
    lease = _lease(row)
    lease_pub = {
        "run_id": text(lease.get("run_id"), 80),
        "case_id": text(lease.get("case_id"), 80),
        "sn": text(lease.get("sn"), 80),
        "expires_at": _expires_at(lease),
        "status": lease_display(lease).get("status", "free"),
        "expired": bool(lease_expired(lease)) if lease.get("run_id") else False,
    }
    base.pop("password_masked", None)
    base.pop("tags", None)
    facets = base.get("facets") if isinstance(base.get("facets"), dict) else account_facets(row)
    out = {
        "account_id": text(base.get("account_id") or base.get("id"), 80),
        "display_name": text(display, 80),
        "env": text(base.get("env"), 32),
        "kind": text(base.get("kind"), 24),
        "ident": text(ident, 160),
        "username": text(base.get("username"), 160),
        "phone": text(base.get("phone"), 40),
        "email": text(base.get("email"), 160),
        "password": text(base.get("password"), 300),
        "otp": text(base.get("otp"), 40),
        "lifecycle": text(facets.get("lifecycle"), 24),
        "facets": redact(facets),
        "facets_brief": scrub_text(base.get("facets_brief"), 300),
        "state": _account_state(row),
        "locked": bool(row.get("locked")),
        "lease": lease_pub,
        "lease_remaining_sec": _lease_remaining_sec(lease),
        "has_password": bool(base.get("has_password")),
        "has_otp": bool(base.get("has_otp")),
        "note": text(base.get("note"), 300),
        "profile_id": text(base.get("profile_id"), 40),
    }
    return out


def _filter_accounts(
    items: list[dict[str, Any]],
    *,
    env: str = "",
    state: str = "",
    lifecycle: str = "",
    q: str = "",
    raw_idents: dict[str, str] | None = None,
    raw_displays: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    env_q = str(env or "").strip().lower()
    state_q = str(state or "").strip().lower()
    if state_q in {"leased", "busy"}:
        state_q = "occupied"
    lifecycle_q = str(lifecycle or "").strip().lower()
    needle = str(q or "").strip().lower()
    out = []
    for item in items:
        if env_q and str(item.get("env") or "").lower() != env_q:
            continue
        if state_q and str(item.get("state") or "") != state_q:
            continue
        if lifecycle_q and str(item.get("lifecycle") or "").lower() != lifecycle_q:
            continue
        if needle:
            hay = " ".join(
                str(item.get(key) or "")
                for key in ("account_id", "display_name", "ident", "username", "phone", "email", "note")
            ).lower()
            key = str(item.get("account_id") or "")
            hay += " " + str((raw_idents or {}).get(key) or "").lower()
            hay += " " + str((raw_displays or {}).get(key) or "").lower()
            if needle not in hay:
                continue
        out.append(item)
    return out


def accounts(
    project_id: str,
    *,
    env: str = "",
    state: str = "",
    lifecycle: str = "",
    q: str = "",
    limit: int = DEFAULT_PAGE,
    offset: int = 0,
) -> dict[str, Any]:
    doc = _env_doc(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    raw_idents = {
        str(row.get("id") or row.get("account_id") or ""): str(account_ident(row) or "")
        for row in rows
    }
    raw_displays = {
        str(row.get("id") or row.get("account_id") or ""): str(row.get("display_name") or "")
        for row in rows
    }
    items = [_public_account(row, doc) for row in rows]
    items = _filter_accounts(
        items, env=env, state=state, lifecycle=lifecycle, q=q,
        raw_idents=raw_idents, raw_displays=raw_displays,
    )
    # 保持号池原顺序，只把 occupied 稳定置前，便于先看到占用。
    items.sort(key=lambda item: item.get("state") != "occupied")
    total = len(items)
    lim = max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))
    off = max(0, int(offset or 0))
    page = items[off: off + lim]
    return {
        "items": page,
        "total": total,
        "limit": lim,
        "next_offset": off + lim if off + lim < total else None,
        "truncated": off + lim < total,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def account_detail(project_id: str, account_id: str) -> dict[str, Any] | None:
    doc = _env_doc(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    row = next(
        (item for item in rows
         if str(item.get("id") or item.get("account_id") or "") == str(account_id or "")),
        None,
    )
    if row is None:
        return None
    out = _public_account(row, doc)
    out["bound_sessions"] = _bound_sessions(account_id)
    out["recent_logs"] = _account_logs(project_id, account_id)
    return out


def account_otp(
    project_id: str,
    account_id: str,
    *,
    env_profile: str = "",
    env_surface: str = "",
    node_id: str = "",
    sn: str = "",
    plugin_user_id: str = "",
    from_allowlist: list[str] | None = None,
    subject_contains: str = "",
) -> dict[str, Any] | None:
    """统一解析一个账号的验证码来源；不代替真实短信/邮件到达。"""
    from mino_nexus.resources.otp_resolve import gmail_inbox_address, resolve_effective_secrets

    doc = _env_doc(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    row = next(
        (item for item in rows
         if str(item.get("id") or item.get("account_id") or "") == str(account_id or "")),
        None,
    )
    if row is None:
        return None

    env_key = str(env_profile or row.get("env") or "test").strip() or "test"
    surface = str(env_surface or "").strip()
    secrets = resolve_effective_secrets(doc, env_profile=env_key, env_surface=surface)
    otp = secrets.get("otp") if isinstance(secrets.get("otp"), dict) else {}
    mode = str(otp.get("mode") or "auto").strip().lower()
    fixed = str(otp.get("fixed") or "").strip()
    account_code = str(row.get("otp") or row.get("sms_code") or "").strip()
    phone = str(row.get("phone") or "").strip()
    mail = str(row.get("email") or "").strip()
    recipient = mail or phone
    inbox = ""
    if plugin_user_id:
        try:
            inbox = gmail_inbox_address(
                doc, plugin_user_id=plugin_user_id, node_id=node_id, sn=sn,
            )
        except Exception:
            inbox = ""
    allow = [str(x).strip() for x in (from_allowlist if from_allowlist is not None
                                      else otp.get("from_allowlist") or []) if str(x).strip()][:20]
    subject = str(subject_contains or otp.get("subject_contains") or "").strip()[:120]
    out: dict[str, Any] = {
        "account_id": text(account_id, 80),
        "code": "",
        "source": "missing",
        "otp_mode": mode,
        "recipient": recipient,
        "requires_human": False,
        "hint": "",
        "env_profile": env_key,
        "env_surface": surface,
        "gmail": {
            "configured": bool(inbox),
            "inbox": inbox,
            "from_allowlist": allow,
            "subject_contains": subject,
        },
    }
    if account_code:
        out.update(code=account_code, source="account_fixed", hint="账号本身配置了固定验证码")
        return out
    if fixed and mode in ("fixed", "auto"):
        out.update(code=fixed, source="env_fixed", hint="使用项目环境配置的固定验证码")
        return out
    if mode == "hitl":
        out.update(source="human_required", requires_human=True,
                   hint="当前环境接码模式是 hitl，请让用户从真实短信/邮箱读出验证码")
        return out
    if mode in ("gmail", "auto") and mail and inbox:
        out.update(source="gmail", hint="从该账号绑定的 Gmail 收件箱取码")
        return out
    if mode == "gmail":
        if not mail:
            out.update(hint="接码模式是 Gmail，但账号没有 email")
        elif not inbox:
            out.update(hint="接码模式是 Gmail，但当前用户/节点没有已配置的 Gmail 收件箱")
        return out
    if mode == "auto":
        out.update(source="human_required", requires_human=True,
                   hint="没有固定码、没有可用的 Gmail 收件箱；真实短信需要用户从手机读出验证码")
        return out
    if mode == "fixed":
        out.update(hint="接码模式是 fixed，但项目环境 otp.fixed 为空")
    return out


def _bound_sessions(account_id: str) -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.device_app_session import DeviceAppSession

    with session_scope() as db:
        rows = (
            db.query(DeviceAppSession)
            .filter(DeviceAppSession.bound_account_id == str(account_id or ""))
            .order_by(DeviceAppSession.updated_at.desc())
            .limit(10)
            .all()
        )
        return [
            {
                "sn": text(row.sn, 80),
                "package_id": text(row.package_id, 120),
                "session": text(row.session, 24),
                "identity_hint": mask_ident(row.identity_hint)[0],
                "stale": bool(row.stale),
                "updated_at": text(row.updated_at, 40),
            }
            for row in rows
        ]


def _account_logs(project_id: str, account_id: str) -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.resource_ops import ResourceAllocationLog

    with session_scope() as db:
        rows = (
            db.query(ResourceAllocationLog)
            .filter(
                ResourceAllocationLog.project_id == str(project_id or ""),
                ResourceAllocationLog.account_id == str(account_id or ""),
            )
            .order_by(ResourceAllocationLog.id.desc())
            .limit(10)
            .all()
        )
        out = []
        for row in rows:
            masked, _ = mask_ident(row.account_ident)
            out.append({
                "id": row.id,
                "created_at": text(row.created_at, 40),
                "action": text(row.action, 40),
                "message": scrub_text(row.message, 200),
                "account_ident_masked": masked,
                "run_id": text(row.run_id, 80),
                "case_id": text(row.case_id, 80),
                "sn": text(row.sn, 80),
                "detail": redact(row.detail_json if isinstance(row.detail_json, dict) else {}, scrub=True),
            })
        return out


def logs(
    project_id: str,
    *,
    action: str = "",
    env: str = "",
    run_id: str = "",
    case_id: str = "",
    account_ident: str = "",
    sn: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    ps.require_project(project_id)
    data = list_allocation_logs(
        project_id,
        page=max(1, int(page or 1)),
        page_size=max(1, min(int(page_size or 50), 100)),
        action=action,
        env=env,
        run_id=run_id,
        case_id=case_id,
        account_ident=account_ident,
        sn=sn,
    )
    items = []
    for row in data.get("items") or []:
        masked, _ = mask_ident(row.get("account_ident"))
        item = dict(row)
        item["account_ident_masked"] = masked
        item.pop("account_ident", None)
        item["message"] = scrub_text(item.get("message"), 300)
        item["detail"] = redact(item.get("detail") if isinstance(item.get("detail"), dict) else {}, scrub=True)
        items.append(item)
    return {
        "items": items,
        "total": int(data.get("total") or 0),
        "page": int(data.get("page") or 1),
        "page_size": int(data.get("page_size") or 50),
        "has_more": (int(data.get("page") or 1) * int(data.get("page_size") or 50)) < int(data.get("total") or 0),
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


_DEVICE_KEYS = (
    "sn", "label", "platform", "type", "status", "online", "ready",
    "desktop_state", "blocked_reason", "node_id", "node_name", "agent_version",
    "capabilities", "readiness", "last_seen", "updated_at",
)


def _public_device(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key in _DEVICE_KEYS:
        if key not in row:
            continue
        value = row.get(key)
        if key == "label":
            out[key] = scrub_text(value, 120)
        elif key == "blocked_reason":
            out[key] = scrub_text(value, 300)
        else:
            out[key] = redact(value, scrub=key in {"capabilities", "readiness"})
    return out


def _public_session(row: dict[str, Any]) -> dict[str, Any]:
    hint, _ = mask_ident(row.get("identity_hint"))
    return {
        "sn": text(row.get("sn"), 80),
        "package_id": text(row.get("package_id"), 120),
        "app_version": text(row.get("app_version"), 60),
        "session": text(row.get("session"), 24),
        "session_display": scrub_text(row.get("session_display"), 40),
        "bound_account_id": text(row.get("bound_account_id"), 80),
        "identity_masked": hint,
        "stale": bool(row.get("stale")),
        "stale_note": scrub_text(row.get("stale_note"), 120),
        "updated_at": text(row.get("updated_at"), 40),
    }


def devices(
    project_id: str,
    *,
    status: str = "",
    platform: str = "",
    sn: str = "",
    include_sessions: bool = False,
    limit: int = DEFAULT_PAGE,
    offset: int = 0,
) -> dict[str, Any]:
    ps.require_project(project_id)
    rows = []
    try:
        rows = list(ui_devices() or [])
    except Exception:
        rows = []
    status_q = str(status or "").strip().lower()
    platform_q = str(platform or "").strip().lower()
    sn_q = str(sn or "").strip()
    filtered = []
    for row in rows:
        if sn_q and str(row.get("sn") or row.get("id") or "") != sn_q:
            continue
        if platform_q and str(row.get("platform") or row.get("type") or "").lower() != platform_q:
            continue
        if status_q == "online" and not bool(row.get("online")):
            continue
        if status_q == "offline" and bool(row.get("online")):
            continue
        filtered.append(_public_device(row))
    total = len(filtered)
    lim = max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))
    off = max(0, int(offset or 0))
    page = filtered[off: off + lim]
    sessions_by_sn: dict[str, list[dict[str, Any]]] = {}
    if include_sessions:
        try:
            matrix = list_project_device_app_matrix(project_id, limit=500)
            for sess in matrix.get("sessions") or []:
                key = str(sess.get("sn") or "")
                bucket = sessions_by_sn.setdefault(key, [])
                if len(bucket) < 5:
                    bucket.append(_public_session(sess))
        except Exception:
            sessions_by_sn = {}
        for item in page:
            item["sessions"] = sessions_by_sn.get(str(item.get("sn") or ""), [])
    return {
        "items": page,
        "total": total,
        "limit": lim,
        "next_offset": off + lim if off + lim < total else None,
        "truncated": off + lim < total,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


_ENV_KEYS = ("key", "label", "description", "platform", "base_url")


def _safe_url(value: Any) -> str:
    """只保留 URL 的 scheme/host/path/非秘密查询参数；去掉 userinfo 与 fragment。"""
    raw = text(value, 300)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.hostname:
            return ""
        host = parts.hostname
        if parts.port:
            host = f"{host}:{parts.port}"
        query = urlencode([
            (key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not is_secret_key(key)
        ])
        return urlunsplit((parts.scheme, host, parts.path, query, ""))
    except (TypeError, ValueError):
        return ""


def _safe_env(project_id: str) -> dict[str, Any]:
    doc = _env_doc(project_id)
    channels = []
    for ch in doc.get("channels") or []:
        if not isinstance(ch, dict):
            continue
        channels.append({
            "id": text(ch.get("id"), 64),
            "label": text(ch.get("label"), 80),
            "alias": text(ch.get("alias"), 80),
            "app_identifier": text(ch.get("app_identifier"), 160),
            "field": text(ch.get("field"), 64),
            "third_party": bool(ch.get("third_party")),
            "placeholder": scrub_text(ch.get("placeholder"), 120),
        })
    environments = []
    for env in doc.get("environments") or []:
        if not isinstance(env, dict):
            continue
        item = {
            key: (
                scrub_text(env.get(key), 200) if key == "description"
                else _safe_url(env.get(key)) if key == "base_url"
                else text(env.get(key), 200)
            )
            for key in _ENV_KEYS if env.get(key)
        }
        secrets = env.get("secrets") if isinstance(env.get("secrets"), dict) else {}
        item["secret_slots"] = sorted(str(key) for key in secrets)
        environments.append(item)
    profiles = {}
    raw_profiles = doc.get("profiles") if isinstance(doc.get("profiles"), dict) else {}
    for env_key, profile in raw_profiles.items():
        if not isinstance(profile, dict):
            continue
        channels_out = {}
        for cid, values in profile.items():
            if not isinstance(values, dict):
                continue
            channels_out[str(cid)] = {
                str(field): {"configured": bool(str(value or "").strip())}
                for field, value in values.items()
                if not is_secret_key(field)
            }
        profiles[str(env_key)] = channels_out
    channel_secrets = doc.get("channel_secrets") if isinstance(doc.get("channel_secrets"), dict) else {}
    inbox = doc.get("gmail_inbox") if isinstance(doc.get("gmail_inbox"), dict) else {}
    return {
        "project_id": text(project_id, 80),
        "default_profile": text(doc.get("default_profile"), 40),
        "pipeline": [text(key, 40) for key in doc.get("pipeline") or []],
        "environments": environments,
        "channels": channels,
        "profiles": profiles,
        "secret_slots": {
            str(key): sorted(str(x) for x in (value if isinstance(value, dict) else {}))
            for key, value in channel_secrets.items()
        },
        "gmail_inbox": {
            "configured": bool(str(inbox.get("address") or "").strip()),
            "address_masked": mask_ident(scrub_text(inbox.get("address"), 200))[0],
        },
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def environment(project_id: str) -> dict[str, Any]:
    return _safe_env(project_id)


def overview(project_id: str, *, env: str = "") -> dict[str, Any]:
    doc = _env_doc(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    accounts_pub = [_public_account(row, doc) for row in rows]
    env_q = str(env or "").strip().lower()
    if env_q:
        accounts_pub = [item for item in accounts_pub if str(item.get("env") or "").lower() == env_q]
    by_state: dict[str, int] = {}
    by_env: dict[str, int] = {}
    by_lifecycle: dict[str, int] = {}
    for item in accounts_pub:
        by_state[item["state"]] = by_state.get(item["state"], 0) + 1
        by_env[item["env"]] = by_env.get(item["env"], 0) + 1
        by_lifecycle[item["lifecycle"]] = by_lifecycle.get(item["lifecycle"], 0) + 1

    device_rows = []
    try:
        device_rows = list(ui_devices() or [])
    except Exception:
        device_rows = []
    online = sum(1 for row in device_rows if bool(row.get("online")))
    ready = sum(1 for row in device_rows if bool(row.get("online")) and bool(row.get("ready")))

    lease_items = [item for item in accounts_pub if item["state"] == "occupied"]
    expiring = [
        item for item in lease_items
        if int(item.get("lease_remaining_sec") or 0) < 3600
    ]
    recent = logs(project_id, env=env_q, page_size=5)
    warnings = []
    if not device_rows:
        warnings.append("设备台账为空")
    elif not online:
        warnings.append("当前没有在线设备")
    if not accounts_pub:
        warnings.append("当前筛选下号池为空" if env_q else "号池为空")
    if expiring:
        warnings.append(f"{len(expiring)} 个账号租约将在 1 小时内到期或被判定过期")
    return {
        "project_id": text(project_id, 80),
        "env": env_q,
        "accounts": {
            "total": len(accounts_pub),
            "by_state": by_state,
            "by_env": by_env,
            "by_lifecycle": by_lifecycle,
        },
        "devices": {"total": len(device_rows), "online": online, "ready": ready},
        "leases": {
            "occupied": len(lease_items),
            "expiring_soon": len(expiring),
        },
        "recent_logs": recent.get("items") or [],
        "warnings": warnings,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _reserve_run_id(run_id: str) -> str:
    rid = str(run_id or "").strip()
    if rid:
        return rid[:80]
    from mino_nexus.core.ids import prefixed
    return prefixed("mcp", 16)


def reserve_account(
    project_id: str,
    *,
    account_id: str = "",
    env: str = "",
    lifecycle: str = "",
    run_id: str = "",
    ttl_sec: int | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    """占用一个账号给 MCP 调用方。返回 (账号, 错误, run_id)。不返回凭证。"""
    doc = _env_doc(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    rid = _reserve_run_id(run_id)
    if str(run_id or "").strip():
        existing = next(
            (row for row in rows if str(_lease(row).get("run_id") or "") == rid),
            None,
        )
        if existing is not None:
            if str(_lease(existing).get("case_id") or "") != "mcp-reserve":
                return None, "run_id 已被其它租约使用，未占用", rid
            return _public_account(existing, doc), "", rid
    wanted = str(account_id or "").strip()
    env_q = str(env or "").strip().lower()
    lifecycle_q = str(lifecycle or "").strip().lower()
    candidates = []
    seen_wanted = False
    target_row = None
    for row in rows:
        aid = str(row.get("id") or row.get("account_id") or "")
        if not aid:
            continue
        if wanted and aid != wanted:
            continue
        if wanted and aid == wanted:
            seen_wanted = True
            target_row = row
        if env_q and str(row.get("env") or "").lower() != env_q:
            continue
        facets = account_facets(row)
        if lifecycle_q and str(facets.get("lifecycle") or "").lower() != lifecycle_q:
            continue
        if str(facets.get("health") or "") in {"dirty", "quarantine", "bad"}:
            continue
        if _account_state(row) != "free":
            continue
        candidates.append(row)
    if wanted and not seen_wanted:
        return None, f"账号不存在: {wanted}", ""
    if not candidates:
        if wanted:
            if target_row is not None and str(account_facets(target_row).get("health") or "") in {"dirty", "quarantine", "bad"}:
                return None, "账号状态不可用（待重置/隔离/坏号），未占用", ""
            if target_row is not None and _account_state(target_row) != "free":
                return None, "账号已被占用或锁定，未占用", ""
            return None, "账号不满足 env/lifecycle 条件，未占用", ""
        return None, "没有可占用的账号（可能已被租用/锁定，或筛选条件不匹配）", ""
    row = candidates[0]
    aid = str(row.get("id") or row.get("account_id") or "")
    ok = _mark_leased(
        project_id,
        aid,
        rid,
        case_id="mcp-reserve",
        env=str(row.get("env") or env_q or ""),
        ttl_sec=max(60, int(ttl_sec)) if ttl_sec else _lease_ttl_sec(),
    )
    if not ok:
        return None, "账号已被占用或状态发生变化，未占用", ""
    latest = next(
        (item for item in list_test_accounts(doc, project_id=project_id)
         if str(item.get("id") or item.get("account_id") or "") == aid),
        None,
    )
    if latest is None:
        return None, "账号占用后读取失败", ""
    return _public_account(latest, doc), "", rid


def release_account(project_id: str, account_id: str, *, run_id: str = "") -> tuple[bool, str]:
    doc = _env_doc(project_id)
    rows = list_test_accounts(doc, project_id=project_id)
    row = next(
        (item for item in rows
         if str(item.get("id") or item.get("account_id") or "") == str(account_id or "")),
        None,
    )
    if row is None:
        return False, "账号不存在"
    lease = _lease(row)
    if str(lease.get("case_id") or "") != "mcp-reserve":
        return False, "该租约不是 MCP 预约，不能通过此接口释放"
    if not str(run_id or "").strip():
        return False, "run_id 必填"
    rid = _reserve_run_id(run_id)
    if str(lease.get("run_id") or "") != rid:
        return False, "run_id 与当前租约不匹配，未释放"
    _clear_leased(project_id, account_id, rid)
    latest = next(
        (item for item in list_test_accounts(doc, project_id=project_id)
         if str(item.get("id") or item.get("account_id") or "") == str(account_id or "")),
        None,
    )
    if latest is not None and str(_lease(latest).get("run_id") or ""):
        return False, "释放时租约已变化，未释放"
    return True, ""
