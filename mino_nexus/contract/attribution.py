"""失败归因 A/B/C：平台缺口 / 程序缺口 / 守卫误拦。

规划口径（docs/架构/V8/覆盖度与架构优化-20261024.md）：

| 类 | 含义 | 典型 reason_code |
|---|---|---|
| A | 平台缺口：平台本来就没有实现 | platform_unsupported / channel_absent |
| B | 程序缺口：目录缺、绑定未提交、块空、依赖未满足 | unavailable_cap / block_empty / missing_ref |
| C | 守卫误拦：手能点、程序也有，策略挡了合法路径 | skip_repeat / fuse_false / stage_menu_empty |

这里只做纯映射，不存状态、不读库、不碰设备。事件、预检、编译产物统一调用
``enrich`` / ``blocker`` 打上 ``attribution_class`` / ``reason_code`` / ``layer``，
Workbench 只展示，不再自己猜。
"""
from __future__ import annotations

from typing import Any, Iterable

CLASS_A = "A"
CLASS_B = "B"
CLASS_C = "C"

CLASSES: tuple[str, ...] = (CLASS_A, CLASS_B, CLASS_C)

CLASS_LABELS: dict[str, str] = {
    CLASS_A: "平台缺口",
    CLASS_B: "程序缺口",
    CLASS_C: "守卫误拦",
}

# 处置建议：A 补执行器 / 标覆盖缺口，不算程序回归；B 修 Nexus/目录/绑定；C 改意图边或柔化守卫。
CLASS_ACTIONS: dict[str, str] = {
    CLASS_A: "补平台执行器或标注覆盖缺口，不计入程序回归",
    CLASS_B: "修 Nexus / 能力目录 / 绑定",
    CLASS_C: "改意图边或柔化守卫，禁止继续叠补丁",
}

LAYERS: tuple[str, ...] = (
    "compile",
    "preflight",
    "intent",
    "dispatch",
    "guard",
    "execution",
    "evidence",
)

_LAYER_ALIASES: dict[str, str] = {
    "plan": "compile",
    "compilation": "compile",
    "prepare": "preflight",
    "partition": "preflight",
    "route": "dispatch",
    "router": "dispatch",
    "channel": "dispatch",
    "capability": "dispatch",
    "step": "execution",
    "loop": "execution",
    "assert": "evidence",
    "check": "evidence",
}

# 显式 reason_code → 类。顺序无关；最长/精确优先。
_REASON_CLASS: dict[str, str] = {
    # A 平台缺口
    "platform_unsupported": CLASS_A,
    "channel_absent": CLASS_A,
    "event_platform_unsupported": CLASS_A,
    "executor_absent": CLASS_A,
    "executor_not_connected": CLASS_A,
    "node_offline": CLASS_A,
    "no_device": CLASS_A,
    "device_offline": CLASS_A,
    "target_channel_unknown": CLASS_A,
    "target_channels_unknown": CLASS_A,
    "platform_missing": CLASS_A,
    # B 程序缺口
    "unavailable_cap": CLASS_B,
    "capability_disabled": CLASS_B,
    "event_disabled": CLASS_B,
    "event_not_registered": CLASS_B,
    "event_not_found": CLASS_B,
    "block_empty": CLASS_B,
    "missing_block_id": CLASS_B,
    "missing_ref": CLASS_B,
    "missing_reference": CLASS_B,
    "catalog_capability_missing": CLASS_B,
    "capability_missing": CLASS_B,
    "dependency_blocked": CLASS_B,
    "dependency_unknown": CLASS_B,
    "dependency_snapshot_invalid": CLASS_B,
    "binding_missing": CLASS_B,
    "binding_stale": CLASS_B,
    "block_revision_mismatch": CLASS_B,
    "compile_model_call_failed": CLASS_B,
    "compile_invalid": CLASS_B,
    "source_invalid": CLASS_B,
    "needs_setup": CLASS_B,
    "setup_missing": CLASS_B,
    "resource_blocked": CLASS_B,
    "resource_busy": CLASS_B,
    "resource_lease_unavailable": CLASS_B,
    "requirement_blocked": CLASS_B,
    "write_outcome_unknown": CLASS_B,
    # C 守卫误拦 / 旧权限墙
    "action_fuse": CLASS_C,
    "fuse_false": CLASS_C,
    "same_target_repeat": CLASS_C,
    "stage_menu_empty": CLASS_C,
    "phase_not_allowed": CLASS_C,
    "event_call_not_allowed": CLASS_C,
    "nav.cap_not_allowed": CLASS_C,
    "deny_mutate": CLASS_C,
    "stuck_alternation": CLASS_C,
    "force_case_expectation": CLASS_C,
    "limit_recovery_retry": CLASS_C,
    "guard_block": CLASS_C,
}

