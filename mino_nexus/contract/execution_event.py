"""统一执行事件：内置循环（tool/result）与外部 agent（ext/*）共用的最小字段集。

只做读取侧归一化，不改任何事件的写入格式。
"""
from __future__ import annotations

from typing import Any

FAIL_STATUS = frozenset({"fail", "failed", "error", "timeout", "declined", "blocked"})
_EXT_STEP_KINDS = frozenset({"action", "observe"})


def normalize(row: dict[str, Any]) -> dict[str, Any] | None:
    """返回 {kind, phase, step, action, status, thumb, started_at, ended_at, duration_ms, source}；
    不是执行类事件返回 None。kind: step（动作）/ result（结果）/ phase。"""
    typ = str(row.get("type") or "")
    p = row.get("payload") or {}
    if typ == "tool/result":
        return {"kind": "result", "phase": str(row.get("phase") or p.get("phase") or ""),
                "step": int(row.get("turn") or 0), "action": str(p.get("capability_id") or ""),
                "status": str(p.get("status") or "").lower(), "thumb": str(p.get("thumb") or ""),
                "started_at": p.get("started_at"), "ended_at": p.get("ended_at"),
                "duration_ms": p.get("elapsed_ms"), "source": "nexus"}
    if typ.startswith("ext/") and typ not in ("ext/begin",):
        kind = typ.split("/", 1)[1]
        status = str(p.get("status") or "").lower()
        if not status:
            status = "fail" if p.get("error") else "pass" if kind != "note" else ""
        return {"kind": "step" if kind in _EXT_STEP_KINDS else "phase" if kind.startswith("phase") else "result",
                "phase": str(p.get("phase") or row.get("phase") or ""),
                "step": int(p.get("case_step") or 0),
                "action": str(p.get("action") or kind), "status": status,
                "thumb": str(p.get("screenshot_ref") or ""),
                "started_at": p.get("started_at"), "ended_at": p.get("ended_at"),
                "duration_ms": p.get("duration_ms"), "source": "external_agent"}
    return None


def is_failure(row: dict[str, Any]) -> bool:
    ev = normalize(row)
    return bool(ev and ev["status"] in FAIL_STATUS)


def is_action(row: dict[str, Any]) -> bool:
    ev = normalize(row)
    return bool(ev and ev["source"] == "external_agent" and ev["kind"] == "step")
