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
    from mino_nexus.core.platform_contract import platform_set, target_platform
    try: return target_platform(platform) in platform_set(platforms)
    except ValueError: return False


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
        if cap.enabled is False or cap.lifecycle != "active":
            continue
        # ui_coverable 只进覆盖统计，不再决定模型能否看见/程序能否派发（规划 §4）。
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



def runtime_contract_diagnostics(capabilities: dict[str, Capability]) -> list[dict[str, Any]]:
    """Expose runtime-policy gaps separately from syntactic catalog load errors.

    No enablement changes or inferred implementations. A configured disable is
    explicit, and target-specific connectivity still belongs to live binding.
    """
    from mino_nexus.contract.interrupt_caps import BUILTIN_INTERRUPT_CAPS
    from mino_nexus.contract.executor_sets import CHILDREN
    expected = {cid: {"prep", "do"} for cid in BUILTIN_INTERRUPT_CAPS}
    expected.update({cid: {"prep", "do", "check", "cleanup"}
                     for cid in ("wake_screen", "dismiss_keyguard", "probe_device_state", "get_foreground_app")})
    sources = {cid: {"runtime_policy"} for cid in expected}
    for parent in capabilities.values():
        if not parent.enabled or parent.lifecycle != "active":
            continue
        for child in CHILDREN.get(parent.id, ()):
            expected.setdefault(child, set()).update(parent.phases)
            sources.setdefault(child, set()).add(parent.id)
    issues = []
    for cid, phases in sorted(expected.items()):
        cap = capabilities.get(cid)
        reason = "capability_missing" if cap is None else (
            "disabled_by_configuration" if not cap.enabled or cap.lifecycle != "active" else
            "phase_contract_incomplete" if phases - set(cap.phases) else
            "implementation_missing" if not cap.implementations and cid not in LOCAL_ORCH_IDS else "")
        if reason:
            issues.append({"capability_id": cid, "reason_code": reason,
                "required_by": sorted(sources[cid]), "required_phases": sorted(phases),
                "declared_phases": list(cap.phases) if cap else [],
                "target_binding_required": True})
    return issues


def health_summary() -> dict[str, Any]:
    loader = get_loader()
    return {
        "root": str(loader.root),
        "capabilities_count": len(loader.capabilities),
        "runtime_contract_gaps": runtime_contract_diagnostics(loader.capabilities),
        "recovery_count": len(loader.recovery_rules),
        "errors": [e.model_dump() for e in loader.errors],
    }
