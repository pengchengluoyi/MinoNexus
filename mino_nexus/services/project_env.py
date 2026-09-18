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
CHANNEL_PLATFORMS = ("android", "ios", "web", "pc", "mac", "server")

DEFAULT_CHANNELS = [
    {"id": "android", "kind": "app", "platform": "android", "alias": "", "third_party": False, "label": "安卓", "field": "package", "placeholder": "com.example.app"},
    {"id": "ios", "kind": "app", "platform": "ios", "alias": "", "third_party": False, "label": "iOS", "field": "bundle", "placeholder": "com.example.app"},
    {"id": "web", "kind": "web", "platform": "web", "alias": "", "third_party": False, "label": "Web", "field": "base_url", "placeholder": "https://test.example.com"},
    {"id": "pc", "kind": "app", "platform": "pc", "alias": "", "third_party": False, "label": "PC", "field": "path", "placeholder": "安装路径或启动命令"},
    {"id": "mac", "kind": "app", "platform": "mac", "alias": "", "third_party": False, "label": "Mac", "field": "bundle", "placeholder": "com.example.desktop"},
    {"id": "server", "kind": "server", "platform": "server", "alias": "", "third_party": False, "label": "Server", "field": "base_url", "placeholder": "https://api.example.com"},
]

_PLATFORM_PRESET = {c["id"]: c for c in DEFAULT_CHANNELS}

_KIND_PREFIX = (
    ("android", "app", "android"),
    ("ios", "app", "ios"),
    ("pc", "app", "pc"),
    ("mac", "app", "mac"),
    ("server", "server", "server"),
    ("web", "web", "web"),
)

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
    }


def _infer_kind_platform(cid: str, kind: str = "", platform: str = "") -> Tuple[str, str]:
    k = str(kind or "").strip().lower()
    p = str(platform or "").strip().lower()
    if k in CHANNEL_KINDS and p in CHANNEL_PLATFORMS:
        return k, p
    if p in _PLATFORM_PRESET:
        preset = _PLATFORM_PRESET[p]
        return str(preset["kind"]), str(preset["platform"])
    if cid in _PLATFORM_PRESET:
        preset = _PLATFORM_PRESET[cid]
        return str(preset["kind"]), str(preset["platform"])
    for prefix, pk, pp in _KIND_PREFIX:
        if cid == prefix or cid.startswith(prefix + "-"):
            return pk, pp
    if k in CHANNEL_KINDS:
        fallback = {"app": "android", "web": "web", "server": "server"}[k]
        return k, p if p in CHANNEL_PLATFORMS else fallback
    return "web", "web"


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


def _norm_channel(raw: Any, seen: set) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    alias = str(raw.get("alias") or "").strip()[:24]
    third_raw = raw.get("third_party")
    third_party = bool(third_raw) if third_raw is not None else bool(alias)
    cid_in = _slug(raw.get("id") or raw.get("key") or "", "")
    kind, platform = _infer_kind_platform(
        cid_in,
        str(raw.get("kind") or ""),
        str(raw.get("platform") or ""),
    )
    preset = _PLATFORM_PRESET.get(platform) or _PLATFORM_PRESET.get(cid_in)
    # 旧数据：无简称的 android/ios/web 保持原 id，方便包名解析。有简称的不折回成唯一 web。
    if cid_in and cid_in not in seen:
        cid = cid_in
    elif not cid_in and not alias and preset and preset["id"] not in seen:
        cid = preset["id"]
    else:
        alias_slug = _slug(alias, "")
        if alias_slug and alias_slug != platform:
            cid = f"{platform}-{alias_slug}"
        else:
            cid = alias_slug or platform
        stem = cid or platform
        cid = stem
        n = 2
        while cid in seen:
            cid = f"{stem}-{n}"
            n += 1
    if not cid or cid in seen:
        return None
    field = _slug(raw.get("field") or (preset or {}).get("field") or "value", "value")
    if not field or field[0].isdigit():
        field = (preset or {}).get("field") or "value"
    label = str(raw.get("label") or alias or (preset or {}).get("label") or cid).strip()[:24] or cid
    if not alias and preset and label in {cid, preset["id"]}:
        label = preset["label"]
    placeholder = str(raw.get("placeholder") or (preset or {}).get("placeholder") or "").strip()[:80]
    seen.add(cid)
    return {
        "id": cid,
        "kind": kind,
        "platform": platform,
        "alias": alias,
        "third_party": bool(third_party),
        "label": alias or label,
        "field": field,
        "placeholder": placeholder,
    }


OTP_MODES = ("auto", "fixed", "adapter", "hitl")
PHONE_MODES = ("auto", "pool", "adapter", "hitl")


