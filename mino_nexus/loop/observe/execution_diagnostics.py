"""Bounded execution metadata for Session Log, excluding input and image bodies."""
from __future__ import annotations

import math
from typing import Any


def execution_result_metadata(result: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    attempts = getattr(result, "attempts", None)
    if isinstance(attempts, list):
        cleaned = []
        for attempt in attempts[:16]:
            if not isinstance(attempt, dict):
                continue
            entry = {key: str(attempt[key])[:160] for key in ("executor", "status", "error") if isinstance(attempt.get(key), str)}
            if isinstance(attempt.get("elapsed_ms"), (int, float)) and math.isfinite(attempt["elapsed_ms"]):
                entry["elapsed_ms"] = max(0, int(attempt["elapsed_ms"]))
            if entry:
                cleaned.append(entry)
        if cleaned:
            out["attempts"] = cleaned
    raw = getattr(result, "raw_response", None)
    if not isinstance(raw, dict):
        return out
    scout = raw.get("scout")
    if isinstance(scout, dict):
        out["scout"] = {key: scout[key][:256] for key in ("node_id", "sn", "platform", "os", "scout_version", "run_id") if isinstance(scout.get(key), str)}
        if isinstance(scout.get("step_idx"), int):
            out["scout"]["step_idx"] = scout["step_idx"]
    desktop = {key: raw[key][:128] for key in ("capture_id", "session_generation", "snapshot_id", "capture_scope")
               if isinstance(raw.get(key), str)}
    target = raw.get("target")
    if isinstance(target, dict) and target.get("kind") == "window":
        desktop["target"] = {"kind": "window", **{key: target[key] for key in ("pid", "window_id")
                                                if isinstance(target.get(key), int) and not isinstance(target[key], bool)}}
    action = raw.get("cua_action")
    launch = raw.get("launch")
    if isinstance(launch, dict):
        desktop["launch"] = {
            key: value[:256] if isinstance(value, str) else value
            for key in ("stage", "target_package", "foreground_package", "foreground_source", "pid", "window_id",
                        "foreground_pid", "foreground_error", "error_type", "launched", "activated", "foreground_verified",
                        "window_verified", "activation_code")
            if isinstance((value := launch.get(key)), (str, int))
        }
        if isinstance(launch.get("window_candidates"), list):
            desktop["launch"]["window_candidates"] = [value for value in launch["window_candidates"][:32]
                                                       if isinstance(value, int) and not isinstance(value, bool) and value > 0]
    if raw.get("verification_required") is True:
        desktop["verification_status"] = "pending"
    if isinstance(action, dict):
        evidence = {key: action[key][:80] for key in ("effect", "route") if isinstance(action.get(key), str)}
        if isinstance(action.get("evidence"), list):
            evidence["evidence"] = [{"kind": str(row.get("kind") or "")[:80], "detail_omitted": bool(row.get("detail_omitted"))}
                                    for row in action["evidence"][:16] if isinstance(row, dict)]
        for key, fields in (("delivery", ("mode", "delivered_count")), ("error", ("code",)), ("escalation", ("target", "reason"))):
            child = action.get(key)
            if isinstance(child, dict):
                evidence[key] = {field: value[:80] if isinstance(value, str) else value for field in fields
                                 if isinstance((value := child.get(field)), (str, int))}
        if evidence:
            desktop["action"] = evidence
    if desktop:
        out["desktop"] = desktop
    return out
