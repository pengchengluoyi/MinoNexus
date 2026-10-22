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
    raw = result if isinstance(result, dict) else getattr(result, "raw_response", None)
    if not isinstance(raw, dict):
        return out
    otp = raw.get("otp_diagnostics")
    if isinstance(otp, dict):
        safe = {key: otp[key] for key in ("connection_attempts", "reconnects")
                if type(otp.get(key)) is int and 0 <= otp[key] <= 10000}
        if otp.get("phase") in {"connect", "login", "select", "search", "fetch"}:
            safe["phase"] = otp["phase"]
        allowed = {"otp_imap_" + code for code in (
            "dns_failed", "connect_timeout", "read_timeout", "tls_certificate_failed", "tls_failed",
            "connection_refused", "connection_lost", "network_unreachable", "auth_failed",
            "protocol_failed", "connection_failed")}
        if isinstance(otp.get("error_code"), str) and otp["error_code"] in allowed:
            safe["error_code"] = otp["error_code"]
        if safe:
            out["otp_diagnostics"] = safe
    for group, fields in (
        ("execution", ("execution_started", "phase", "reason_code", "source")),
        ("execution_control", ("stop_run", "resource_isolated", "reason_code", "worker_active", "stop_confirmed")),
        ("action_ref", ("run_id", "step_idx")),
    ):
        value = raw.get(group)
        if isinstance(value, dict):
            out[group] = {key: item[:256] if isinstance(item, str) else item
                          for key in fields if isinstance((item := value.get(key)), (str, bool, int))}
    if "execution" not in out and isinstance(raw.get("execution_started"), bool):
        out["execution"] = {"execution_started": "yes" if raw["execution_started"] else "no"}
    admission = raw.get("admission")
    if isinstance(admission, dict) and isinstance(admission.get("active_requests"), list):
        out["admission"] = {"active_requests": [
            {"capability_id": str(row.get("capability_id") or "")[:128],
             "effect_class": str(row.get("effect_class") or "")[:16],
             "action_ref": {key: value[:256] if isinstance(value, str) else value
                            for key in ("run_id", "step_idx")
                            if isinstance((value := (row.get("action_ref") or {}).get(key)), (str, int))}}
            for row in admission["active_requests"][:16] if isinstance(row, dict)]}
    scout = raw.get("scout")
    if isinstance(scout, dict):
        out["scout"] = {key: scout[key][:256] for key in ("node_id", "sn", "platform", "os", "scout_version", "run_id", "scout_process_generation", "node_connection_generation") if isinstance(scout.get(key), str)}
        if isinstance(scout.get("step_idx"), int):
            out["scout"]["step_idx"] = scout["step_idx"]
        from mino_nexus.casework.execution_evidence import public_identity
        out["scout"]["nexus_identity"] = public_identity(scout.get("nexus_identity"))
        receipt = scout.get("execution_identity")
        if isinstance(receipt, dict):
            safe = {key: receipt[key][:256] for key in ("node_id", "scout_process_generation", "node_connection_generation")
                    if isinstance(receipt.get(key), str)}
            safe["schema_version"] = receipt.get("schema_version") if type(receipt.get("schema_version")) is int else 0
            build = receipt.get("build")
            if isinstance(build, dict):
                safe["build"] = {key: build[key][:256] for key in ("build_id", "source_commit", "source_digest", "identity_kind")
                                 if isinstance(build.get(key), str)}
                safe["build"].update({key: build[key] for key in ("dirty", "source_unchanged") if type(build.get(key)) is bool})
            out["scout"]["execution_identity"] = safe
    fd = raw.get("focus_diag")
    if fd is True:
        # Scout 焦点引用失配诊断：只含 bounds/hint/包名，无输入正文。
        edits = raw.get("edit_texts")
        out["focus_diag"] = {
            "ref_sent": str(raw.get("ref_sent") or "")[:128],
            "ref_recorded": str(raw.get("ref_recorded") or "")[:128],
            "recorded": raw.get("recorded") if isinstance(raw.get("recorded"), dict) else None,
            "focused_count": raw.get("focused_count") if isinstance(raw.get("focused_count"), int) else None,
            "edit_texts": [
                {k: row.get(k) for k in ("focused", "enabled", "bounds", "hint", "resource_id", "package")}
                for row in (edits[:8] if isinstance(edits, list) else []) if isinstance(row, dict)
            ],
            "dump_error": str(raw.get("dump_error") or raw.get("diag_error") or "")[:120],
        }
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
