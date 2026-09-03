"""集成插件（飞书 / 禅道 / Figma / IM）。

- Console：`plugin_policy.json` —— 客户端是否展示、是否启用。不存密钥。
- Studio：`users/<user_id>/plugins.json` —— 当前登录用户自己的 key。
- 执行任务时按发起用户的 user_id 取 key，不共用 settings.json。

仓库 `plugins/**.yaml` 是能力目录，给扩展包用。
"""
from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Optional

from mino_nexus import settings_store as ss
from mino_nexus.json_store import load_json, save_json

_POLICY_FILE = "plugin_policy.json"
_LEGACY_MIGRATED = False

IM_PLUGIN_IDS = ("feishu", "wecom", "dingtalk", "slack", "wechat")

INTEGRATION_PLUGIN_CATEGORIES: list[dict[str, str]] = [
    {"id": "docs", "label": "文档", "desc": "Wiki 副本"},
    {"id": "im", "label": "IM", "desc": "群通知、对话与提缺陷"},
    {"id": "defect", "label": "缺陷", "desc": "缺陷库同步"},
    {"id": "design", "label": "设计", "desc": "设计稿学习"},
]

INTEGRATION_PLUGIN_SPECS: list[dict[str, Any]] = [
    {
        "id": "feishu",
        "name": "飞书",
        "kind": "docs",
        "categories": ["docs", "im"],
        "color": "#2563eb",
        "summary": "Wiki、群通知和收发消息。",
        "robot_platform": "lark",
        "capabilities": [
            {"id": "connect", "label": "连接", "desc": "应用凭证", "categories": ["docs", "im"]},
            {"id": "wiki", "label": "Wiki", "desc": "按版本建文件夹", "categories": ["docs"]},
            {"id": "notify", "label": "通知", "desc": "失败与待办推群", "categories": ["im"]},
            {"id": "chat", "label": "对话", "desc": "收消息并回复", "categories": ["im"]},
        ],
    },
    {
        "id": "zentao",
        "name": "禅道",
        "kind": "defect",
        "categories": ["defect"],
        "color": "#f59e0b",
        "summary": "自动化失败与手工发现的缺陷，同步到禅道。",
        "capabilities": [
            {"id": "connect", "label": "连接", "desc": "地址、账号换 Token", "categories": ["defect"]},
            {"id": "bind", "label": "产品绑定", "desc": "项目对应禅道产品", "categories": ["defect"]},
            {"id": "flow", "label": "提单规则", "desc": "失败如何进缺陷", "categories": ["defect"]},
            {"id": "templates", "label": "提单模板", "desc": "标题和描述怎么填", "categories": ["defect"]},
        ],
    },
    {
        "id": "figma",
        "name": "Figma",
        "kind": "design",
        "categories": ["design"],
        "color": "#a259ff",
        "summary": "用设计稿学习页面结构。Token 在这里，文件链接按应用绑定。",
        "capabilities": [
            {"id": "connect", "label": "连接", "desc": "Personal Access Token", "categories": ["design"]},
            {"id": "bind", "label": "应用绑定", "desc": "每个应用的设计稿", "categories": ["design"]},
        ],
    },
    {
        "id": "wecom",
        "name": "企业微信",
        "kind": "im",
        "categories": ["im"],
        "color": "#10b981",
        "summary": "群机器人 webhook。收消息稍后接入。",
        "robot_platform": "wecom",
        "capabilities": [
            {"id": "connect", "label": "连接", "desc": "Webhook", "categories": ["im"]},
            {"id": "chat", "label": "对话", "desc": "试对话", "categories": ["im"]},
        ],
    },
    {
        "id": "dingtalk",
        "name": "钉钉",
        "kind": "im",
        "categories": ["im"],
        "color": "#0ea5e9",
        "summary": "群机器人。收消息稍后接入。",
        "robot_platform": "dingtalk",
        "capabilities": [
            {"id": "connect", "label": "连接", "desc": "Webhook", "categories": ["im"]},
            {"id": "chat", "label": "对话", "desc": "试对话", "categories": ["im"]},
        ],
    },
    {
        "id": "slack",
        "name": "Slack",
        "kind": "im",
        "categories": ["im"],
        "color": "#8b5cf6",
        "summary": "频道消息。收消息稍后接入。",
        "robot_platform": "slack",
        "capabilities": [
            {"id": "connect", "label": "连接", "desc": "Webhook / Bot Token", "categories": ["im"]},
            {"id": "chat", "label": "对话", "desc": "试对话", "categories": ["im"]},
        ],
    },
    {
        "id": "wechat",
        "name": "微信",
        "kind": "im",
        "categories": ["im"],
        "color": "#07c160",
        "summary": "个人微信。扫码登录尚未搬到 Nexus，配置可以先存。",
        "capabilities": [
            {"id": "connect", "label": "连接", "desc": "微信扫码绑定", "categories": ["im"]},
            {"id": "chat", "label": "对话", "desc": "收消息并回复", "categories": ["im"]},
        ],
    },
]

