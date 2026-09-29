"""P6 开关：唯一真源为 run-case SOP 当前 phase（Console 编排 job / exec_job / advance_on）。"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Optional

_phase_cfg: ContextVar[Optional[dict[str, Any]]] = ContextVar("sop_phase_cfg", default=None)


def bind_sop_phase_cfg(phase_cfg: dict[str, Any]) -> Token:
    return _phase_cfg.set(dict(phase_cfg or {}))


def reset_sop_phase_cfg(token: Token) -> None:
    _phase_cfg.reset(token)


def _cfg() -> dict[str, Any]:
    return dict(_phase_cfg.get() or {})


def _plan_job() -> str:
    return str(_cfg().get("job") or "agent-decide").strip() or "agent-decide"


def _exec_job() -> str:
    cfg = _cfg()
    return str(cfg.get("exec_job") or cfg.get("job") or "agent-decide").strip() or "agent-decide"


def vision_plan_v1_enabled() -> bool:
    return _plan_job() == "agent-vision-plan"


def vision_exec_v1_enabled() -> bool:
    return _exec_job() == "agent-vision-exec"


def step_phase_fsm_v1_enabled() -> bool:
    return str(_cfg().get("advance_on") or "signal_done").strip() == "milestones"


def vision_assert_v1_enabled() -> bool:
    return _exec_job() == "agent-vision-assert"


def sop_orchestration_snapshot(phases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for p in phases or []:
        if not isinstance(p, dict):
            continue
        rows.append(
            {
                "id": str(p.get("id") or ""),
                "job": str(p.get("job") or ""),
                "exec_job": str(p.get("exec_job") or ""),
                "advance_on": str(p.get("advance_on") or ""),
            }
        )
    return {
        "phases": rows,
        "active": {
            "plan": vision_plan_v1_enabled(),
            "exec": vision_exec_v1_enabled(),
            "step_fsm": step_phase_fsm_v1_enabled(),
            "assert": vision_assert_v1_enabled(),
            "plan_job": _plan_job(),
            "exec_job": _exec_job(),
            "advance_on": str(_cfg().get("advance_on") or ""),
        },
    }
