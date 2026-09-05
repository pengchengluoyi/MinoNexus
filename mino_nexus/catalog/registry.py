"""能力目录查询：按连通性 + 平台过滤。impl.executor 对 Scout 上报的通道名。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.catalog.exec_classes import LOCAL_ORCH_IDS
from mino_nexus.catalog.loader import get_loader, force_reload
from mino_nexus.catalog.models import Capability, Implementation, LoadError

_ALWAYS = frozenset({"internal", "vlm", "hitl", "ai_persona"})

ConnectivityFlags = dict[str, bool]


def list_capabilities(*, kinds: Optional[list[str]] = None) -> list[Capability]:
    caps = list(get_loader().capabilities.values())
    if kinds:
        allow = {str(x) for x in kinds}
        caps = [c for c in caps if c.kind in allow]
    return caps


def list_recovery_rules(*, enabled_only: bool = True) -> list:
    rules = list(get_loader().recovery_rules.values())
    if enabled_only:
        rules = [r for r in rules if r.enabled and r.lifecycle == "active"]
    return sorted(rules, key=lambda r: -int(getattr(r, "priority", 0)))


def get_recovery_rule(rule_id: str):
    return get_loader().recovery_rules.get(rule_id)


def list_load_errors() -> list[LoadError]:
    return list(get_loader().errors)


def get_capability(capability_id: str) -> Optional[Capability]:
    return get_loader().capabilities.get(capability_id)


def _executor_ok(executor: str, connectivity: ConnectivityFlags) -> bool:
    ex = str(executor or "").strip()
    if not ex or ex in _ALWAYS:
        return True
    return bool(connectivity.get(ex, False))


def platform_ok(platforms: list[str], platform: str) -> bool:
    """platforms 为空 = 全平台。"""
    want = [str(p).strip().lower() for p in (platforms or []) if str(p).strip()]
    if not want:
        return True
    got = str(platform or "").strip().lower()
    if not got:
        return True
    return got in want


def filter_capabilities(
    connectivity: ConnectivityFlags,
    *,
    kinds: Optional[list[str]] = None,
    platform: str = "",
    drop_when_no_impl: bool = True,
) -> list[Capability]:
    """连通性看 impl.executor 是否在 Scout 通道里；无 implementation 的本地能力仍保留。"""
    out: list[Capability] = []
    for cap in list_capabilities(kinds=kinds):
        if cap.enabled is False or str(cap.lifecycle or "active") == "deprecated":
            continue
        if not platform_ok(list(cap.platforms or []), platform):
            continue
        kept: list[Implementation] = []
        for impl in cap.implementations or []:
            if _executor_ok(impl.executor, connectivity):
                kept.append(impl)
        if kept:
            out.append(cap.model_copy(update={"implementations": kept}))
        elif not (cap.implementations or []) and cap.id in LOCAL_ORCH_IDS:
            out.append(cap)
    return out


def reload() -> dict[str, Any]:
    loader = force_reload()
    return {
        "capabilities": len(loader.capabilities),
        "recovery": len(loader.recovery_rules),
        "errors": [e.model_dump() for e in loader.errors],
    }


def health_summary() -> dict[str, Any]:
    loader = get_loader()
    return {
        "root": str(loader.root),
        "capabilities_count": len(loader.capabilities),
        "recovery_count": len(loader.recovery_rules),
        "errors": [e.model_dump() for e in loader.errors],
    }