ROBOT_PLATFORM_PRESETS: dict[str, dict[str, Any]] = {
    "lark": {
        "label": "飞书",
        "required": ["app_id", "app_secret"],
        "secret_fields": ["app_secret", "encrypt_key", "verification_token"],
    },
    "wecom": {
        "label": "企业微信",
        "required": ["webhook_url"],
        "secret_fields": ["webhook_url", "secret"],
    },
    "dingtalk": {
        "label": "钉钉",
        "required": ["webhook_url"],
        "secret_fields": ["webhook_url", "secret"],
    },
    "slack": {
        "label": "Slack",
        "required": ["webhook_url"],
        "secret_fields": ["webhook_url", "bot_token"],
    },
}

ZENTAO_BUG_TYPES = (
    "codeerror", "config", "install", "security", "performance",
    "standard", "automation", "designdefect", "others",
)
DEFAULT_ZENTAO_TITLE_TEMPLATE = "[{project}] {title}"
DEFAULT_ZENTAO_STEPS_TEMPLATE = (
    "【项目】{project}\n【应用】{app}\n【版本】{version}\n【模块】{module}\n"
    "【用例】{case}\n【环境】{env}\n\n【重现步骤】\n{steps}\n\n【期望】\n{expected}\n\n"
    "【实际】\n{actual}\n\n【来源】Mino {run}\n"
)

WECHAT_NOT_PORTED = (
    "微信扫码尚未搬到 Nexus。插件条目和已保存的开关是真的，但不能在这里扫码登录。"
)
FEISHU_LIVE_NOT_PORTED = (
    "飞书长连接 / Wiki 调试尚未搬到 Nexus。凭证和 Wiki 配置可以保存，长连接收消息还不能在这里开。"
)
ZENTAO_BUG_NOT_PORTED = "禅道建测试单尚未搬到 Nexus。连通测试和 Token 换取可以用。"


def _plugin_spec(plugin_id: str) -> Optional[dict[str, Any]]:
    return next((x for x in INTEGRATION_PLUGIN_SPECS if x["id"] == plugin_id), None)


def _deep_merge(base: dict[str, Any], overlay: Optional[dict[str, Any]]) -> dict[str, Any]:
    out = dict(base or {})
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _empty_user_root() -> dict[str, Any]:
    return {"integrations": {}, "robots": {"items": []}, "figma": {}}


def _user_plugins_path(user_id: str) -> str:
    return f"users/{ss._require_user_id(user_id)}/plugins.json"


def _normalize_user_root(raw: Any) -> dict[str, Any]:
    root = dict(raw) if isinstance(raw, dict) else {}
    if not isinstance(root.get("integrations"), dict):
        root["integrations"] = {}
    robots = root.get("robots")
    if not isinstance(robots, dict):
        root["robots"] = {"items": []}
    elif not isinstance(robots.get("items"), list):
        root["robots"]["items"] = []
    if not isinstance(root.get("figma"), dict):
        root["figma"] = {}
    return root


def ensure_legacy_keys_migrated() -> None:
    """按账号拆 key 尚未接完路由，先不要把全局密钥搬空。"""
    global _LEGACY_MIGRATED
    _LEGACY_MIGRATED = True
    return
    if False:  # noqa: keep body for the later cutover
        _legacy_migrate_shared_keys()


def _legacy_migrate_shared_keys() -> None:
    root = ss._root()
    integrations = root.get("integrations") if isinstance(root.get("integrations"), dict) else {}
    robots = root.get("robots") if isinstance(root.get("robots"), dict) else {}
    figma = root.get("figma") if isinstance(root.get("figma"), dict) else {}
    robot_items = robots.get("items") if isinstance(robots.get("items"), list) else []
    has_secrets = bool(integrations or robot_items or str(figma.get("access_token") or "").strip())
    if not has_secrets:
        return
    from mino_nexus import auth_store

    admin = auth_store.seed_admin_user()
    uid = str(admin.get("user_id") or "").strip()
    if not uid:
        return
    try:
        user = _normalize_user_root(load_json(_user_plugins_path(uid), {}))
    except ValueError:
        return
    empty = (
        not user.get("integrations")
        and not (user.get("robots") or {}).get("items")
        and not str((user.get("figma") or {}).get("access_token") or "").strip()
    )
    if empty:
        user["integrations"] = {k: dict(v) for k, v in integrations.items() if isinstance(v, dict)}
        user["robots"] = {"items": [dict(x) for x in robot_items if isinstance(x, dict)]}
        user["figma"] = dict(figma)
        save_json(_user_plugins_path(uid), user)
    policy = _policy_root()
    changed = False
    for pid, cfg in integrations.items():
        if not isinstance(cfg, dict) or "enabled" not in cfg:
            continue
        row = dict(policy["plugins"].get(pid) or _default_policy_row())
        row["enabled"] = bool(cfg.get("enabled"))
        policy["plugins"][str(pid)] = row
        changed = True
    if changed:
        save_json(_POLICY_FILE, policy)
    root["integrations"] = {}
    root["robots"] = {"items": []}
    if isinstance(root.get("figma"), dict):
        root["figma"]["access_token"] = ""
    ss._save(root)