def default_env_secrets() -> dict:
    return {
        "otp": {
            "mode": "auto",
            "fixed": "",
            "adapter": "http",
            "adapter_url": "",
            "adapter_header": "",
        },
        "phone": {
            "mode": "auto",
            "adapter": "http",
            "adapter_url": "",
            "adapter_header": "",
        },
    }


def _norm_secret_slot(raw: Any, *, slot: str) -> dict:
    src = raw if isinstance(raw, dict) else {}
    modes = OTP_MODES if slot == "otp" else PHONE_MODES
    mode = str(src.get("mode") or "auto").strip().lower()
    if mode not in modes:
        mode = "auto"
    out = {
        "mode": mode,
        "adapter": str(src.get("adapter") or "http").strip()[:40] or "http",
        "adapter_url": str(src.get("adapter_url") or "").strip()[:400],
        "adapter_header": str(src.get("adapter_header") or "").strip()[:240],
    }
    if slot == "otp":
        out["fixed"] = str(src.get("fixed") or "").strip()[:32]
    return out


def _norm_env_secrets(raw: Any) -> dict:
    src = raw if isinstance(raw, dict) else {}
    return {
        "otp": _norm_secret_slot(src.get("otp"), slot="otp"),
        "phone": _norm_secret_slot(src.get("phone"), slot="phone"),
    }


def env_secrets(env_doc: dict | None, env_key: str = "") -> dict:
    key = _slug(env_key, "")
    for row in (env_doc or {}).get("environments") or []:
        if isinstance(row, dict) and str(row.get("key") or "") == key:
            return _norm_env_secrets(row.get("secrets"))
    return default_env_secrets()


def _norm_environment(raw: Any, seen: set) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    key = _slug(raw.get("key") or raw.get("id") or raw.get("label"), "")
    if not key or key in seen:
        return None
    label = str(raw.get("label") or ENV_PROFILE_LABELS.get(key) or key).strip()[:20] or key
    seen.add(key)
    return {"key": key, "label": label, "secrets": _norm_env_secrets(raw.get("secrets"))}


def _profile_value(block: Any, field: str) -> str:
    if not isinstance(block, dict):
        return str(block or "").strip() if block not in (None, "") else ""
    for k in (field, "value", "package", "bundle", "base_url", "path", "url", "host"):
        v = str(block.get(k) or "").strip()
        if v:
            return v
    return ""


def _norm_profile(raw: Any, channels: List[dict]) -> dict:
    src = raw if isinstance(raw, dict) else {}
    out = empty_profile(channels)
    for ch in channels:
        cid = ch["id"]
        field = ch["field"]
        out[cid][field] = _profile_value(src.get(cid), field)
    return out