_PREFIX_CLASS: tuple[tuple[str, str], ...] = (
    ("platform_", CLASS_A),
    ("channel_", CLASS_A),
    ("executor_", CLASS_A),
    ("skip_repeat", CLASS_C),
    ("guard_", CLASS_C),
    ("require_", CLASS_C),
    ("block_repeat", CLASS_C),
    ("block_", CLASS_C),
)

_REASON_LAYER: dict[str, str] = {
    "resource_busy": "preflight",
    "resource_lease_unavailable": "preflight",
    "binding_missing": "dispatch",
    "binding_stale": "dispatch",
    "compile_model_call_failed": "compile",
    "compile_invalid": "compile",
}

_RESPONSIBILITY_CLASS: dict[str, str] = {
    "authorization": CLASS_C,
    "policy": CLASS_C,
    "platform": CLASS_A,
    "configuration": CLASS_B,
    "resource": CLASS_B,
    "input": CLASS_B,
}


def canonical_layer(layer: str) -> str:
    raw = str(layer or "").strip().lower()
    if not raw:
        return ""
    return raw if raw in LAYERS else _LAYER_ALIASES.get(raw, raw)


def classify_reason(reason_code: str, *, layer: str = "", responsibility: str = "") -> dict[str, Any]:
    """把任意 reason_code 归到 A/B/C，并带上层。未知原因按 B（先当作程序缺口）。"""
    code = str(reason_code or "").strip()
    klass = _REASON_CLASS.get(code, "")
    if not klass:
        low = code.lower()
        for prefix, value in _PREFIX_CLASS:
            if low.startswith(prefix):
                klass = value
                break
    if not klass:
        klass = _RESPONSIBILITY_CLASS.get(str(responsibility or "").strip().lower(), CLASS_B)
    resolved_layer = canonical_layer(layer) or _REASON_LAYER.get(code, "")
    if not resolved_layer and code.lower().startswith("resource_"):
        resolved_layer = "preflight"
    return {
        "attribution_class": klass,
        "class": klass,
        "attribution_label": CLASS_LABELS.get(klass, ""),
        "action": CLASS_ACTIONS.get(klass, ""),
        "reason_code": code,
        "layer": resolved_layer,
        "responsibility": str(responsibility or ""),
    }


def enrich(
    payload: dict[str, Any] | None,
    *,
    reason_code: str = "",
    layer: str = "",
    responsibility: str = "",
    klass: str = "",
) -> dict[str, Any]:
    """复制并补齐归因字段；已有字段不被空值覆盖。"""
    out = dict(payload or {})
    code = str(reason_code or out.get("reason_code") or out.get("code")
               or out.get("dispatch_gate_code") or out.get("error") or "").strip()
    got = classify_reason(
        code,
        layer=layer or str(out.get("layer") or ""),
        responsibility=responsibility or str(out.get("responsibility") or ""),
    )
    out.setdefault("reason_code", code or "unclassified")
    out.setdefault("layer", got["layer"] or canonical_layer(layer))
    out.setdefault("attribution_class", got["attribution_class"])
    out.setdefault("attribution_label", got["attribution_label"])
    if klass:
        out["attribution_class"] = klass
        out["attribution_label"] = CLASS_LABELS.get(klass, "")
    return out


def blocker(
    *,
    reason_code: str,
    layer: str,
    message: str = "",
    responsibility: str = "",
    klass: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """结构化阻塞项：预检 / 编译 / 覆盖统计共用同一种形状。"""
    row = {
        "reason_code": str(reason_code or "unclassified"),
        "layer": canonical_layer(layer),
        "message": str(message or "")[:400],
        "responsibility": str(responsibility or ""),
        **extra,
    }
    return enrich(row, klass=klass)


def merge_classes(items: Iterable[dict[str, Any]]) -> list[str]:
    """聚合一组阻塞项的 A/B/C 类，按 A→B→C 排序去重。"""
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("attribution_class") or "")
        if value in CLASSES:
            seen.add(value)
    return [c for c in CLASSES if c in seen]


def describe() -> dict[str, Any]:
    """给 Workbench / API 的字典：类含义、处置、已知 reason_code 归属。"""
    return {
        "schema": "mino.failure_attribution.v1",
        "classes": [
            {"code": c, "label": CLASS_LABELS[c], "action": CLASS_ACTIONS[c]} for c in CLASSES
        ],
        "layers": list(LAYERS),
        "reason_codes": dict(sorted(_REASON_CLASS.items())),
    }


__all__ = [
    "CLASS_A",
    "CLASS_B",
    "CLASS_C",
    "CLASSES",
    "CLASS_LABELS",
    "CLASS_ACTIONS",
    "LAYERS",
    "blocker",
    "canonical_layer",
    "classify_reason",
    "describe",
    "enrich",
    "merge_classes",
]