def _settings_root() -> dict[str, Any]:
    root = ss._root()
    if not isinstance(root.get("integrations"), dict):
        root["integrations"] = {}
    robots = root.get("robots")
    if not isinstance(robots, dict):
        root["robots"] = {"items": []}
    elif not isinstance(robots.get("items"), list):
        root["robots"]["items"] = []
    return root


def _load_user(user_id: str = "") -> dict[str, Any]:
    uid = str(user_id or "").strip()
    if not uid:
        return _empty_user_root()
    try:
        return _normalize_user_root(load_json(_user_plugins_path(uid), {}))
    except ValueError:
        return _empty_user_root()


def _save_user(user_id: str, root: dict[str, Any]) -> None:
    save_json(_user_plugins_path(user_id), _normalize_user_root(root))


def _default_policy_row() -> dict[str, bool]:
    return {"visible": True, "enabled": True}


def _policy_root() -> dict[str, Any]:
    raw = load_json(_POLICY_FILE, {})
    plugins = raw.get("plugins") if isinstance(raw, dict) else {}
    if not isinstance(plugins, dict):
        plugins = {}
    out: dict[str, Any] = {"plugins": {}}
    for spec in INTEGRATION_PLUGIN_SPECS:
        pid = spec["id"]
        row = plugins.get(pid) if isinstance(plugins.get(pid), dict) else {}
        out["plugins"][pid] = {
            "visible": row.get("visible", True) is not False,
            "enabled": row.get("enabled", True) is not False,
        }
    return out


def get_plugin_policy(plugin_id: str) -> dict[str, bool]:
    pid = str(plugin_id or "").strip()
    return dict(_policy_root()["plugins"].get(pid) or _default_policy_row())


def save_plugin_policy(
    plugin_id: str,
    *,
    visible: Optional[bool] = None,
    enabled: Optional[bool] = None,
) -> dict[str, bool]:
    if not _plugin_spec(plugin_id):
        raise ValueError(f"未知插件: {plugin_id}")
    root = _policy_root()
    row = dict(root["plugins"].get(plugin_id) or _default_policy_row())
    if visible is not None:
        row["visible"] = bool(visible)
    if enabled is not None:
        row["enabled"] = bool(enabled)
    root["plugins"][plugin_id] = row
    save_json(_POLICY_FILE, root)
    return dict(row)


def plugin_enabled(plugin_id: str) -> bool:
    return bool(get_plugin_policy(plugin_id).get("enabled", True))


def default_zentao_template_row(
    *,
    template_id: str = "tpl-default",
    name: str = "默认模板",
    is_default: bool = True,
    raw: Any = None,
) -> dict[str, Any]:
    body = normalize_zentao_bug_template(raw)
    return {"id": template_id or f"tpl-{uuid.uuid4().hex[:10]}", "name": name, "is_default": bool(is_default), **body}


def normalize_zentao_bug_template(raw: Any = None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}

    def _level(value: Any, default: int = 3) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number in (1, 2, 3, 4) else default

    bug_type = str(src.get("type") or "codeerror").strip() or "codeerror"
    if bug_type not in ZENTAO_BUG_TYPES:
        bug_type = "codeerror"
    return {
        "title": str(src.get("title") or "").strip() or DEFAULT_ZENTAO_TITLE_TEMPLATE,
        "steps": str(src.get("steps") or "").strip() or DEFAULT_ZENTAO_STEPS_TEMPLATE,
        "type": bug_type,
        "severity": _level(src.get("severity"), 3),
        "pri": _level(src.get("pri"), 3),
        "opened_build": str(src.get("opened_build") or "trunk").strip() or "trunk",
    }


