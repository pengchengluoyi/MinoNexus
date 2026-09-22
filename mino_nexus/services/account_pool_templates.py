"""号池业务模板：Console 可编辑；项目启用；申请账号按模板匹配并下发凭证。"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Any

from mino_nexus.services.account_facet_schema import normalize_extensions
from mino_nexus.services.resource_pool import normalize_facets


def normalize_template_extensions(raw: Any) -> list[dict[str, Any]]:
    """模板字段定义：允许 lifecycle/session 等键（与项目扩展 normalize_extensions 不同）。"""
    from mino_nexus.services.account_facet_schema import _KEY_RE, _norm_option, _slug_key

    rows = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = _slug_key(str(item.get("key") or item.get("id") or ""))
        if not key or not _KEY_RE.match(key) or key in seen:
            continue
        if key in ACCOUNT_LEVEL_FACET_KEYS:
            continue
        label = str(item.get("label") or key).strip()[:24] or key
        opts = []
        for row in item.get("options") or []:
            o = _norm_option(row)
            if o and o["value"] not in {x["value"] for x in opts}:
                opts.append(o)
        if not opts:
            opts = [{"value": "unknown", "label": "未设置"}]
        seen.add(key)
        kind = str(item.get("data_kind") or "").strip().lower()
        if kind not in (FACET_DATA_KIND_STATIC, FACET_DATA_KIND_DYNAMIC):
            kind = FACET_DATA_KIND_STATIC
        out.append({
            "key": key,
            "label": label,
            "options": opts[:16],
            "help": str(item.get("help") or "").strip()[:200],
            "source": str(item.get("source") or "builtin").strip()[:16] or "builtin",
            "data_kind": kind,
        })
    return out[:32]

CATEGORIES = [
    {"id": "personal", "label": "个人"},
    {"id": "ecommerce", "label": "电商"},
    {"id": "content", "label": "内容"},
    {"id": "im", "label": "IM 通信"},
    {"id": "social", "label": "社交"},
    {"id": "flow", "label": "业务流转"},
]

DEFAULT_TEMPLATE_ID = "tpl_personal"

FACET_DATA_KIND_STATIC = "static"
FACET_DATA_KIND_DYNAMIC = "dynamic"

FACET_DATA_KINDS: dict[str, dict[str, str]] = {
    FACET_DATA_KIND_STATIC: {
        "label": "静态侧写",
        "help": "描述账号在某业务线上的快照（有无资料、是否填地址等）；运维或 Studio 可直接改值，无强制步骤顺序。",
    },
    FACET_DATA_KIND_DYNAMIC: {
        "label": "动态流转",
        "help": "注册/登录/审核等流程态；选项顺序即推荐流转路径，写回须满足转移规则（用例、能力、手动）。",
    },
}

# 账号级维度（五维中的注册/登录/健康），不出现在业务模板字段里
ACCOUNT_LEVEL_FACET_KEYS = frozenset({"lifecycle", "session", "health"})

TEMPLATE_GUIDE = (
    "业务模板分两类字段：静态侧写（资料/地址/收藏等快照）与动态流转（注册/登录/审核等流程态）。"
    "同一测试账号可并行启用多个模板；注册、登录、健康在账号级维护。"
)

_EXT_LIFECYCLE = {
    "key": "lifecycle",
    "label": "注册",
    "options": [
        {"value": "unknown", "label": "未设置"},
        {"value": "unregistered", "label": "未注册"},
        {"value": "registered", "label": "已注册"},
    ],
}
_EXT_SESSION = {
    "key": "session",
    "label": "登录",
    "options": [
        {"value": "unknown", "label": "未设置"},
        {"value": "logged_out", "label": "未登录"},
        {"value": "logged_in", "label": "已登录"},
        {"value": "guest", "label": "游客"},
    ],
}
_EXT_HEALTH = {
    "key": "health",
    "label": "健康",
    "options": [
        {"value": "available", "label": "可租用"},
        {"value": "dirty", "label": "待重置"},
        {"value": "quarantine", "label": "隔离"},
        {"value": "bad", "label": "坏号"},
    ],
}
_EXT_PROFILE = {
    "key": "profile_data",
    "label": "资料",
    "options": [
        {"value": "unknown", "label": "未设置"},
        {"value": "none", "label": "无"},
        {"value": "filled", "label": "已完善"},
    ],
}
_EXT_ADDRESS = {
    "key": "address",
    "label": "地址",
    "options": [
        {"value": "unknown", "label": "未设置"},
        {"value": "none", "label": "无"},
        {"value": "filled", "label": "已填写"},
    ],
}


# 账号「基础信息」编辑，不属于模板字段
ACCOUNT_BASIC_FACET_KEYS = frozenset({"health"})
ACCOUNT_HEALTH_FIELD: dict[str, Any] = dict(_EXT_HEALTH)
ACCOUNT_CORE_FACET_FIELDS: list[dict[str, Any]] = [
    {**dict(_EXT_LIFECYCLE), "data_kind": FACET_DATA_KIND_DYNAMIC},
    {**dict(_EXT_SESSION), "data_kind": FACET_DATA_KIND_DYNAMIC},
    {**dict(_EXT_HEALTH), "data_kind": FACET_DATA_KIND_STATIC},
]


def _flow_options(steps: list[tuple[str, str]]) -> list[dict[str, str]]:
    opts: list[dict[str, str]] = [{"value": "unknown", "label": "未设置"}]
    for val, label in steps:
        opts.append({"value": val, "label": label})
    return opts


def _dynamic_field(key: str, label: str, steps: list[tuple[str, str]], *, help: str = "") -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "options": _flow_options(steps),
        "data_kind": FACET_DATA_KIND_DYNAMIC,
        "help": help[:200],
        "source": "builtin",
    }


DYNAMIC_FLOW_STARTERS: list[dict[str, Any]] = [
    _dynamic_field(
        "register_flow",
        "注册流程",
        [
            ("not_started", "未开始"),
            ("phone_entered", "已填手机号"),
            ("sms_ok", "短信验证通过"),
            ("registered", "注册完成"),
        ],
        help="App 内注册步骤；完成时可对齐账号级 lifecycle=registered",
    ),
    _dynamic_field(
        "login_flow",
        "登录流程",
        [
            ("logged_out", "未登录"),
            ("credential_ok", "凭证已提交"),
            ("otp_ok", "验证码通过"),
            ("logged_in", "已登录"),
        ],
        help="登录中间态；终态 logged_in 时可对齐账号级 session=logged_in",
    ),
    _dynamic_field(
        "audit_flow",
        "审核流程",
        [
            ("none", "无"),
            ("submitted", "已提交"),
            ("reviewing", "审核中"),
            ("approved", "已通过"),
            ("rejected", "已拒绝"),
        ],
    ),
    _dynamic_field(
        "kyc_flow",
        "实名/KYC",
        [
            ("none", "未实名"),
            ("pending", "审核中"),
            ("verified", "已通过"),
            ("failed", "未通过"),
        ],
    ),
]


def _strip_account_level_from_template(tpl: dict[str, Any]) -> dict[str, Any]:
    """Catalog 展示：去掉注册/登录/健康，只留业务扩展字段。"""
    out = dict(tpl)
    defs = []
    for ext in normalize_template_extensions(out.get("facet_extensions") or []):
        key = str(ext.get("key") or "")
        if key in ACCOUNT_LEVEL_FACET_KEYS:
            continue
        defs.append(ext)
    out["facet_extensions"] = defs
    shipped = []
    for ext in normalize_template_extensions(out.get("builtin_facet_extensions") or []):
        key = str(ext.get("key") or "")
        if key in ACCOUNT_LEVEL_FACET_KEYS:
            continue
        shipped.append(ext)
    if shipped:
        out["builtin_facet_extensions"] = shipped
    df = normalize_facets(out.get("default_facets"))
    out["default_facets"] = {k: v for k, v in df.items() if k not in ACCOUNT_LEVEL_FACET_KEYS}
    return out


def _base_extensions(*, profile: bool = False, address: bool = False) -> list[dict[str, Any]]:
    exts: list[dict[str, Any]] = []
    if profile:
        exts.append(dict(_EXT_PROFILE))
    if address:
        exts.append(dict(_EXT_ADDRESS))
    return exts


def facets_defaults_from_template(template: dict[str, Any] | None) -> dict[str, str]:
    tpl = template if isinstance(template, dict) else {}
    out: dict[str, str] = {}
    for key, val in (tpl.get("default_facets") or {}).items():
        out[str(key)] = str(val).strip().lower()
    for ext in normalize_template_extensions(tpl.get("facet_extensions")):
        key = str(ext.get("key") or "").strip()
        if not key or key in out:
            continue
        opts = [o for o in (ext.get("options") or []) if isinstance(o, dict)]
        val = "unknown"
        for o in opts:
            if str(o.get("value") or "") == "unknown":
                val = "unknown"
                break
        else:
            val = str(opts[0].get("value") or "unknown") if opts else "unknown"
        out[key] = val
    return out


_BUILTIN: list[dict[str, Any]] = [
    {
        "id": "tpl_personal",
        "category": "personal",
        "label": "个人账号",
        "description": "同一账号的身份侧写：头像、绑定手机/邮箱等（不含注册/登录状态）",
        "scope": "business",
        "builtin": True,
        "credential_fields": ["phone", "email", "username", "password", "otp"],
        "default_facets": {},
        "facet_extensions": _base_extensions()
        + [
            {
                "key": "avatar_ready",
                "label": "头像",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "default", "label": "默认"},
                    {"value": "custom", "label": "已自定义"},
                ],
            },
            {
                "key": "phone_bound",
                "label": "手机号",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "bound", "label": "已绑定"},
                    {"value": "none", "label": "未绑定"},
                ],
            },
            {
                "key": "email_bound",
                "label": "邮箱",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "bound", "label": "已绑定"},
                    {"value": "none", "label": "未绑定"},
                ],
            },
        ],
    },
    {
        "id": "tpl_ecommerce",
        "category": "ecommerce",
        "label": "电商买家",
        "description": "同一账号在电商业务线的侧写：收货地址、订单、购物车等",
        "scope": "business",
        "builtin": True,
        "credential_fields": ["phone", "email", "password", "otp"],
        "default_facets": {
            "profile_data": "filled",
            "address": "filled",
        },
        "facet_extensions": _base_extensions(profile=True, address=True)
        + [
            {
                "key": "shipping_address",
                "label": "收货地址",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "none", "label": "无"},
                    {"value": "filled", "label": "已填写"},
                ],
            },
            {
                "key": "order_history",
                "label": "已购订单",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "empty", "label": "无订单"},
                    {"value": "has_orders", "label": "有历史订单"},
                ],
            },
            {
                "key": "cart_state",
                "label": "购物车",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "empty", "label": "空"},
                    {"value": "has_items", "label": "有商品"},
                ],
            },
        ],
    },
    {
        "id": "tpl_content",
        "category": "content",
        "label": "内容消费",
        "description": "同一账号在内容场景的侧写：收藏、点赞、阅读记录",
        "scope": "business",
        "builtin": True,
        "credential_fields": ["phone", "email", "password"],
        "default_facets": {},
        "facet_extensions": _base_extensions()
        + [
            {
                "key": "favorite_posts",
                "label": "收藏",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "none", "label": "无"},
                    {"value": "has", "label": "有收藏"},
                ],
            },
            {
                "key": "like_activity",
                "label": "点赞",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "none", "label": "无"},
                    {"value": "active", "label": "有互动"},
                ],
            },
            {
                "key": "read_history",
                "label": "阅读",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "empty", "label": "无"},
                    {"value": "has", "label": "有记录"},
                ],
            },
        ],
    },
    {
        "id": "tpl_im",
        "category": "im",
        "label": "IM 会话",
        "description": "同一账号在 IM 场景的侧写：会话与聊天记录",
        "scope": "business",
        "builtin": True,
        "credential_fields": ["phone", "username", "password"],
        "default_facets": {},
        "facet_extensions": _base_extensions()
        + [
            {
                "key": "chat_history",
                "label": "聊天记录",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "empty", "label": "无"},
                    {"value": "has", "label": "有记录"},
                ],
            },
        ],
    },
    {
        "id": "tpl_social",
        "category": "social",
        "label": "社交关系",
        "description": "同一账号在社交场景的侧写：关注、粉丝、互关",
        "scope": "business",
        "builtin": True,
        "credential_fields": ["phone", "email", "password"],
        "default_facets": {"profile_data": "filled"},
        "facet_extensions": _base_extensions(profile=True)
        + [
            {
                "key": "follow_state",
                "label": "关注",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "none", "label": "未关注"},
                    {"value": "following", "label": "已关注"},
                ],
            },
            {
                "key": "fans_state",
                "label": "粉丝",
                "options": [
                    {"value": "unknown", "label": "未设置"},
                    {"value": "none", "label": "无粉丝"},
                    {"value": "has_fans", "label": "有粉丝"},
                ],
            },
        ],
    },
    {
        "id": "tpl_dynamic",
        "category": "flow",
        "label": "账号业务流转",
        "description": "注册/登录/审核/KYC 等流程态（有序流转，与静态侧写模板并行启用）",
        "scope": "flow",
        "builtin": True,
        "credential_fields": ["phone", "email", "password", "otp"],
        "default_facets": {},
        "facet_extensions": [dict(x) for x in DYNAMIC_FLOW_STARTERS],
    },
]


def _norm_template(raw: dict[str, Any], seen: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    tid = str(raw.get("id") or "").strip()[:48]
    if not tid or tid in seen:
        return None
    seen.add(tid)
    cat = str(raw.get("category") or "personal").strip()[:24]
    ext = [
        e
        for e in normalize_template_extensions(raw.get("facet_extensions") or [])
        if str(e.get("key") or "") not in ACCOUNT_BASIC_FACET_KEYS
        and str(e.get("key") or "") not in ACCOUNT_LEVEL_FACET_KEYS
    ]
    creds = [str(x).strip() for x in (raw.get("credential_fields") or []) if str(x).strip()]
    if not creds:
        creds = ["phone", "email", "password", "otp"]
    return {
        "id": tid,
        "category": cat,
        "label": str(raw.get("label") or tid).strip()[:48],
        "description": str(raw.get("description") or "").strip()[:400],
        "builtin": bool(raw.get("builtin")),
        "enabled": bool(raw.get("enabled", True)),
        "credential_fields": creds[:12],
        "default_facets": normalize_facets(raw.get("default_facets")),
        "facet_extensions": ext,
    }


def _builtin_ids() -> set[str]:
    return {str(t["id"]) for t in _BUILTIN}


def _builtin_shipped_facet_extensions(template_id: str) -> list[dict[str, Any]]:
    tid = str(template_id or "").strip()
    for row in _BUILTIN:
        if str(row.get("id") or "") != tid:
            continue
        seen: set[str] = set()
        n = _norm_template(row, seen)
        if not n:
            return []
        return normalize_template_extensions(n.get("facet_extensions") or [])
    return []


def _facet_def_equiv(a: dict[str, Any], b: dict[str, Any]) -> bool:
    aa = normalize_template_extensions([a])
    bb = normalize_template_extensions([b])
    if not aa or not bb:
        return False
    return aa[0] == bb[0]


def _apply_extension_addons(tpl: dict[str, Any], addons: list[dict] | None) -> dict[str, Any]:
    if not addons:
        return tpl
    merged = [dict(e) for e in normalize_template_extensions(tpl.get("facet_extensions") or [])]
    index_by_key = {str(e.get("key") or ""): i for i, e in enumerate(merged)}
    for ext in normalize_template_extensions(addons):
        key = str(ext.get("key") or "")
        if not key or key in ACCOUNT_BASIC_FACET_KEYS:
            continue
        if key in index_by_key:
            merged[index_by_key[key]] = ext
        else:
            index_by_key[key] = len(merged)
            merged.append(ext)
    out = dict(tpl)
    out["facet_extensions"] = merged
    return out


def merge_template_catalog(
    custom: list[dict] | None,
    extension_addons: dict[str, list[dict]] | None = None,
) -> list[dict[str, Any]]:
    addons_map = extension_addons if isinstance(extension_addons, dict) else {}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in _BUILTIN:
        n = _norm_template(row, seen)
        if n:
            n["builtin"] = True
            n["builtin_facet_extensions"] = normalize_template_extensions(n.get("facet_extensions") or [])
            n = _apply_extension_addons(n, addons_map.get(n["id"]))
            out.append(n)
    for row in custom or []:
        n = _norm_template(row, seen)
        if n:
            n["builtin"] = bool(row.get("builtin"))
            if not n.get("facet_extensions"):
                n["facet_extensions"] = _base_extensions()
            out.append(n)
    return out


def _settings_block() -> dict[str, Any]:
    from mino_nexus.services import settings_store as ss

    root = ss._root()
    block = root.get("account_pool_templates")
    return block if isinstance(block, dict) else {}


def get_custom_templates_from_settings() -> list[dict]:
    rows = _settings_block().get("templates")
    return [x for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []


def get_extension_addons_from_settings() -> dict[str, list[dict]]:
    raw = _settings_block().get("extension_addons")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for tid, rows in raw.items():
        key = str(tid or "").strip()
        if not key or key not in _builtin_ids():
            continue
        if isinstance(rows, list):
            out[key] = [x for x in rows if isinstance(x, dict)]
    return out


def save_template_catalog(
    templates: list[dict] | None,
    *,
    extension_addons: dict[str, list[dict]] | None = None,
) -> dict[str, Any]:
    from mino_nexus.services import settings_store as ss

    root = ss._root()
    seen: set[str] = set(_builtin_ids())
    custom: list[dict] = []
    for row in templates or []:
        if not isinstance(row, dict) or bool(row.get("builtin")):
            continue
        n = _norm_template(row, seen)
        if n:
            n["builtin"] = False
            if not n.get("facet_extensions"):
                n["facet_extensions"] = []
            custom.append(n)
    addons_out: dict[str, list[dict]] = {}
    if isinstance(extension_addons, dict):
        for tid, rows in extension_addons.items():
            key = str(tid or "").strip()
            if key not in _builtin_ids() or not isinstance(rows, list):
                continue
            normed = normalize_template_extensions(rows)
            if normed:
                addons_out[key] = normed
    root["account_pool_templates"] = {
        "templates": custom,
        "extension_addons": addons_out,
        "version": 2,
    }
    ss._save(root)
    return {"templates": custom, "extension_addons": addons_out}


def upsert_custom_template(row: dict[str, Any]) -> dict[str, Any]:
    """只 upsert 一条自定义模板，不覆盖其它模板或 extension_addons。"""
    from mino_nexus.services import settings_store as ss

    if not isinstance(row, dict) or bool(row.get("builtin")):
        raise ValueError("仅支持非内置自定义模板")
    root = ss._root()
    block = root.get("account_pool_templates")
    if not isinstance(block, dict):
        block = {"templates": [], "extension_addons": {}, "version": 2}
    existing = [x for x in (block.get("templates") or []) if isinstance(x, dict)]
    tid = str(row.get("id") or "").strip()
    seen: set[str] = set(_builtin_ids())
    for t in existing:
        if str(t.get("id") or "") != tid:
            seen.add(str(t.get("id") or ""))
    n = _norm_template(row, seen)
    if not n:
        raise ValueError("模板无效")
    n["builtin"] = False
    if len(n.get("facet_extensions") or []) < 3:
        n["facet_extensions"] = normalize_template_extensions(n.get("facet_extensions") or [])
    out_list: list[dict] = []
    replaced = False
    for t in existing:
        if str(t.get("id") or "") == str(n.get("id") or ""):
            out_list.append(n)
            replaced = True
        else:
            out_list.append(t)
    if not replaced:
        out_list.append(n)
    block["templates"] = out_list
    block["version"] = 2
    root["account_pool_templates"] = block
    ss._save(root)
    return n


def upsert_extension_addon(template_id: str, fields: list[dict] | None) -> list[dict[str, Any]]:
    """保存内置模板的字段定制：覆盖同名内置字段或追加新 key（相对出厂定义 diff 落盘）。"""
    from mino_nexus.services import settings_store as ss

    tid = str(template_id or "").strip()
    if tid not in _builtin_ids():
        raise ValueError("仅支持内置模板字段定制")
    edited = normalize_template_extensions(fields or [])
    shipped = _builtin_shipped_facet_extensions(tid)
    shipped_by = {str(e.get("key") or ""): e for e in shipped}
    stored: list[dict[str, Any]] = []
    for ext in edited:
        key = str(ext.get("key") or "")
        if not key:
            continue
        base = shipped_by.get(key)
        if base is not None:
            if _facet_def_equiv(ext, base):
                continue
            stored.append(ext)
        else:
            stored.append(ext)
    root = ss._root()
    block = root.get("account_pool_templates")
    if not isinstance(block, dict):
        block = {"templates": [], "extension_addons": {}, "version": 2}
    addons = block.get("extension_addons")
    if not isinstance(addons, dict):
        addons = {}
    if stored:
        addons[tid] = stored
    else:
        addons.pop(tid, None)
    block["extension_addons"] = addons
    block["version"] = 2
    root["account_pool_templates"] = block
    ss._save(root)
    return stored


def delete_custom_template(template_id: str) -> bool:
    from mino_nexus.services import settings_store as ss

    tid = str(template_id or "").strip()
    if not tid or tid in _builtin_ids():
        return False
    root = ss._root()
    block = root.get("account_pool_templates")
    if not isinstance(block, dict):
        return False
    existing = [x for x in (block.get("templates") or []) if isinstance(x, dict)]
    nxt = [t for t in existing if str(t.get("id") or "") != tid]
    if len(nxt) == len(existing):
        return False
    block["templates"] = nxt
    root["account_pool_templates"] = block
    ss._save(root)
    return True


def save_custom_templates(templates: list[dict]) -> list[dict]:
    saved = save_template_catalog(templates)
    return saved["templates"]


def list_templates_catalog(*, include_disabled: bool = False) -> dict[str, Any]:
    custom = get_custom_templates_from_settings()
    addons = get_extension_addons_from_settings()
    templates = merge_template_catalog(custom, addons)
    if not include_disabled:
        templates = [t for t in templates if t.get("enabled", True)]
    return {
        "guide": TEMPLATE_GUIDE,
        "categories": CATEGORIES,
        "facet_data_kinds": FACET_DATA_KINDS,
        "dynamic_flow_starters": [dict(x) for x in DYNAMIC_FLOW_STARTERS],
        "templates": [_strip_account_level_from_template(t) for t in templates],
        "extension_addons": addons,
        "starter_extensions": [],
        "account_core_facets": ACCOUNT_CORE_FACET_FIELDS,
        "account_health_field": ACCOUNT_HEALTH_FIELD,
    }


def get_template(template_id: str, env_doc: dict | None = None) -> dict[str, Any] | None:
    tid = str(template_id or "").strip()
    if not tid:
        return None
    if env_doc is not None:
        from mino_nexus.services.project_account_pool import get_template_in_project

        hit = get_template_in_project(tid, env_doc)
        if hit:
            return hit
    for row in merge_template_catalog(
        get_custom_templates_from_settings(),
        get_extension_addons_from_settings(),
    ):
        if str(row.get("id")) == tid:
            return row
    return None


def project_enabled_templates(env_doc: dict | None) -> list[str]:
    doc = env_doc if isinstance(env_doc, dict) else {}
    raw = doc.get("account_template_ids")
    if isinstance(raw, list) and raw:
        return [str(x).strip() for x in raw if str(x).strip()]
    return [t["id"] for t in merge_template_catalog(get_custom_templates_from_settings()) if t.get("enabled", True)]


def template_facet_extensions(template: dict[str, Any] | None) -> list[dict]:
    tpl = template if isinstance(template, dict) else {}
    return normalize_template_extensions(tpl.get("facet_extensions"))


def merged_template_field_defs(env_doc: dict | None = None) -> list[dict[str, Any]]:
    """项目启用的全部模板字段合并；账号共享同一套维度，不按账号绑唯一模板。"""
    from mino_nexus.services.project_account_pool import list_templates_for_project

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for tpl in list_templates_for_project(env_doc):
        for ext in template_facet_extensions(tpl):
            key = str(ext.get("key") or "")
            if not key or key in seen or key in ACCOUNT_BASIC_FACET_KEYS:
                continue
            if key in ACCOUNT_LEVEL_FACET_KEYS:
                continue
            seen.add(key)
            out.append(ext)
    return out


def merged_pool_field_defs(env_doc: dict | None = None) -> list[dict[str, Any]]:
    """模板字段 + 项目级 account_facet_extensions（Studio/Console 自定义扩展）。"""
    from mino_nexus.services.account_facet_schema import extensions_from_env

    out = list(merged_template_field_defs(env_doc))
    seen = {str(x.get("key") or "") for x in out}
    for ext in extensions_from_env(env_doc):
        key = str(ext.get("key") or "")
        if not key or key in seen or key in ACCOUNT_BASIC_FACET_KEYS:
            continue
        seen.add(key)
        out.append(ext)
    return out


def facets_defaults_from_catalog(env_doc: dict | None = None) -> dict[str, str]:
    from mino_nexus.services.project_account_pool import list_templates_for_project

    out: dict[str, str] = {}
    for tpl in list_templates_for_project(env_doc):
        for key, val in facets_defaults_from_template(tpl).items():
            if key not in out:
                out[key] = val
    return out


def new_account_id() -> str:
    return f"acc_{uuid.uuid4().hex[:12]}"


def apply_template_to_account_row(row: dict[str, Any], template: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(row or {})
    if not out.get("account_id"):
        out["account_id"] = str(out.get("id") or new_account_id())
    out["id"] = str(out["account_id"])
    if template:
        out["template_id"] = str(template.get("id") or "")
        base = facets_defaults_from_template(template)
        cur = out.get("facets") if isinstance(out.get("facets"), dict) else {}
        merged = {**base, **{k: str(v).strip().lower() for k, v in cur.items() if str(v).strip()}}
        out["facets"] = merged
    return out


_TEMPLATE_ID_RE = re.compile(
    r"(?:template_id|account_template|号池模板)\s*[=:：]\s*([a-zA-Z0-9_-]+)",
    re.I,
)
_TEMPLATE_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("tpl_ecommerce", re.compile(r"电商|购物车|已购|订单|收货地址|收货", re.I)),
    ("tpl_content", re.compile(r"收藏帖|收藏|点赞|阅读记录|阅读历史", re.I)),
    ("tpl_im", re.compile(r"IM|聊天|消息记录|会话列表", re.I)),
    ("tpl_social", re.compile(r"关注|粉丝|互关|社交关系", re.I)),
    ("tpl_personal", re.compile(r"头像|个人资料|绑定手机|绑定邮箱", re.I)),
    ("tpl_dynamic", re.compile(r"注册流程|登录流程|审核|KYC|实名|流转", re.I)),
]


def infer_template_id_from_text(text: str) -> str:
    blob = str(text or "").strip()
    if not blob:
        return ""
    m = _TEMPLATE_ID_RE.search(blob)
    if m:
        return str(m.group(1) or "").strip()[:48]
    for tid, pat in _TEMPLATE_HINTS:
        if pat.search(blob):
            return tid
    return ""


def augment_requirements_with_template(
    req: dict[str, Any],
    template_id: str,
    env_doc: dict | None = None,
) -> dict[str, Any]:
    """模板默认 facet 并入 DSL：扩展字段默认必满足；五维仅在前置未约束时追加。"""
    from mino_nexus.services.resource_pool import DEFAULT_FACETS

    tid = str(template_id or "").strip()
    out = dict(req or {})
    if not tid:
        return out
    tpl = get_template(tid, env_doc)
    if not tpl:
        return out
    defaults = facets_defaults_from_template(tpl)
    all_clauses = list(out.get("all") or [])
    covered = {
        str(c.get("facet") or "").strip()
        for c in all_clauses
        if isinstance(c, dict) and str(c.get("facet") or "").strip()
    }
    for key, val in defaults.items():
        facet = str(key or "").strip()
        if not facet or facet in covered:
            continue
        if facet in DEFAULT_FACETS:
            continue
        all_clauses.append({"facet": facet, "op": "eq", "value": str(val).strip().lower()})
    out["all"] = all_clauses
    return out


def merge_need_requirements(
    base: dict[str, Any],
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """合并 CaseScene.lease_requirements 与 event params.requirements。"""
    out = dict(base or {})
    if not isinstance(extra, dict):
        return out
    for key in ("env", "template_id"):
        if extra.get(key) and not out.get(key):
            out[key] = extra[key]
    for bucket in ("all", "prefer"):
        rows = list(out.get(bucket) or [])
        seen = {str(c.get("facet") or "") for c in rows if isinstance(c, dict)}
        for clause in extra.get(bucket) or []:
            if not isinstance(clause, dict):
                continue
            facet = str(clause.get("facet") or "").strip()
            if facet and facet in seen:
                continue
            rows.append(clause)
            if facet:
                seen.add(facet)
        out[bucket] = rows
    return out


def credentials_grant(row: dict[str, Any], template: dict[str, Any] | None) -> dict[str, Any]:
    tpl = template or {}
    fields = tpl.get("credential_fields") or ["phone", "email", "username", "password", "otp"]
    grant: dict[str, Any] = {
        "account_id": str(row.get("account_id") or row.get("id") or ""),
        "template_id": str(row.get("template_id") or tpl.get("id") or ""),
        "env": str(row.get("env") or ""),
        "display_name": str(row.get("display_name") or row.get("name") or ""),
    }
    for key in fields:
        if key in row and row.get(key):
            grant[key] = row.get(key)
    grant["facets"] = dict(row.get("facets") or {})
    return grant
