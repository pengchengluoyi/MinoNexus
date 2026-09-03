"""Studio 侧栏 / 设置入口允许名单。Console 决定，Nexus 落盘，Studio 只读。"""
from __future__ import annotations

from typing import Any

from mino_nexus.json_store import load_json, save_json

_FILE = "studio_nav.json"

# id 必须稳定：Studio 侧栏 / 路由用同一套。
ENTRIES: list[dict[str, str]] = [
    {"id": "testing", "label": "测试工作台", "group": "work"},
    {"id": "agent", "label": "Agent", "group": "work"},
    {"id": "knowledge", "label": "知识工作台", "group": "work"},
    {"id": "runtime", "label": "运行与设备", "group": "settings"},
    {"id": "scout", "label": "Scout 节点", "group": "settings"},
    {"id": "keys", "label": "模型密钥", "group": "settings"},
    {"id": "dispatch", "label": "调用记录", "group": "settings"},
    {"id": "plugins", "label": "插件配置", "group": "settings"},
]

# 默认对齐产品切面：插件配置归 Console，Studio 不展示。Scout 节点留在 Studio。
DEFAULT_ALLOWED = ("testing", "agent", "knowledge", "runtime", "scout", "keys", "dispatch")
_NAV_VERSION = 2


def catalog() -> list[dict[str, str]]:
    return [dict(row) for row in ENTRIES]


def _known_ids() -> set[str]:
    return {str(row["id"]) for row in ENTRIES}


def _normalize(ids: Any) -> list[str]:
    known = _known_ids()
    out: list[str] = []
    if not isinstance(ids, list):
        return [i for i in DEFAULT_ALLOWED if i in known]
    for raw in ids:
        item = str(raw or "").strip()
        if item in known and item not in out:
            out.append(item)
    return out


def get_allowed() -> list[str]:
    raw = load_json(_FILE, {})
    if not isinstance(raw, dict) or "allowed" not in raw:
        return list(DEFAULT_ALLOWED)
    allowed = _normalize(raw.get("allowed"))
    try:
        version = int(raw.get("v") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < _NAV_VERSION and "scout" in _known_ids() and "scout" not in allowed:
        if "runtime" in allowed:
            allowed.insert(allowed.index("runtime") + 1, "scout")
        else:
            allowed.append("scout")
        save_json(_FILE, {"allowed": allowed, "v": _NAV_VERSION})
    return allowed


def save_allowed(ids: list[str] | None) -> dict[str, Any]:
    allowed = _normalize(ids)
    save_json(_FILE, {"allowed": allowed, "v": _NAV_VERSION})
    return public_nav()


def public_nav() -> dict[str, Any]:
    allowed = get_allowed()
    return {
        "entries": catalog(),
        "allowed": allowed,
        "default_allowed": list(DEFAULT_ALLOWED),
    }


def is_allowed(entry_id: str) -> bool:
    return str(entry_id or "").strip() in set(get_allowed())
