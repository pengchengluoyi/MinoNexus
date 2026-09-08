"""skills.sop_json 阶段程序：规范化 + 默认值。"""
from __future__ import annotations

from typing import Any

from mino_nexus.catalog.exec_classes import ALL_KINDS
from mino_nexus.loop.registry import ADVANCERS, GUARDS, INSPECTION_ATS

DEFAULT_PHASES: dict[str, dict[str, Any]] = {
    "prep": {
        "id": "prep",
        "job": "agent-decide",
        "tool_kinds": ["prep", "generic"],
        "guards": [],
        "advance_on": "signal_done",
    },
    "do": {
        "id": "do",
        "job": "agent-decide",
        "tool_kinds": ["do", "generic"],
        "guards": ["skip_repeat_tap"],
        "advance_on": "signal_done",
    },
    "check": {
        "id": "check",
        "job": "agent-decide",
        "tool_kinds": ["check"],
        "guards": ["deny_mutate", "force_case_expectation"],
        "advance_on": "signal_done",
        "require": "saw_assert",
    },
}

DEFAULT_INSPECTIONS: list[dict[str, Any]] = [
    {"job": "inspect-session", "at": "case_start", "on_fail": "continue"},
]


def _norm_str_list(raw: Any, allowed: set[str]) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        key = str(item or "").strip()
        if key and key in allowed and key not in out:
            out.append(key)
    return out


def normalize_phase(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        pid = str(raw or "").strip().lower() or "do"
        base = dict(DEFAULT_PHASES.get(pid, {"id": pid}))
        base["id"] = pid
        return base
    if not isinstance(raw, dict):
        return dict(DEFAULT_PHASES["do"])
    pid = str(raw.get("id") or "do").strip().lower() or "do"
    base = dict(DEFAULT_PHASES.get(pid, {"id": pid}))
    base.update(raw)
    base["id"] = pid
    base["job"] = str(base.get("job") or "agent-decide").strip() or "agent-decide"
    base["tool_kinds"] = _norm_str_list(base.get("tool_kinds"), set(ALL_KINDS)) or list(
        DEFAULT_PHASES.get(pid, DEFAULT_PHASES["do"]).get("tool_kinds") or list(ALL_KINDS)
    )
    base["guards"] = _norm_str_list(base.get("guards"), set(GUARDS))
    adv = str(base.get("advance_on") or "signal_done").strip()
    if adv not in ADVANCERS:
        adv = "signal_done"
    base["advance_on"] = adv
    req = str(base.get("require") or "").strip()
    if req and req not in ("saw_assert",):
        req = ""
    base["require"] = req
    return base


def normalize_phases(raw: Any) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else ["prep", "do", "check"]
    if not items:
        items = ["prep", "do", "check"]
    return [normalize_phase(item) for item in items]


def normalize_inspection(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("inspection 必须是对象")
    job = str(raw.get("job") or "").strip()
    if not job:
        raise ValueError("inspection 缺少 job")
    at = str(raw.get("at") or "").strip()
    if at not in INSPECTION_ATS:
        raise ValueError(f"未知 inspection.at：{at}")
    on_fail = str(raw.get("on_fail") or "continue").strip()
    if on_fail not in ("continue", "abort"):
        on_fail = "continue"
    return {"job": job, "at": at, "on_fail": on_fail}


def normalize_inspections(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return list(DEFAULT_INSPECTIONS)
    if not isinstance(raw, list):
        return list(DEFAULT_INSPECTIONS)
    if not raw:
        return []
    return [normalize_inspection(item) for item in raw]


def phase_for_id(phases: list[dict[str, Any]], phase_id: str) -> dict[str, Any]:
    pid = str(phase_id or "").strip().lower()
    for row in phases:
        if str(row.get("id") or "").lower() == pid:
            return row
    return normalize_phase(pid)


def sop_union_tool_kinds(phases: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for row in phases:
        for kind in row.get("tool_kinds") or []:
            k = str(kind)
            if k not in seen:
                seen.append(k)
    return seen or list(ALL_KINDS)


def merge_phase_tool_kinds(phase_cfg: dict[str, Any], sop: dict[str, Any]) -> list[str]:
    """阶段菜单 = 阶段 tool_kinds + SOP 全局声明的 recovery（不并 prep/do/check，避免串阶段）。"""
    pid = str(phase_cfg.get("id") or "do").strip().lower() or "do"
    phase_kinds = _norm_str_list(phase_cfg.get("tool_kinds"), set(ALL_KINDS))
    if not phase_kinds:
        phase_kinds = list(DEFAULT_PHASES.get(pid, DEFAULT_PHASES["do"]).get("tool_kinds") or [])
    global_kinds = _norm_str_list(sop.get("tool_kinds"), set(ALL_KINDS))
    merged = list(phase_kinds)
    for kind in global_kinds:
        if kind == "recovery" and kind not in merged:
            merged.append(kind)
    return merged
