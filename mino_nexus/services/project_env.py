"""项目级运行环境：可增删环境 / 渠道，上线路径可配。

从 MiniOrangeServer `server/services/project_env.py` 搬来，去掉 SQLAlchemy。
项目读写在 `project_store`。
"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

ENV_PROFILE_KEYS = ("dev", "test", "pre", "prod")

ENV_PROFILE_LABELS = {
    "dev": "开发",
    "test": "测试",
    "pre": "预发",
    "prod": "正式",
}

CHANNEL_KINDS = ("app", "web", "server")
CHANNEL_PLATFORMS = ("mac", "win", "android", "web")

DEFAULT_CHANNELS = [
    {"id": "android", "kind": "app", "platform": "android", "alias": "", "third_party": False, "label": "安卓", "field": "package", "placeholder": "com.example.app"},
    {"id": "web", "kind": "web", "platform": "web", "alias": "", "third_party": False, "label": "Web", "field": "base_url", "placeholder": "https://test.example.com"},
    {"id": "win", "kind": "app", "platform": "win", "alias": "", "third_party": False, "label": "Windows", "field": "path", "placeholder": "完整 EXE 路径或 AUMID"},
    {"id": "mac", "kind": "app", "platform": "mac", "alias": "", "third_party": False, "label": "Mac", "field": "bundle", "placeholder": "com.example.desktop"},
    {"id": "server", "kind": "server", "alias": "", "third_party": False, "label": "Server", "field": "base_url", "placeholder": "https://api.example.com"},
]

_PLATFORM_PRESET = {c["id"]: c for c in DEFAULT_CHANNELS}

DEFAULT_ENVIRONMENTS = [
    {"key": "test", "label": "测试"},
    {"key": "pre", "label": "预发"},
    {"key": "prod", "label": "正式"},
]

EMPTY_PLATFORM = {
    "android": {"package": ""},
    "ios": {"bundle": ""},
    "web": {"base_url": ""},
}

_KEY_RE = re.compile(r"[^a-z0-9_-]+")


def _slug(text: str, fallback: str = "env") -> str:
    s = _KEY_RE.sub("", str(text or "").strip().lower())[:24]
    return s or fallback


def empty_profile(channels: List[dict]) -> dict:
    out: Dict[str, Any] = {}
    for ch in channels:
        cid = ch.get("id")
        field = ch.get("field") or "value"
        if cid:
            out[cid] = {field: ""}
    return out


def default_project_env() -> dict:
    channels = [copy.deepcopy(c) for c in DEFAULT_CHANNELS]
    environments = [copy.deepcopy(e) for e in DEFAULT_ENVIRONMENTS]
    for e in environments:
        e["secrets"] = default_env_secrets()
    profiles = {e["key"]: empty_profile(channels) for e in environments}
    return {
        "default_profile": "test",
        "environments": environments,
        "channels": channels,
        "pipeline": [e["key"] for e in environments],
        "profiles": profiles,
        "gmail_inbox": _norm_gmail_inbox({}),
        "channel_secrets": {},
        "channel_gmail_alias": {},
        "channel_phone_seq": {},
    }





def _channel_title(ch: Optional[dict]) -> str:
    row = ch if isinstance(ch, dict) else {}
    alias = str(row.get("alias") or "").strip()
    if alias:
        return alias
    return str(row.get("label") or row.get("id") or "").strip()


def _channel_by_id(channels: Optional[List[dict]], cid: str) -> Optional[dict]:
    want = str(cid or "").strip()
    if not want:
        return None
    for ch in channels or []:
        if isinstance(ch, dict) and str(ch.get("id") or "") == want:
            return ch
    return None


def _norm_channel(raw: Any, seen: set) -> dict:
    from mino_nexus.core.platform_contract import environment_channel
    if not isinstance(raw, dict): raise ValueError("channel_object_required")
    cid = raw.get("id")
    if not isinstance(cid, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", cid) or cid in seen:
        raise ValueError("channel_identity_invalid_or_duplicate")
    environment_channel(raw)
    field = raw.get("field")
    if not isinstance(field, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", field):
        raise ValueError("channel_field_required")
    seen.add(cid)
    return {**raw, "id":cid, "field":field, "label":str(raw.get("label") or cid),
            "alias":str(raw.get("alias") or ""), "app_identifier":str(raw.get("app_identifier") or ""),
            "third_party":bool(raw.get("third_party",False)), "placeholder":str(raw.get("placeholder") or "")}


OTP_MODES = ("auto", "fixed", "gmail", "hitl")
LOGIN_MODES = ("auto", "pool", "hitl")
LOGIN_KINDS = ("phone", "email")


def default_env_secrets() -> dict:
    return {
        "otp": {
            "mode": "auto",
            "fixed": "",
            "from_allowlist": [],
            "subject_contains": "",
            "poll_interval_ms": 3000,
            "max_wait_ms": 90000,
        },
        "login": {"mode": "auto", "kind": "phone"},
    }


def _validated_mode(mode: str | None, *, slot: str) -> str:
    value = "auto" if mode is None else mode
    if value not in (OTP_MODES if slot == "otp" else LOGIN_MODES):
        raise ValueError(f"{slot}_mode_invalid")
    return value


def _norm_otp_slot(raw: Any) -> dict:
    src = raw if isinstance(raw, dict) else {}
    mode = _validated_mode(src.get("mode"), slot="otp")
    allow = src.get("from_allowlist")
    if isinstance(allow, str):
        allow = [x.strip() for x in allow.split(",") if x.strip()]
    elif not isinstance(allow, list):
        allow = []
    allow = [str(x).strip()[:120] for x in allow if str(x).strip()][:20]
    return {
        "mode": mode,
        "fixed": str(src.get("fixed") or "").strip()[:32],
        "from_allowlist": allow,
        "subject_contains": str(src.get("subject_contains") or "").strip()[:120],
        "poll_interval_ms": max(1000, min(int(src.get("poll_interval_ms") or 3000), 30_000)),
        "max_wait_ms": max(5000, min(int(src.get("max_wait_ms") or 90_000), 180_000)),
    }


def _norm_login_slot(raw: Any) -> dict:
    src = raw if isinstance(raw, dict) else {}
    if not src and isinstance(raw, type(None)):
        src = {}
    mode = _validated_mode(src.get("mode"), slot="login")
    kind = str(src.get("kind") or "phone").strip().lower()
    if kind not in LOGIN_KINDS:
        raise ValueError("login_kind_invalid")
    return {"mode": mode, "kind": kind}


def _norm_env_secrets(raw: Any) -> dict:
    if raw is not None and not isinstance(raw, dict): raise ValueError("secrets_object_required")
    src = raw or {}
    if "phone" in src or "login_kind" in src: raise ValueError("obsolete_login_fields")
    return {"otp":_norm_otp_slot(src.get("otp")), "login":_norm_login_slot(src.get("login"))}


def _norm_gmail_inbox(raw: Any) -> dict:
    import ast

    src = raw if isinstance(raw, dict) else {}
    addr = src.get("address") if isinstance(src, dict) else None
    if isinstance(addr, dict):
        addr = addr.get("address")
    if addr is None and not isinstance(raw, dict):
        addr = raw
    s = str(addr or "").strip()
    if s.startswith("{") and "address" in s:
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, dict):
                s = str(parsed.get("address") or "").strip()
        except (SyntaxError, ValueError):
            pass
    return {"address": s[:120]}


def _norm_channel_gmail_alias_doc(
    raw: Any, channels: List[dict], env_keys: set[str]
) -> dict[str, dict[str, dict[str, str]]]:
    from mino_nexus.services.gmail_alias_lease import normalize_channel_gmail_alias

    return normalize_channel_gmail_alias(raw, channels, env_keys)


def _norm_channel_phone_seq_doc(
    raw: Any, channels: List[dict], env_keys: set[str]
) -> dict[str, dict[str, dict[str, str]]]:
    from mino_nexus.services.phone_pool_lease import normalize_channel_phone_seq

    return normalize_channel_phone_seq(raw, channels, env_keys)


def _norm_channel_secrets(raw: Any, channels: List[dict], env_keys: set[str]) -> dict:
    src = raw if isinstance(raw, dict) else {}
    ch_ids = {str(c.get("id") or "") for c in channels if c.get("id")}
    out: dict = {}
    for cid, per_env in src.items():
        if cid not in ch_ids or not isinstance(per_env, dict):
            raise ValueError("channel_secrets_channel_invalid")
        row: dict = {}
        for ek, slot in per_env.items():
            env_k = str(ek)
            if env_k not in env_keys or not isinstance(slot, dict):
                raise ValueError("channel_secrets_environment_invalid")
            if bool(slot.get("inherit")):
                continue
            row[env_k] = _norm_env_secrets(slot)
        if row:
            out[cid] = row
    return out


def env_secrets(env_doc: dict | None, env_key: str = "") -> dict:
    key = _slug(env_key, "")
    for row in (env_doc or {}).get("environments") or []:
        if isinstance(row, dict) and str(row.get("key") or "") == key:
            return _norm_env_secrets(row.get("secrets"))
    return default_env_secrets()


def _norm_environment(raw: Any, seen: set) -> dict:
    if not isinstance(raw, dict): raise ValueError("environment_object_required")
    key = raw.get("key")
    if not isinstance(key,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,24}",key) or key in seen:
        raise ValueError("environment_identity_invalid_or_duplicate")
    seen.add(key)
    return {"key":key,"label":str(raw.get("label") or key),"secrets":_norm_env_secrets(raw.get("secrets"))}


def _profile_value(block: Any, field: str) -> str:
    if block is None: return ""
    if not isinstance(block,dict): raise ValueError("profile_channel_object_required")
    value = block.get(field, "")
    if not isinstance(value,str): raise ValueError("profile_channel_value_invalid")
    return value.strip()


def _norm_profile(raw: Any, channels: List[dict]) -> dict:
    src = raw if isinstance(raw, dict) else {}
    out = copy.deepcopy(src)
    for ch in channels:
        cid = ch["id"]
        field = ch["field"]
        block = copy.deepcopy(src[cid]) if isinstance(src.get(cid), dict) else {}
        block[field] = _profile_value(src.get(cid), field)
        out[cid] = block
    return out


def normalize_project_env(raw: Any) -> dict:
    """Current environment contract only; empty new projects use explicit defaults."""
    if raw is None or raw == {}: return default_project_env()
    if not isinstance(raw, dict): raise ValueError("environment_document_required")
    validate_environment_write(raw)
    profiles_in = raw["profiles"]
    seen_ch, seen_env = set(), set()
    channels = [_norm_channel(row,seen_ch) for row in raw["channels"]]
    environments = [_norm_environment(row,seen_env) for row in raw["environments"]]
    pipeline = list(raw["pipeline"])
    default_profile = raw["default_profile"]

    profiles = {}
    src_profiles = profiles_in or {}
    for env in environments:
        profiles[env["key"]] = _norm_profile(src_profiles.get(env["key"]), channels)

    from mino_nexus.services.account_facet_schema import normalize_extensions

    env_keys = {e["key"] for e in environments}
    return {
        "default_profile": default_profile,
        "environments": environments,
        "channels": channels,
        "pipeline": pipeline,
        "profiles": profiles,
        "gmail_inbox": _norm_gmail_inbox({}),
        "channel_secrets": _norm_channel_secrets(raw.get("channel_secrets"), channels, env_keys),
        "channel_gmail_alias": _norm_channel_gmail_alias_doc(
            raw.get("channel_gmail_alias"), channels, env_keys
        ),
        "channel_phone_seq": _norm_channel_phone_seq_doc(
            raw.get("channel_phone_seq"), channels, env_keys
        ),
        "account_facet_extensions": normalize_extensions(raw.get("account_facet_extensions")),
        "account_template_ids": _norm_template_ids(raw.get("account_template_ids")),
        "account_pool_local": raw.get("account_pool_local")
        if isinstance(raw.get("account_pool_local"), dict)
        else {},

    }


def _norm_template_ids(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in raw:
        tid = str(item or "").strip()[:48]
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def account_ident(row: dict | None) -> str:
    """Console/日志展示用短标签：display_name → account_id → 手机号 → 邮箱 → 用户名。

    前置里指定账号命中见 account_lease_match_blob（不含 display_name）；facet 硬约束仍走 Requirement DSL。
    """
    row = row if isinstance(row, dict) else {}
    display = str(row.get("display_name") or "").strip()
    if display:
        return display[:80]
    phone = re.sub(r"\s+", "", str(row.get("phone") or ""))
    if phone:
        return phone
    email = str(row.get("email") or "").strip()
    if email:
        return email
    username = str(row.get("username") or "").strip()
    if username:
        return username
    return str(row.get("account_id") or row.get("id") or "").strip()


def account_label(row: dict | None, channels: Optional[List[dict]] = None) -> str:
    row = row if isinstance(row, dict) else {}
    ident = account_ident(row) or "未填号码"
    env = str(row.get("env") or "").strip() or "-"
    return f"{ident} · {env}"


def _norm_test_accounts(raw: Any, env_doc: dict | None = None) -> List[dict]:
    rows = raw if isinstance(raw, list) else []
    out: List[dict] = []
    seen: set = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        from mino_nexus.services.account_pool_templates import new_account_id

        aid = str(item.get("account_id") or item.get("id") or "").strip() or new_account_id()
        if aid in seen:
            continue
        seen.add(aid)
        from mino_nexus.services.account_pool_templates import facets_defaults_from_catalog
        from mino_nexus.services.resource_pool import account_facets

        display_name = str(item.get("display_name") or "").strip()[:80]
        if "tags" in item: raise ValueError("obsolete_account_tags; use facets")
        profile_id = str(item.get("profile_id") or "").strip()[:40]
        stored = item.get("facets") if isinstance(item.get("facets"), dict) else {}
        defaults = facets_defaults_from_catalog(env_doc)
        stored_lower = {
            k: str(v).strip().lower() for k, v in stored.items() if str(v).strip()
        }
        combined = {**defaults, **stored_lower}
        core = account_facets(
            {"facets": combined, "profile_id": profile_id}
        )
        from mino_nexus.services.account_facet_schema import merge_account_facets
        from mino_nexus.services.account_pool_templates import merged_pool_field_defs

        ext_defs = merged_pool_field_defs(env_doc)
        if ext_defs:
            facets = merge_account_facets({**combined, **core}, ext_defs)
        else:
            facets = dict(core)
            for k, v in combined.items():
                if k not in facets:
                    facets[k] = v
        if not str(facets.get("health") or "").strip():
            facets["health"] = "available"
        from mino_nexus.services.account_facet_schema import facets_for_storage
        from mino_nexus.services.account_pool_templates import merged_pool_field_defs

        facets = facets_for_storage(facets, merged_pool_field_defs(env_doc))
        phone = str(item.get("phone") or "").strip()[:32]
        email = str(item.get("email") or "").strip()[:80]
        username = str(item.get("username") or "").strip()[:80]
        ident = account_ident(
            {
                "account_id": aid,
                "display_name": display_name,
                "phone": phone,
                "email": email,
                "username": username,
            }
        )
        lease_raw = item.get("lease") if isinstance(item.get("lease"), dict) else {}
        lease = {}
        rid = str(lease_raw.get("run_id") or "").strip()
        if rid:
            lease = {
                "run_id": rid[:80],
                "case_id": str(lease_raw.get("case_id") or "").strip()[:80],
                "leased_at": str(lease_raw.get("leased_at") or lease_raw.get("at") or "").strip()[:40],
                "expires_at": str(lease_raw.get("expires_at") or "").strip()[:40],
            }
            lease_sn = str(lease_raw.get("sn") or "").strip()
            if lease_sn:
                lease["sn"] = lease_sn[:64]
        out.append(
            {
                "id": aid,
                "account_id": aid,
                "display_name": display_name or ident[:40],
                "name": display_name or ident[:40],
                "env": _slug(item.get("env") or "test", "test"),
                "kind": str(item.get("kind") or "mixed").strip() or "mixed",
                "phone": phone,
                "email": email,
                "username": username,
                "password": str(item.get("password") or "").strip()[:120],
                "otp": str(item.get("otp") or item.get("sms_code") or "").strip()[:32],
                "facets": facets,
                "profile_id": profile_id,
                "note": str(item.get("note") or "").strip()[:200],
                "locked": bool(item.get("locked")),
                "lease": lease,
            }
        )
    return out


def public_test_accounts(
    rows: List[dict],
    *,
    include_password: bool = False,
    facet_extensions: Optional[List[dict]] = None,
    pool_field_defs: Optional[List[dict]] = None,
) -> List[dict]:
    """列表给前端看。筛号 / 环境接口默认不带明文；账号管理页需要带上才能展示。"""
    from mino_nexus.services.account_facet_schema import extension_display_rows, merge_account_facets
    from mino_nexus.services.resource_pool import account_facets, lease_display

    tpl_ext = pool_field_defs if pool_field_defs is not None else (facet_extensions or [])
    out = []
    for row in rows or []:
        pwd = str(row.get("password") or "")
        item = dict(row)
        item.pop("tags", None)
        item.pop("template_id", None)
        item.pop("template_field_defs", None)
        stored = item.get("facets") if isinstance(item.get("facets"), dict) else {}
        # account_facets() 只保留五维核心键，不能用它当扩展字段真源（会把已存模板/自定义 facet 裁成 unknown）
        core = account_facets({**item, "facets": stored})
        facets = merge_account_facets({**core, **stored}, tpl_ext)
        item["facets"] = facets
        ext_rows = extension_display_rows(facets, tpl_ext, configured_only=True)
        item["facets_brief"] = " · ".join(f"{r['title']}:{r['label']}" for r in ext_rows)
        item["lease_display"] = lease_display(item.get("lease"))
        item["has_password"] = bool(pwd)
        item["password_masked"] = ("••••" + pwd[-2:]) if len(pwd) >= 4 else ("••••" if pwd else "")
        otp = str(item.get("otp") or "")
        item["has_otp"] = bool(otp)
        if include_password:
            item["password"] = pwd
        else:
            item.pop("password", None)
            item.pop("otp", None)
        out.append(item)
    return out


def list_test_accounts(env_doc: dict, *, project_id: str = "") -> List[dict]:
    pid = str(project_id or "").strip()
    doc = env_doc if isinstance(env_doc, dict) else {}
    if pid:
        from mino_nexus.services.pool_account_store import list_accounts

        return list_accounts(pid, doc)
    return _norm_test_accounts(doc.get("test_accounts"), env_doc=doc)


def _merge_account_row(row: dict, old: dict) -> dict:
    out = dict(row)
    if not out.get("password"):
        out["password"] = str(old.get("password") or "")
    if not out.get("otp"):
        out["otp"] = str(old.get("otp") or "")
    if not out.get("profile_id"):
        out["profile_id"] = str(old.get("profile_id") or "")
    if not out.get("display_name"):
        out["display_name"] = str(old.get("display_name") or "")
    if not out.get("facets") and old.get("facets"):
        out["facets"] = old.get("facets")
    return out


def save_test_accounts(env_doc: dict, rows: List[dict], *, project_id: str = "") -> dict:
    doc = dict(env_doc or {})
    pid = str(project_id or "").strip()
    incoming = _norm_test_accounts(rows, env_doc=doc)
    prev = {str(x.get("id")): x for x in list_test_accounts(doc, project_id=pid)}
    merged = [_merge_account_row(row, prev.get(row["id"]) or {}) for row in incoming]
    if pid:
        from mino_nexus.services.pool_account_store import env_without_test_accounts_blob, replace_all

        replace_all(pid, merged)
        return env_without_test_accounts_blob(doc)
    doc["test_accounts"] = merged
    return doc


def save_one_test_account(
    env_doc: dict,
    row: dict,
    *,
    project_id: str,
    account_id: str = "",
) -> dict[str, Any]:
    """单条 upsert，避免整表 PUT。"""
    from mino_nexus.services.pool_account_store import upsert_one

    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id required")
    doc = dict(env_doc or {})
    aid = str(account_id or row.get("account_id") or row.get("id") or "").strip()
    prev = {str(x.get("id")): x for x in list_test_accounts(doc, project_id=pid)}
    old = prev.get(aid) or {}
    payload = dict(row)
    if aid:
        payload["id"] = aid
        payload["account_id"] = aid
    if isinstance(payload.get("facets"), dict):
        from mino_nexus.services.account_pool_templates import merged_pool_field_defs

        def_keys = {
            str(d.get("key") or "")
            for d in merged_pool_field_defs(doc)
            if str(d.get("key") or "")
        }
        # 五维核心键不在模板 def 里，但 Studio 保存时会带 health
        allowed_facet_keys = def_keys | {"health"}
        prev_f = old.get("facets") if isinstance(old.get("facets"), dict) else {}
        merged_f = dict(prev_f)
        incoming = payload["facets"]
        # 自动化写回（租号/探针/用例 pass）只带 facets_for_storage 的稀疏子集；
        # 缺 key 不能当作「用户清空」，否则会擦掉 Console 配的扩展 facet（如 field_7065t5）。
        for key, raw in incoming.items():
            k = str(key or "").strip()
            if not k or k not in allowed_facet_keys:
                continue
            if raw is None or not str(raw).strip():
                if k != "health":
                    merged_f.pop(k, None)
            else:
                merged_f[k] = str(raw).strip().lower()
        payload["facets"] = merged_f
    normed = _norm_test_accounts([{**old, **payload}], env_doc=doc)
    if not normed:
        raise ValueError("account save failed")
    merged = _merge_account_row(normed[0], old)
    saved = upsert_one(pid, merged)
    if not saved:
        raise ValueError("account save failed")
    return saved


_ENV_HINTS = (
    ("正式", "prod"),
    ("生产", "prod"),
    ("预发", "pre"),
    ("测试", "test"),
    ("开发", "dev"),
)


def infer_env_from_prompt(prompt: str) -> str:
    q = str(prompt or "")
    for word, key in _ENV_HINTS:
        if word in q:
            return key
    return ""


def _surface_is_primary(ch: Optional[dict]) -> bool:
    if not isinstance(ch, dict):
        return False
    return not bool(ch.get("third_party") or str(ch.get("alias") or "").strip())


def account_fits_surface(row: dict | None, want: str, channels: Optional[List[dict]] = None) -> bool:
    """账号不再绑定平台；保留签名供租号路径调用。"""
    return True


def resolve_surface_id(
    env_doc: dict | None, *, surface: str = "", platform: str = "",
    target_id: str = "", prompt: str = "", env_profile: str = "",
) -> str:
    """Resolve exactly one channel without changing an explicit identity."""
    from mino_nexus.core.platform_contract import target_platform
    channels = [c for c in (env_doc or {}).get("channels", []) if isinstance(c, dict) and c.get("id")]
    plat = target_platform(platform) if platform else ""
    want = str(surface or "")
    if want:
        matches = [c for c in channels if c["id"] == want]
        if len(matches) != 1:
            raise ValueError("surface_not_found_or_ambiguous")
        if plat and matches[0].get("platform") != plat:
            raise ValueError("surface_platform_mismatch")
        return want
    if plat:
        channels = [c for c in channels if c.get("platform") == plat]
    tid = str(target_id or "").strip()
    if tid:
        snap = profile_snapshot(env_doc, env_profile)
        channels = [c for c in channels if _profile_value(snap.get(c["id"], {}), c.get("field") or "value") == tid]
    if len(channels) != 1:
        raise ValueError("surface_selection_required" if channels else "surface_not_found")
    return str(channels[0]["id"])


def resolve_pick_requirements(
    *,
    prompt: str = "",
    env: str = "",
    env_doc: Optional[dict] = None,
    need_facets: Optional[dict] = None,
    requirements: Optional[dict] = None,
) -> dict[str, Any]:
    from mino_nexus.services.resource_pool import (
        compile_requirements_from_case_need,
        compile_requirements_from_text,
    )

    raw = str(prompt or "").strip()
    env_key = _slug(env, "") or infer_env_from_prompt(raw)
    need = need_facets if isinstance(need_facets, dict) else {}
    if isinstance(requirements, dict) and (requirements.get("all") or requirements.get("prefer")):
        req = dict(requirements)
    elif need:
        req = compile_requirements_from_case_need(need, env=env_key, env_doc=env_doc)
    elif raw:
        from mino_nexus.loop.session_ensure import account_need_from_case

        inferred = account_need_from_case({"precondition": raw}, {})
        req = compile_requirements_from_case_need(inferred, env=env_key, env_doc=env_doc)
    else:
        req = compile_requirements_from_text(raw, env=env_key)
    if env_key:
        req["env"] = env_key
    if env_doc and raw:
        from mino_nexus.services.account_requirement_compile import augment_requirements_from_precondition

        req = augment_requirements_from_precondition(req, raw, env_doc)
    if env_doc and raw:
        from mino_nexus.services.account_pool_templates import (
            augment_requirements_with_template,
            infer_template_id_from_text,
        )

        tid = infer_template_id_from_text(raw)
        if tid:
            req = augment_requirements_with_template(req, tid, env_doc)
    return req


def pick_test_accounts(
    rows: List[dict],
    *,
    prompt: str = "",
    env: str = "",
    surface: str = "",
    channels: Optional[List[dict]] = None,
    platform: str = "",
    target_id: str = "",
    env_doc: Optional[dict] = None,
    need_facets: Optional[dict] = None,
    requirements: Optional[dict] = None,
    run_id: str = "",
    holder_sn: str = "",
    project_id: str = "",
    observed_by_account: Optional[dict] = None,
) -> List[dict]:
    from mino_nexus.services.resource_pool import pick_accounts_by_requirements

    raw = str(prompt or "").strip()
    req = resolve_pick_requirements(
        prompt=raw,
        env=env,
        env_doc=env_doc,
        need_facets=need_facets,
        requirements=requirements,
    )
    from mino_nexus.services.resource_pool import relax_pool_session_for_device_login

    req = relax_pool_session_for_device_login(req)
    env_key = str(req.get("env") or _slug(env, "") or infer_env_from_prompt(raw))
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    pool_defs = merged_pool_field_defs(env_doc) if env_doc else []
    from mino_nexus.services.account_ident_parse import extract_account_ident_hints, primary_ident_query

    ident_hints = extract_account_ident_hints(raw)
    ident_q = primary_ident_query(raw)
    if not ident_q and re.fullmatch(r"[\d+\-@.\w]+", raw.replace(" ", "")):
        ident_q = raw.strip()
    return pick_accounts_by_requirements(
        rows,
        req,
        env=env_key,
        run_id=run_id,
        holder_sn=holder_sn,
        project_id=project_id,
        account_ident_fn=account_ident,
        observed_by_account=observed_by_account,
        ident_query=ident_q,
        ident_hints=ident_hints,
        field_defs=pool_defs,
    )


def profile_keys(env_doc: dict) -> List[str]:
    rows = env_doc.get("environments") if isinstance(env_doc,dict) else None
    if not isinstance(rows,list) or not rows: raise ValueError("environments_required")
    return [row["key"] for row in rows]


def pipeline_keys(env_doc: dict) -> List[str]:
    keys = set(profile_keys(env_doc))
    pipe = env_doc.get("pipeline")
    if not isinstance(pipe,list) or len(pipe)!=len(set(pipe)) or any(k not in keys for k in pipe):
        raise ValueError("environment_pipeline_invalid")
    return list(pipe)


def resolve_run_env_profile(
    env_doc: dict | None,
    *,
    requested: str = "",
    automation_default: str = "",
) -> str:
    """跑批启动时选定 env key：请求值须落在项目 environments，否则回落 default / automation。"""
    doc = env_doc if isinstance(env_doc, dict) else {}
    req = str(requested or "").strip()
    keys = set(profile_keys(doc))
    if req:
        if keys and req not in keys:
            labels = {
                str(e.get("key") or ""): str(e.get("label") or e.get("key") or "")
                for e in (doc.get("environments") or [])
                if isinstance(e, dict)
            }
            hint = "、".join(f"{labels.get(k, k)}({k})" for k in sorted(keys)[:8]) or "（空）"
            raise ValueError(f"运行环境「{req}」不在项目环境配置中。可选：{hint}")
        return req
    auto = str(automation_default or "").strip()
    if auto:
        return resolve_profile_name(doc, auto)
    return resolve_profile_name(doc, None)


def resolve_profile_name(env_doc: dict, env_profile: Optional[str] = None) -> str:
    keys = set(profile_keys(env_doc))
    selected = str(env_profile) if env_profile else str((env_doc or {}).get("default_profile") or "")
    if not selected or selected not in keys:
        raise ValueError("environment_profile_missing_or_invalid")
    return selected



def profile_snapshot(env_doc: dict, env_profile: Optional[str] = None) -> dict:
    """当前 Run 使用的 app.* 数据源（某一环境下的渠道配置）。"""
    name = resolve_profile_name(env_doc, env_profile)
    profiles = env_doc.get("profiles") or {}
    snap = profiles.get(name)
    channels = env_doc.get("channels") if isinstance(env_doc.get("channels"), list) else DEFAULT_CHANNELS
    if not isinstance(snap, dict):
        snap = empty_profile(channels)
    else:
        snap = copy.deepcopy(snap)
    # Missing values remain missing. A different environment or an iOS bundle
    # is not evidence of the selected Android application target.
    return snap


def target_id_from_snapshot(snap: dict, platform: str = "android", surface: str = "") -> str:
    from mino_nexus.core.platform_contract import target_platform
    plat = target_platform(platform)
    field = {"android":"package","web":"base_url","mac":"bundle","win":"path"}[plat]
    return _profile_value(snap.get(surface or plat),field)


def env_from_project_row(project: dict | None, apps: Optional[List[dict]] = None) -> dict:
    """Read this project's environment only; app order cannot select its values."""
    if not isinstance(project, dict):
        return default_project_env()
    return normalize_project_env(project.get("env"))