def normalize_zentao_templates(raw: Any = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        body = normalize_zentao_bug_template(item)
        tid = str(item.get("id") or "").strip() or f"tpl-{uuid.uuid4().hex[:10]}"
        if tid in seen:
            tid = f"tpl-{uuid.uuid4().hex[:10]}"
        seen.add(tid)
        rows.append({
            "id": tid,
            "name": str(item.get("name") or "").strip() or "未命名模板",
            "is_default": bool(item.get("is_default")),
            **body,
        })
    if not rows:
        rows = [default_zentao_template_row()]
    if not any(row.get("is_default") for row in rows):
        rows[0]["is_default"] = True
    seen_default = False
    for row in rows:
        if row.get("is_default"):
            if seen_default:
                row["is_default"] = False
            seen_default = True
    return rows


def _default_plugin_config(plugin_id: str) -> dict[str, Any]:
    spec = _plugin_spec(plugin_id) or {}
    cap_on = {c["id"]: True for c in (spec.get("capabilities") or [])}
    if plugin_id == "feishu":
        cap_on["wiki"] = False
        cap_on["notify"] = False
        return {
            "enabled": True,
            "capabilities": cap_on,
            "wiki": {
                "space_id": "",
                "root_node_token": "",
                "folder_pattern": "{project}/版本/{version}",
                "children": ["测试报告", "测试用例", "测试脑图", "需求", "缺陷"],
            },
            "notify": {
                "bot_id": "",
                "chat_id": "",
                "on_run_fail": True,
                "on_atlas_pending": True,
                "on_verdict": True,
            },
            "writeback": {"enabled": False, "status_column": "状态"},
            "chat": {"enabled": False},
        }
    if plugin_id == "zentao":
        return {
            "enabled": False,
            "url": "",
            "account": "",
            "token": "",
            "capabilities": cap_on,
            "flow": {
                "auto_create_local": False,
                "push_requires_confirm": True,
                "list_default": "current_version",
            },
            "templates": [default_zentao_template_row()],
            "bindings": [],
        }
    if plugin_id == "figma":
        return {"enabled": True, "capabilities": cap_on}
    if plugin_id in IM_PLUGIN_IDS:
        return {"enabled": True, "capabilities": cap_on, "chat": {"enabled": False}}
    return {"enabled": True, "capabilities": cap_on}


def _raw_plugin_config(plugin_id: str, user_id: str = "") -> dict[str, Any]:
    stored = (
        _load_user(user_id).get("integrations", {}).get(plugin_id)
        if user_id
        else _settings_root().get("integrations", {}).get(plugin_id)
    )
    return stored if isinstance(stored, dict) else {}


def _merged_plugin_config(plugin_id: str, user_id: str = "") -> dict[str, Any]:
    cfg = _deep_merge(_default_plugin_config(plugin_id), _raw_plugin_config(plugin_id, user_id))
    if plugin_id == "zentao":
        stored = _raw_plugin_config(plugin_id, user_id)
        stored_list = stored.get("templates") if isinstance(stored.get("templates"), list) else None
        stored_flow = stored.get("flow") if isinstance(stored.get("flow"), dict) else {}
        legacy = stored_flow.get("template") if isinstance(stored_flow.get("template"), dict) else None
        if stored_list:
            cfg["templates"] = normalize_zentao_templates(stored_list)
        elif legacy:
            cfg["templates"] = [default_zentao_template_row(raw=legacy)]
        else:
            cfg["templates"] = list_zentao_templates(stored)
        flow = dict(cfg.get("flow") or {})
        flow.pop("template", None)
        cfg["flow"] = flow
    if plugin_id in IM_PLUGIN_IDS:
        chat = cfg.get("chat") if isinstance(cfg.get("chat"), dict) else {}
        cfg["chat"] = {"enabled": bool(chat.get("enabled"))}
    policy = get_plugin_policy(plugin_id)
    cfg["enabled"] = bool(policy.get("enabled", True))
    cfg["visible"] = bool(policy.get("visible", True))
    return cfg


def list_zentao_templates(cfg: Optional[dict[str, Any]] = None, user_id: str = "") -> list[dict[str, Any]]:
    stored = cfg if isinstance(cfg, dict) else _raw_plugin_config("zentao", user_id)
    stored_list = stored.get("templates") if isinstance(stored.get("templates"), list) else None
    stored_flow = stored.get("flow") if isinstance(stored.get("flow"), dict) else {}
    legacy = stored_flow.get("template") if isinstance(stored_flow.get("template"), dict) else None
    if stored_list:
        return normalize_zentao_templates(stored_list)
    if legacy:
        return [default_zentao_template_row(raw=legacy)]
    return [default_zentao_template_row()]


def _robot_rows(user_id: str = "") -> list[dict[str, Any]]:
    items = (
        _load_user(user_id).get("robots", {}).get("items")
        if user_id
        else _settings_root().get("robots", {}).get("items")
    )
    return [dict(x) for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def _save_robot_rows(user_id: str, rows: list[dict[str, Any]]) -> None:
    if user_id:
        root = _load_user(user_id)
        robots = root.setdefault("robots", {})
        if not isinstance(robots, dict):
            robots = {}
            root["robots"] = robots
        robots["items"] = rows
        _save_user(user_id, root)
        return
    root = _settings_root()
    robots = root.setdefault("robots", {})
    if not isinstance(robots, dict):
        robots = {}
        root["robots"] = robots
    robots["items"] = rows
    ss._save(root)


def _robot_public(row: dict[str, Any]) -> dict[str, Any]:
    platform = (row.get("platform") or "lark").strip()
    preset = ROBOT_PLATFORM_PRESETS.get(platform, ROBOT_PLATFORM_PRESETS["lark"])
    credentials = row.get("credentials") if isinstance(row.get("credentials"), dict) else {}
    masked: dict[str, str] = {}
    public_credentials: dict[str, str] = {}
    for key, value in credentials.items():
        val = str(value or "").strip()
        if key in preset.get("secret_fields", []):
            masked[f"{key}_masked"] = ss._mask_secret(val)
        else:
            public_credentials[key] = val
    configured = all(str(credentials.get(k) or "").strip() for k in preset.get("required", []))
    app_id = str(credentials.get("app_id") or "").strip()
    app_secret = str(credentials.get("app_secret") or "").strip()
    return {
        "id": row.get("id") or "",
        "platform": platform,
        "platform_label": preset.get("label") or platform,
        "name": row.get("name") or preset.get("label") or "机器人",
        "credentials": public_credentials,
        "masked": masked,
        "configured": configured,
        "app_id": app_id,
        "app_secret_masked": ss._mask_secret(app_secret),
    }


def list_robot_integrations(user_id: str = "") -> list[dict[str, Any]]:
    return [_robot_public(x) for x in _robot_rows(user_id)]


def create_robot_integration(
    *,
    user_id: str = "",
    platform: str,
    name: str,
    credentials: dict[str, Any],
) -> dict[str, Any]:
    platform = (platform or "lark").strip()
    if platform not in ROBOT_PLATFORM_PRESETS:
        raise ValueError(f"不支持的平台: {platform}")
    clean = {str(k): str(v).strip() for k, v in (credentials or {}).items()}
    preset = ROBOT_PLATFORM_PRESETS[platform]
    for key in preset.get("required", []):
        if not clean.get(key):
            raise ValueError(f"请填写 {key}")
    rows = _robot_rows(user_id)
    row = {
        "id": uuid.uuid4().hex[:12],
        "platform": platform,
        "name": (name or preset.get("label") or "机器人").strip(),
        "credentials": clean,
    }
    rows.append(row)
    _save_robot_rows(user_id, rows)
    return _robot_public(row)


def update_robot_integration(
    robot_id: str,
    *,
    user_id: str = "",
    platform: Optional[str] = None,
    name: Optional[str] = None,
    credentials: Optional[dict[str, Any]] = None,
    clear_secret: bool = False,
) -> dict[str, Any]:
    rows = _robot_rows(user_id)
    found = next((x for x in rows if str(x.get("id")) == str(robot_id)), None)
    if not found:
        raise ValueError(f"机器人不存在: {robot_id}")
    if platform:
        if platform not in ROBOT_PLATFORM_PRESETS:
            raise ValueError(f"不支持的平台: {platform}")
        found["platform"] = platform
    if name is not None:
        found["name"] = str(name).strip() or found.get("name")
    current = found.setdefault("credentials", {})
    if not isinstance(current, dict):
        current = {}
        found["credentials"] = current
    plat = found.get("platform") or "lark"
    preset = ROBOT_PLATFORM_PRESETS.get(plat, ROBOT_PLATFORM_PRESETS["lark"])
    if clear_secret:
        for key in preset.get("secret_fields", []):
            current.pop(key, None)
    if isinstance(credentials, dict):
        for key, value in credentials.items():
            val = str(value or "").strip()
            if not val and key in preset.get("secret_fields", []):
                continue
            current[str(key)] = val
    _save_robot_rows(user_id, rows)
    return _robot_public(found)


def delete_robot_integration(robot_id: str, *, user_id: str = "") -> None:
    rows = _robot_rows(user_id)
    nxt = [x for x in rows if str(x.get("id")) != str(robot_id)]
    if len(nxt) == len(rows):
        raise ValueError(f"机器人不存在: {robot_id}")
    _save_robot_rows(user_id, nxt)


def _plugin_configured(plugin_id: str, cfg: dict[str, Any], user_id: str = "") -> bool:
    if plugin_id == "feishu":
        return any(b.get("platform") == "lark" and b.get("configured") for b in list_robot_integrations(user_id))
    if plugin_id in ("wecom", "dingtalk", "slack"):
        return any(b.get("platform") == plugin_id and b.get("configured") for b in list_robot_integrations(user_id))
    if plugin_id == "wechat":
        return False
    if plugin_id == "figma":
        return bool(ss.get_figma_settings().get("configured"))
    if plugin_id == "zentao":
        return bool(str(cfg.get("url") or "").strip() and str(cfg.get("token") or "").strip())
    return False


def _plugin_status(enabled: bool, configured: bool) -> str:
    if not enabled:
        return "off"
    if configured:
        return "ready"
    return "need_connect"


def _plugin_public_config(plugin_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    public = dict(cfg)
    if plugin_id == "zentao":
        token = str(public.pop("token", "") or "")
        public.pop("password", None)
        public["token_masked"] = ss._mask_secret(token)
        public["has_token"] = bool(token)
    if plugin_id in IM_PLUGIN_IDS:
        public["chat"] = {"enabled": bool((cfg.get("chat") or {}).get("enabled"))}
        public["chat_roles"] = [
            {"id": "im-qa-assistant", "label": "IM 总指挥"},
            {"id": "im-defect-assistant", "label": "IM 缺陷助手"},
        ]
        if plugin_id == "feishu":
            public["chat_webhook"] = {
                "path": "/webhooks/feishu",
                "event": "im.message.receive_v1",
                "mode": "long_connection",
            }
            public["chat_listener"] = {
                "running": False,
                "wanted": bool((cfg.get("chat") or {}).get("enabled")),
                "connected": False,
                "error": FEISHU_LIVE_NOT_PORTED if (cfg.get("chat") or {}).get("enabled") else "",
                "last": {},
            }
        if plugin_id == "wechat":
            public["wechat_account"] = {"logged_in": False}
            public["chat_listener"] = {
                "running": False,
                "wanted": False,
                "connected": False,
                "error": WECHAT_NOT_PORTED,
                "last": {},
            }
    return public


def _plugin_app_bindings(plugin_id: str) -> list[dict[str, Any]]:
    from mino_nexus import project_store as ps
    from mino_nexus.app_automation import count_qa_process_cases_from_env

    bots = {str(b.get("id")): b for b in list_robot_integrations() if b.get("platform") == "lark"}
    rows: list[dict[str, Any]] = []
    for project in ps.list_projects():
        for app in project.get("apps") or []:
            detail = ps.get_app_detail(str(app.get("id") or ""))
            env = detail.get("env") if isinstance(detail.get("env"), dict) else {}
            if plugin_id == "feishu":
                feishu = env.get("feishu") if isinstance(env.get("feishu"), dict) else {}
                bot_id = str(feishu.get("bot_id") or "")
                rows.append({
                    "project_id": project.get("id"),
                    "project_name": project.get("name") or "",
                    "app_id": app.get("id"),
                    "app_name": app.get("name") or "",
                    "doc_url": feishu.get("doc_url") or "",
                    "bot_id": bot_id,
                    "bot_name": (bots.get(bot_id) or {}).get("name") or "",
                    "enabled": feishu.get("enabled", True) is not False,
                    "env_profile": feishu.get("env_profile") or "test",
                    "data_range": feishu.get("data_range") or "A1:O500",
                    "case_count": count_qa_process_cases_from_env(env),
                })
            elif plugin_id == "figma":
                automation = env.get("automation") if isinstance(env.get("automation"), dict) else {}
                figma = automation.get("figma") if isinstance(automation.get("figma"), dict) else {}
                rows.append({
                    "project_id": project.get("id"),
                    "project_name": project.get("name") or "",
                    "app_id": app.get("id"),
                    "app_name": app.get("name") or "",
                    "file_url": figma.get("file_url") or "",
                    "file_key": figma.get("file_key") or "",
                    "last_sync_at": figma.get("last_sync_at") or "",
                })
    return rows


def list_integration_plugins() -> dict[str, Any]:
    robots = list_robot_integrations()
    figma = ss.get_figma_settings()
    out = []
    for spec in INTEGRATION_PLUGIN_SPECS:
        pid = spec["id"]
        cfg = _merged_plugin_config(pid)
        configured = _plugin_configured(pid, cfg)
        robot_n = 0
        platform = spec.get("robot_platform")
        if platform:
            robot_n = sum(1 for b in robots if b.get("platform") == platform and b.get("configured"))
        if pid == "figma" and figma.get("configured"):
            robot_n = 1
        if pid == "zentao" and configured:
            robot_n = 1
        out.append({
            "id": pid,
            "name": spec["name"],
            "kind": spec["kind"],
            "categories": list(spec.get("categories") or [spec["kind"]]),
            "color": spec["color"],
            "summary": spec["summary"],
            "capabilities": spec["capabilities"],
            "enabled": bool(cfg.get("enabled", True)),
            "configured": configured,
            "status": _plugin_status(bool(cfg.get("enabled", True)), configured),
            "ready_count": robot_n,
        })
    return {"categories": INTEGRATION_PLUGIN_CATEGORIES, "plugins": out}


def get_integration_plugin(plugin_id: str) -> dict[str, Any]:
    spec = _plugin_spec(plugin_id)
    if not spec:
        raise ValueError(f"未知插件: {plugin_id}")
    cfg = _merged_plugin_config(plugin_id)
    configured = _plugin_configured(plugin_id, cfg)
    data: dict[str, Any] = {
        "id": plugin_id,
        "name": spec["name"],
        "kind": spec["kind"],
        "categories": list(spec.get("categories") or [spec["kind"]]),
        "color": spec["color"],
        "summary": spec["summary"],
        "capabilities": spec["capabilities"],
        "enabled": bool(cfg.get("enabled", True)),
        "configured": configured,
        "status": _plugin_status(bool(cfg.get("enabled", True)), configured),
        "config": _plugin_public_config(plugin_id, cfg),
    }
    platform = spec.get("robot_platform")
    if platform:
        data["robots"] = [b for b in list_robot_integrations() if b.get("platform") == platform]
        data["robot_platform"] = platform
    if plugin_id == "figma":
        data["figma"] = ss.get_figma_settings()
    if plugin_id in ("feishu", "figma"):
        data["bindings"] = _plugin_app_bindings(plugin_id)
    return data


def save_integration_plugin(plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
    spec = _plugin_spec(plugin_id)
    if not spec:
        raise ValueError(f"未知插件: {plugin_id}")
    current = _merged_plugin_config(plugin_id)
    incoming = body if isinstance(body, dict) else {}

    if "enabled" in incoming:
        current["enabled"] = bool(incoming.get("enabled"))
    if isinstance(incoming.get("capabilities"), dict):
        caps = current.setdefault("capabilities", {})
        for key, value in incoming["capabilities"].items():
            caps[str(key)] = bool(value)

    if plugin_id in IM_PLUGIN_IDS and isinstance(incoming.get("chat"), dict):
        prev = current.get("chat") if isinstance(current.get("chat"), dict) else {}
        current["chat"] = {"enabled": bool(incoming["chat"].get("enabled", prev.get("enabled")))}
    if plugin_id == "feishu":
        for key in ("wiki", "notify", "writeback"):
            if isinstance(incoming.get(key), dict):
                current[key] = _deep_merge(current.get(key) or {}, incoming[key])
    elif plugin_id == "zentao":
        if "url" in incoming:
            current["url"] = str(incoming.get("url") or "").strip().rstrip("/")
        if "account" in incoming:
            current["account"] = str(incoming.get("account") or "").strip()
        if incoming.get("clear_token"):
            current["token"] = ""
        elif str(incoming.get("token") or "").strip():
            current["token"] = str(incoming.get("token") or "").strip()
        if isinstance(incoming.get("flow"), dict):
            incoming_flow = dict(incoming["flow"])
            incoming_flow.pop("template", None)
            current["flow"] = _deep_merge(current.get("flow") or {}, incoming_flow)
            if isinstance(current.get("flow"), dict):
                current["flow"].pop("template", None)
        if isinstance(incoming.get("templates"), list):
            current["templates"] = normalize_zentao_templates(incoming["templates"])
        if isinstance(incoming.get("bindings"), list):
            rows = []
            for row in incoming["bindings"]:
                if not isinstance(row, dict):
                    continue
                pid = str(row.get("project_id") or "").strip()
                if not pid:
                    continue
                rows.append({
                    "project_id": pid,
                    "project_name": str(row.get("project_name") or "").strip(),
                    "product_id": str(row.get("product_id") or "").strip(),
                    "product_name": str(row.get("product_name") or "").strip(),
                })
            current["bindings"] = rows
    elif plugin_id == "figma":
        figma_kwargs: dict[str, Any] = {}
        if "access_token" in incoming or incoming.get("clear_token"):
            figma_kwargs["access_token"] = str(incoming.get("access_token") or "")
            figma_kwargs["clear_token"] = bool(incoming.get("clear_token"))
        if "default_file_url" in incoming:
            figma_kwargs["default_file_url"] = str(incoming.get("default_file_url") or "")
        if figma_kwargs:
            ss.save_figma_settings(**figma_kwargs)

    root = _settings_root()
    to_store = dict(current)
    if plugin_id == "zentao" and not str(to_store.get("token") or "").strip():
        prev = _raw_plugin_config(plugin_id)
        if str(prev.get("token") or "").strip() and not incoming.get("clear_token"):
            to_store["token"] = prev["token"]
    root.setdefault("integrations", {})[plugin_id] = to_store
    ss._save(root)
    return get_integration_plugin(plugin_id)


def get_zentao_credentials() -> dict[str, str]:
    cfg = _merged_plugin_config("zentao")
    return {
        "url": str(cfg.get("url") or "").strip(),
        "account": str(cfg.get("account") or "").strip(),
        "token": str(cfg.get("token") or "").strip(),
    }


def _zentao_json(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        text = str(getattr(resp, "text", "") or "").strip()
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        try:
            import json
            return json.loads(text)
        except Exception:
            return {}


def _zentao_error_text(payload: Any) -> str:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "error", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, (dict, str)):
        nested = _zentao_error_text(data)
        if nested:
            return nested
    return ""


def _pick_zentao_token(payload: Any) -> str:
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if text.startswith("{") or text.startswith("["):
            try:
                import json
                return _pick_zentao_token(json.loads(text))
            except Exception:
                return ""
        return text if re.fullmatch(r"[A-Za-z0-9._-]{8,}", text) else ""
    if not isinstance(payload, dict):
        return ""
    token = payload.get("token") or payload.get("Token")
    if isinstance(token, dict):
        token = token.get("token") or token.get("Token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    for key in ("sessionID", "sessionId", "zentaosid"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, (dict, str)):
        return _pick_zentao_token(data)
    return ""


def fetch_zentao_token(*, url: str = "", account: str = "", password: str = "") -> dict[str, Any]:
    import requests

    saved = get_zentao_credentials()
    base = (url or saved.get("url") or "").strip().rstrip("/")
    account = (account or saved.get("account") or "").strip()
    password = str(password or "")
    if not base:
        raise ValueError("请填写禅道地址")
    if not re.match(r"^https?://", base, re.I):
        raise ValueError("禅道地址需要以 http:// 或 https:// 开头")
    if not account:
        raise ValueError("请填写账号")
    if not password:
        raise ValueError("请填写密码")

    last_error = ""
    payload = {"account": account, "password": password}
    for path in ("/api.php/v1/tokens", "/index.php?m=user&f=apilogin&t=json"):
        endpoint = f"{base}{path}"
        attempts = (
            {"json": payload, "headers": {"Content-Type": "application/json", "Accept": "application/json"}},
            {"data": payload, "headers": {"Accept": "application/json"}},
        )
        for kwargs in attempts:
            try:
                resp = requests.post(endpoint, timeout=12, **kwargs)
            except requests.RequestException as e:
                last_error = str(e)
                continue
            body = _zentao_json(resp)
            token = _pick_zentao_token(body)
            if token:
                return {"ok": True, "url": base, "account": account, "token": token, "path": path}
            ctype = str(resp.headers.get("content-type") or "")
            if "text/html" in ctype:
                last_error = "地址能打开，但不是开放接口。请确认禅道已开启 API。"
            else:
                last_error = _zentao_error_text(body) or f"HTTP {resp.status_code}"
            if path.startswith("/api.php/v1/tokens") and resp.status_code in (400, 401, 403) and "text/html" not in ctype:
                raise ValueError(last_error or "账号或密码不正确")

    session_id = ""
    for path in ("/api-getsessionid.json", "/index.php?m=api&f=getSessionID&t=json"):
        try:
            resp = requests.get(f"{base}{path}", timeout=12)
        except requests.RequestException as e:
            last_error = str(e)
            continue
        session_id = _pick_zentao_token(_zentao_json(resp))
        if session_id:
            break
    if session_id:
        digest = hashlib.md5(password.encode("utf-8")).hexdigest()
        for path in ("/user-login.json", "/index.php?m=user&f=login&t=json"):
            for pwd in (password, digest):
                try:
                    resp = requests.get(
                        f"{base}{path}",
                        params={"account": account, "password": pwd, "zentaosid": session_id},
                        timeout=12,
                    )
                except requests.RequestException as e:
                    last_error = str(e)
                    continue
                body = _zentao_json(resp)
                status = str(body.get("status") or body.get("result") or "").lower()
                token = _pick_zentao_token(body) or (session_id if status in {"success", "ok"} else "")
                if token:
                    return {"ok": True, "url": base, "account": account, "token": token, "path": path}
                last_error = _zentao_error_text(body) or last_error or "账号或密码不正确"

    raise ValueError(last_error or "无法向禅道换取 Token。请确认已开启开放接口，且账号密码正确。")


def test_zentao_connection(*, url: str = "", account: str = "", token: str = "") -> dict[str, Any]:
    import requests

    saved = get_zentao_credentials()
    base = (url or saved.get("url") or "").strip().rstrip("/")
    account = (account or saved.get("account") or "").strip()
    token = (token or saved.get("token") or "").strip()
    if not base:
        raise ValueError("请填写禅道地址")
    if not re.match(r"^https?://", base, re.I):
        raise ValueError("禅道地址需要以 http:// 或 https:// 开头")
    headers = {"Token": token} if token else {}
    last_error = ""
    for path in ("/api.php/v1/users", "/api.php/v1", "/api.php"):
        try:
            resp = requests.get(f"{base}{path}", headers=headers, timeout=8)
        except requests.RequestException as e:
            last_error = str(e)
            continue
        if resp.status_code < 500:
            return {
                "ok": True,
                "url": base,
                "account": account,
                "http_status": resp.status_code,
                "path": path,
                "hint": "已连通。产品 ID 在「产品绑定」里填禅道产品编号。",
            }
        last_error = f"HTTP {resp.status_code}"
    raise ValueError(last_error or "无法连接禅道")


def chat_integration_plugin(
    plugin_id: str,
    *,
    text: str = "",
    history: list | None = None,
    mode: str = "",
) -> dict[str, Any]:
    from mino_nexus.ai.roles_catalog import chat_with_role
    from mino_nexus.ai import dispatch_log as dispatch

    if plugin_id not in IM_PLUGIN_IDS:
        raise ValueError("这个插件没有 IM 对话")
    want = str(mode or "").strip()
    role_id = "im-defect-assistant" if want == "defect" else "im-qa-assistant"
    messages = [m for m in (history or []) if isinstance(m, dict)]
    messages.append({"role": "user", "content": str(text or "")})
    tok = dispatch.bind(trigger="settings_chat", source="plugin_trial", role=role_id, job="role_chat")
    try:
        data = chat_with_role(role_id=role_id, messages=messages)
    finally:
        dispatch.reset(tok)
    data["mode"] = "defect" if role_id == "im-defect-assistant" else "dialogue"
    data["plugin_id"] = plugin_id
    return data
