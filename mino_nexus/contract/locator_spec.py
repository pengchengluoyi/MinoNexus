"""Typed locator_spec by target platform. No version token in the execution path."""
from __future__ import annotations

from typing import Any

from mino_nexus.core.platform_contract import target_platform
from mino_nexus.contract.web_v5_contract import CONTRACTS, validate_locator as validate_web_locator

_ANDROID_KINDS = frozenset({"resource_id", "content_desc", "text", "role"})
_MAC_KINDS = frozenset({"identifier", "title", "text", "role"})
_WIN_KINDS = frozenset({"automation_id", "name", "text", "role"})
_FORBIDDEN = ("xpath=", "nth(", "instance=", "index=")
_IOS_KINDS = frozenset({"name", "label", "identifier", "text", "role"})
_CELLS = {"android": "uiautomator", "ios": "xcuitest", "web": "playwright", "mac": "ax", "win": "uia"}
_KINDS = {"android": _ANDROID_KINDS, "ios": _IOS_KINDS, "mac": _MAC_KINDS, "win": _WIN_KINDS}
_EXACT = {
    "android": frozenset({"content_desc", "text"}),
    "ios": frozenset({"name", "label", "text"}),
    "mac": frozenset({"title", "text"}),
    "win": frozenset({"name", "text"}),
}


def spec_cell(platform: str) -> str:
    plat = target_platform(platform)
    return _CELLS.get(plat, "")


def spec_contract(platform: str) -> str:
    """Web Workbench already publishes locator_spec under this name. Other platforms have none."""
    plat = target_platform(platform)
    if plat == "web":
        return str(CONTRACTS.get("locator_contract") or "")
    return ""


def validate_locator_spec(spec: Any, platform: str) -> None:
    """Validate the cell that belongs to this platform. Other cells are ignored."""
    plat = target_platform(platform)
    if not isinstance(spec, dict):
        raise ValueError("invalid_locator")
    if plat == "web":
        if set(spec) == {"playwright"}:
            validate_web_locator(spec)
            return
        cfg = spec.get("playwright")
        if not isinstance(cfg, dict):
            raise ValueError("invalid_locator")
        validate_web_locator({"playwright": cfg})
        return
    cell = spec_cell(plat)
    cfg = spec.get(cell)
    if not isinstance(cfg, dict):
        raise ValueError("invalid_locator")
    kinds = _KINDS.get(plat)
    if kinds is None:
        raise ValueError("invalid_locator")
    _validate_selector(cfg, kinds, _EXACT.get(plat) or frozenset())


def _validate_selector(cfg: dict[str, Any], kinds: frozenset[str], exact_kinds: frozenset[str]) -> None:
    if cfg.get("cardinality") not in {"one", "set"}:
        raise ValueError("invalid_locator")
    sel = cfg.get("selector") or {}
    kind = sel.get("kind")
    if kind not in kinds or set(sel) - {"kind", "value", "name", "exact"}:
        raise ValueError("invalid_locator")
    if not isinstance(sel.get("value"), str) or not sel["value"].strip():
        raise ValueError("invalid_locator")
    if kind in exact_kinds and sel.get("exact") is not True:
        raise ValueError("invalid_locator")
    if kind == "role" and not isinstance(sel.get("name"), str):
        raise ValueError("invalid_locator")
    blob = str(sel.get("value") or "").lower()
    if any(token in blob for token in _FORBIDDEN):
        raise ValueError("invalid_locator")


def spec_selector(spec: Any, platform: str) -> dict[str, Any]:
    if not isinstance(spec, dict):
        return {}
    try:
        plat = target_platform(platform)
    except (TypeError, ValueError):
        return {}
    cell = spec_cell(plat)
    cfg = spec.get(cell) if cell else None
    if not isinstance(cfg, dict):
        return {}
    sel = cfg.get("selector") if isinstance(cfg.get("selector"), dict) else {}
    return dict(sel) if sel else {}