def validate_environment_write(doc: dict) -> None:
    """Reject before normalization; never guess platform or rewrite channel identities."""
    if not isinstance(doc,dict): raise ValueError("environment_document_required")
    if any(key in doc for key in ("android","ios","web","phone","login_kind","test_accounts")):
        raise ValueError("obsolete_environment_fields")
    envs=doc.get("environments")
    if not isinstance(envs,list) or not envs: raise ValueError("environments_required")
    seen_env=set()
    for row in envs: _norm_environment(row,seen_env)
    if doc.get("default_profile") not in seen_env: raise ValueError("default_profile_invalid")
    pipeline_keys(doc)
    profiles=doc.get("profiles")
    if not isinstance(profiles,dict) or set(profiles)!=seen_env or any(not isinstance(v,dict) for v in profiles.values()):
        raise ValueError("environment_profiles_mismatch")
    from mino_nexus.core.platform_contract import environment_channel
    channels = doc.get("channels")
    if not isinstance(channels, list) or not channels:
        raise ValueError("环境至少需要一个明确的渠道")
    seen = set()
    for index, ch in enumerate(channels):
        if not isinstance(ch, dict):
            raise ValueError(f"channels[{index}] 必须为渠道对象")
        cid = ch.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", cid) or cid in seen:
            raise ValueError(f"channels[{index}] 的 ID 无效或重复；请保留唯一稳定 ID")
        seen.add(cid)
        try:
            environment_channel(ch)
        except ValueError as exc:
            raise ValueError(f"渠道 {cid} 平台合同无效：{exc}；目标平台限 mac/win/android/web，server 不设 platform") from exc