def normalize_project_env(raw: Any) -> dict:
    """规范结构：environments + channels + pipeline + profiles。兼容旧四套 android/ios/web。"""
    if not isinstance(raw, dict):
        return default_project_env()

    profiles_in = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else None
    if profiles_in is None and any(k in raw for k in ("android", "ios", "web")):
        profiles_in = {"test": {k: raw[k] for k in ("android", "ios", "web") if isinstance(raw.get(k), dict)}}

    channels: List[dict] = []
    seen_ch: set = set()
    for row in raw.get("channels") or []:
        ch = _norm_channel(row, seen_ch)
        if ch:
            channels.append(ch)
    if not channels:
        inferred = set()
        if isinstance(profiles_in, dict):
            for snap in profiles_in.values():
                if isinstance(snap, dict):
                    inferred.update(k for k, v in snap.items() if isinstance(v, dict))
        if not inferred:
            inferred = {"android", "ios", "web"}
        for preset in DEFAULT_CHANNELS:
            if preset["id"] in inferred:
                channels.append(copy.deepcopy(preset))
        for cid in sorted(inferred):
            if cid not in seen_ch:
                ch = _norm_channel({"id": cid, "label": cid, "field": "value"}, seen_ch)
                if ch:
                    channels.append(ch)

    environments: List[dict] = []
    seen_env: set = set()
    for row in raw.get("environments") or []:
        env = _norm_environment(row, seen_env)
        if env:
            environments.append(env)
    if not environments:
        keys = list(profiles_in.keys()) if isinstance(profiles_in, dict) and profiles_in else list(ENV_PROFILE_KEYS)
        for key in keys:
            env = _norm_environment({"key": key, "label": ENV_PROFILE_LABELS.get(key, key)}, seen_env)
            if env:
                environments.append(env)
    if not environments:
        environments = [copy.deepcopy(e) for e in DEFAULT_ENVIRONMENTS]
        seen_env = {e["key"] for e in environments}

    pipeline: List[str] = []
    for item in raw.get("pipeline") or []:
        key = _slug(item, "")
        if key in seen_env and key not in pipeline:
            pipeline.append(key)
    if not pipeline:
        # 旧数据默认三步走：测试 → 预发 → 正式；其余环境可单独开测但不在上线路径里
        for key in ("test", "pre", "prod"):
            if key in seen_env:
                pipeline.append(key)
        if not pipeline:
            pipeline = [e["key"] for e in environments]

    default_profile = _slug(raw.get("default_profile") or "", "")
    if default_profile not in seen_env:
        default_profile = pipeline[0] if pipeline else environments[0]["key"]

    profiles = {}
    src_profiles = profiles_in or {}
    for env in environments:
        profiles[env["key"]] = _norm_profile(src_profiles.get(env["key"]), channels)

    from mino_nexus.services.account_facet_schema import normalize_extensions

    return {
        "default_profile": default_profile,
        "environments": environments,
        "channels": channels,
        "pipeline": pipeline,
        "profiles": profiles,
        "account_facet_extensions": normalize_extensions(raw.get("account_facet_extensions")),
        "account_template_ids": _norm_template_ids(raw.get("account_template_ids")),
        "account_pool_local": raw.get("account_pool_local")
        if isinstance(raw.get("account_pool_local"), dict)
        else {},
        "test_accounts": _norm_test_accounts(
            raw.get("test_accounts"),
            env_doc={
                "account_template_ids": _norm_template_ids(raw.get("account_template_ids")),
                "account_pool_local": raw.get("account_pool_local")
                if isinstance(raw.get("account_pool_local"), dict)
                else {},
            },
        ),
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
    """展示/检索用标识：display_name → account_id → 手机号 → 邮箱 → 用户名（手机号等可重复，非唯一键）。"""
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
        tags_legacy = item.get("tags") if isinstance(item.get("tags"), list) else []
        profile_id = str(item.get("profile_id") or "").strip()[:40]
        stored = item.get("facets") if isinstance(item.get("facets"), dict) else {}
        defaults = facets_defaults_from_catalog(env_doc)
        stored_lower = {
            k: str(v).strip().lower() for k, v in stored.items() if str(v).strip()
        }
        combined = {**defaults, **stored_lower}
        core = account_facets(
            {"tags": tags_legacy, "facets": combined, "profile_id": profile_id}
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
        prev_f = old.get("facets") if isinstance(old.get("facets"), dict) else {}
        merged_f = dict(prev_f)
        incoming = payload["facets"]
        for key in def_keys:
            if key != "health" and key not in incoming:
                merged_f.pop(key, None)
        merged_f.update(incoming)
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
    env_doc: dict | None,
    *,
    surface: str = "",
    platform: str = "",
    target_id: str = "",
    prompt: str = "",
    env_profile: str = "",
) -> str:
    """开跑/筛号时落到哪一条应用配置：显式 id > 启动标识匹配 > 设备端主应用 > 简称命中。"""
    doc = env_doc if isinstance(env_doc, dict) else {}
    channels = [c for c in (doc.get("channels") or []) if isinstance(c, dict) and c.get("id")]
    want = _slug(surface, "")
    if want and any(str(c.get("id")) == want for c in channels):
        return want
    tid = str(target_id or "").strip().rstrip("/")
    if tid:
        snap = profile_snapshot(doc, env_profile) if doc.get("profiles") or doc.get("environments") else {}
        for ch in channels:
            val = _profile_value(snap.get(ch["id"]) if isinstance(snap, dict) else {}, ch.get("field") or "value")
            if val and val.rstrip("/") == tid:
                return str(ch["id"])
    plat = str(platform or "").lower()
    kind = ""
    if plat in ("web", "browser", "playwright"):
        kind = "web"
    elif plat == "server":
        kind = "server"
    elif plat in ("android", "ios", "pc", "mac"):
        kind = "app"
    if kind:
        matching = [c for c in channels if str(c.get("kind") or "") == kind]
        if plat in ("android", "ios", "pc", "mac"):
            matching = [
                c for c in matching
                if str(c.get("platform") or "") == plat or str(c.get("id") or "") == plat
            ] or matching
        primaries = [c for c in matching if _surface_is_primary(c)]
        pool = primaries or matching
        if pool:
            return str(pool[0]["id"])
    q = str(prompt or "")
    alias_hits = []
    for ch in channels:
        alias = str(ch.get("alias") or "").strip()
        label = str(ch.get("label") or "").strip()
        if alias and alias in q:
            alias_hits.append(str(ch["id"]))
        elif ch.get("third_party") and label and len(label) >= 2 and label in q:
            alias_hits.append(str(ch["id"]))
    if len(set(alias_hits)) == 1:
        return alias_hits[0]
    return want


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
    env_key = str(req.get("env") or _slug(env, "") or infer_env_from_prompt(raw))
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    pool_defs = merged_pool_field_defs(env_doc) if env_doc else []
    return pick_accounts_by_requirements(
        rows,
        req,
        env=env_key,
        run_id=run_id,
        account_ident_fn=account_ident,
        observed_by_account=observed_by_account,
        ident_query=raw if re.fullmatch(r"[\d+\-@.\w]+", raw.replace(" ", "")) else "",
        field_defs=pool_defs,
    )


def profile_keys(env_doc: dict) -> List[str]:
    envs = env_doc.get("environments") if isinstance(env_doc, dict) else None
    if isinstance(envs, list) and envs:
        return [str(e.get("key")) for e in envs if isinstance(e, dict) and e.get("key")]
    profiles = (env_doc or {}).get("profiles") if isinstance(env_doc, dict) else {}
    if isinstance(profiles, dict) and profiles:
        return [str(k) for k in profiles.keys()]
    return list(ENV_PROFILE_KEYS)


def pipeline_keys(env_doc: dict) -> List[str]:
    keys = set(profile_keys(env_doc))
    pipe = env_doc.get("pipeline") if isinstance(env_doc, dict) else None
    if isinstance(pipe, list) and pipe:
        out = [str(k) for k in pipe if str(k) in keys]
        if out:
            return out
    return profile_keys(env_doc)


def resolve_profile_name(env_doc: dict, env_profile: Optional[str] = None) -> str:
    keys = set(profile_keys(env_doc))
    if env_profile and str(env_profile) in keys:
        return str(env_profile)
    default = str((env_doc or {}).get("default_profile") or "")
    if default in keys:
        return default
    pipe = pipeline_keys(env_doc)
    if pipe:
        return pipe[0]
    return next(iter(keys), "test")


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
    order = pipeline_keys(env_doc)
    for ch in channels or []:
        cid = str(ch.get("id") or "")
        field = str(ch.get("field") or "value")
        if cid not in ("android", "ios"):
            continue
        if _profile_value((snap.get(cid) if isinstance(snap.get(cid), dict) else {}), field):
            continue
        filled = _inherit_mobile_value(profiles, order, name, cid, field)
        if filled:
            snap.setdefault(cid, {})
            snap[cid][field] = filled
    return snap


def _inherit_mobile_value(profiles: dict, order: List[str], env_key: str, channel_id: str, field: str) -> str:
    keys = [k for k in (order or []) if k] or list((profiles or {}).keys())
    for key in keys:
        snap = profiles.get(key) if isinstance(profiles, dict) else None
        if not isinstance(snap, dict):
            continue
        v = _profile_value(snap.get(channel_id), field)
        if v:
            return v
    other = "ios" if channel_id == "android" else "android"
    other_field = "bundle" if other == "ios" else "package"
    for key in [env_key, *keys]:
        snap = profiles.get(key) if isinstance(profiles, dict) else None
        if not isinstance(snap, dict):
            continue
        v = _profile_value(snap.get(other), other_field)
        if v:
            return v
    return ""


def target_id_from_snapshot(snap: dict, platform: str = "android", surface: str = "") -> str:
    """从某一环境 profile 快照取启动标识：Android 包名 / iOS Bundle / Web 网址。"""
    data = snap if isinstance(snap, dict) else {}
    sid = str(surface or "").strip()
    if sid:
        block = data.get(sid)
        return _profile_value(block, "value") if isinstance(block, dict) else ""
    plat = str(platform or "android").lower()
    if plat in data and plat not in ("android", "ios", "web", "browser", "playwright", "iphone", "ipad"):
        block = data.get(plat)
        if isinstance(block, dict):
            val = _profile_value(block, "value")
            if val:
                return val
    if plat in ("web", "browser", "playwright"):
        web = data.get("web") if isinstance(data.get("web"), dict) else {}
        return str(web.get("base_url") or web.get("url") or "").strip()
    if plat in ("ios", "iphone", "ipad"):
        ios = data.get("ios") if isinstance(data.get("ios"), dict) else {}
        return str(ios.get("bundle") or ios.get("bundle_id") or "").strip()
    android = data.get("android") if isinstance(data.get("android"), dict) else {}
    pkg = str(android.get("package") or "").strip()
    if pkg:
        return pkg
    ios = data.get("ios") if isinstance(data.get("ios"), dict) else {}
    return str(ios.get("bundle") or ios.get("bundle_id") or "").strip()


def env_from_project_row(project: dict | None, apps: Optional[List[dict]] = None) -> dict:
    """从 JSON 行取出规范环境。空项目时尝试用第一个应用 env 填 test。"""
    if not isinstance(project, dict):
        return default_project_env()
    raw = project.get("env")
    doc = normalize_project_env(raw)
    if raw:
        return doc
    for app in apps or []:
        if isinstance(app, dict) and isinstance(app.get("env"), dict) and app.get("env"):
            doc["profiles"]["test"] = normalize_project_env(app.get("env"))["profiles"]["test"]
            break
    return doc
