"""Read-only I04 migration preflight. No ORM, model calls, or write operation.

Only recognized platform fields are projected. Author text, secrets, historical
session evidence and complete JSON documents are never returned in the report.
Suggestions are not an executable migration: revisions and live references must
be switched together after N1/N2 acceptance and an operator-reviewed preflight.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

_ALIASES = {"mac": "mac", "macos": "mac", "darwin": "mac", "osx": "mac",
            "win": "win", "windows": "win", "win32": "win", "android": "android", "web": "web"}
_KNOWN = set(_ALIASES) | {"desktop", "pc", "mobile", "移动端", "ios", "iphone", "ipad", "linux",
                         "server", "any", "all", "mixed", "other", "playwright", ""}
_SKIP = {"password", "token", "secret", "api_key", "secrets", "channel_secrets", "plugins",
         "params", "schema", "properties", "input_schema", "output_schema"}
# Explicit inventories, not SELECT *: unrelated account data is never read.
_TABLES = {
    "tasks": ("status",), "case_compile_jobs": ("status",),
    "apps": ("platforms", "env"), "projects": ("env",),
    "project_cases": ("platform", "extra"), "m_device": ("sn", "platform", "node_id", "extra"),
    "nodes": ("extra",), "app_regression_runs": ("status", "payload"),
    "device_reservations": ("resource_key", "run_id"),
    "device_app_sessions": ("sn", "platform", "extra"),
    "catalog_entries": ("platforms_json", "payload_json"),
    "resource_transition_rules": ("platform", "effects_json"),
    "nav_flow_block_catalog": ("steps_json",),
    "device_resource_leases": ("sn", "platform"),
    "ui_controls": ("locator_spec", "locator_value"),
}
_TEXT_TABLES = {"skills", "knowledge_entries", "llm_jobs", "settings"}
_PLATFORM_FIELDS = {"platform", "platforms", "platform_scope", "platforms_by_sn", "native_os", "device_platform", "target_platform"}
_SN_FIELDS = {"sn", "sns", "device_sn"}
_ACTIVE_WORK = {"tasks": ("pending", "queued", "running"),
                "case_compile_jobs": ("queued", "running")}


def _safe_value(value):
    if isinstance(value, list):
        return [_safe_value(v) for v in value[:32]]
    if isinstance(value, str) and value.lower() in _KNOWN:
        return value
    return "<unrecognized>"


def suggest_platform(value, *, authoritative_host=None, windows_channel=False, android_only=False) -> dict:
    """Offline-only interpretation with explicit evidence for ambiguous aliases."""
    old = _safe_value(value)
    if not isinstance(value, str):
        return {"old": old, "state": "blocked", "reason": "platform_not_string"}
    key = value.lower()
    if key in _ALIASES:
        new = _ALIASES[key]
        return {"old": old, "new": new, "state": "unchanged" if value == new else "candidate",
                "reason": "canonical" if value == new else "documented_platform_alias"}
    if key == "desktop":
        system = _ALIASES.get(str(authoritative_host or "").lower())
        if system in ("mac", "win"):
            return {"old": old, "new": system, "state": "candidate", "reason": "device_recorded_os"}
    if key == "pc" and windows_channel:
        return {"old": old, "new": "win", "state": "candidate", "reason": "windows_channel_schema"}
    if key in ("mobile", "移动端") and android_only:
        return {"old": old, "new": "android", "state": "candidate", "reason": "reviewed_android_only_evidence"}
    reason = ("unsupported_platform_archive_required" if key in ("ios", "iphone", "ipad", "linux")
              else "collection_semantics_required" if key in ("any", "all", "mixed")
              else "platform_missing" if not key else "authoritative_evidence_required")
    return {"old": old, "state": "blocked", "reason": reason}


def _decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _walk(value, path, findings, *, depth=0):
    if depth > 40:
        findings.append({"field": path, "state": "blocked", "reason": "document_depth_limit"})
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]", findings, depth=depth + 1)
        return
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        if not isinstance(key, str) or key.lower() in _SKIP or any(s in key.lower() for s in ("password", "secret", "token", "credential")):
            continue
        # Arbitrary map keys can contain user text. Do not print them.
        label = key if key in _PLATFORM_FIELDS | _SN_FIELDS | {"host", "os", "channels", "runtime_capabilities", "device_targets", "resource_key"} else "*"
        field = f"{path}.{label}"
        if key == "platform_scope":
            if child not in ("all", "explicit"):
                findings.append({"field": field, "state": "blocked", "reason": "platform_scope_invalid"})
        elif key == "platform":
            if value.get("kind") == "server":
                findings.append({"field": field, "state": "candidate", "old": _safe_value(child),
                                 "new": "<remove field>", "reason": "server_is_channel_kind"})
            else:
                windows = value.get("kind") == "app" and value.get("field") == "path" and value.get("id") == "pc"
                suggestion = suggest_platform(child, windows_channel=windows)
                if suggestion["state"] != "unchanged":
                    findings.append({"field": field, **suggestion})
        elif key == "platforms":
            members = _decoded(child)
            if not isinstance(members, list):
                findings.append({"field": field, "state": "blocked", "reason": "platform_array_required"})
                # Legacy apps used a comma string; report each token without
                # changing its scope or interpreting Mobile as Android.
                members = child.split(",") if isinstance(child, str) else []
            for index, member in enumerate(members):
                suggestion = suggest_platform(member)
                if suggestion["state"] != "unchanged":
                    findings.append({"field": f"{field}[{index}]", **suggestion})
        elif key == "platforms_by_sn" and isinstance(child, dict):
            for index, (sn, platform) in enumerate(child.items()):
                suggestion = suggest_platform(platform)
                if suggestion["state"] != "unchanged":
                    findings.append({"field": f"{field}[{index}]", **suggestion})
                if isinstance(sn, str) and sn.startswith("desktop:"):
                    findings.append({"field": f"{field}[{index}].key", "state": "blocked", "reason": "device_reference_requires_atomic_switch"})
        elif key in ("native_os", "device_platform", "target_platform"):
            findings.append({"field": field, "state": "blocked", "reason": "duplicate_platform_consumer"})
        elif key in _SN_FIELDS:
            values = child if isinstance(child, list) else [child]
            for index, sn in enumerate(values):
                if isinstance(sn, str) and sn.startswith("desktop:"):
                    findings.append({"field": f"{field}[{index}]", "state": "blocked", "reason": "device_reference_requires_atomic_switch"})
        elif key == "device_targets" and isinstance(child, dict):
            for index, (sn, target) in enumerate(child.items()):
                if isinstance(sn, str) and sn.startswith("desktop:"):
                    findings.append({"field": f"{field}[{index}].key", "state": "blocked", "reason": "device_reference_requires_atomic_switch"})
                _walk(target, f"{field}[{index}]", findings, depth=depth + 1)
        elif key == "resource_key" and isinstance(child, str):
            # desktop:* is a resource domain, not a target platform alias.
            continue
        elif key == "host" and isinstance(child, dict):
            system = child.get("os")
            if system not in ("mac", "win", "linux"):
                new = _ALIASES.get(str(system or "").lower())
                findings.append({"field": field + ".os", "state": "candidate" if new in ("mac", "win") else "blocked",
                                 "old": _safe_value(system), "new": new if new in ("mac", "win") else None,
                                 "reason": "host_boundary_migration"})
        else:
            if key.startswith("desktop:"):
                findings.append({"field": field + ".key", "state": "blocked",
                                 "reason": "unclassified_device_reference_requires_review"})
            _walk(child, field, findings, depth=depth + 1)


def audit_database(database_path: str | Path) -> dict:
    """Read one consistent SQLite snapshot; do not migrate or copy the DB."""
    source = Path(database_path).resolve(strict=True)
    if not source.is_file():
        raise ValueError("database_file_required")
    report = {"schema": "i04-platform-preflight-v1", "mode": "read_only", "cutover_ready": False,
              "reviewer": None, "tables": {}, "findings": [], "blockers": ["n1_n2_and_operator_cutover_not_verified"],
              "excluded": ["session_events", "session_meta", "author_text", "credentials"],
              "limits": ["No data is modified; candidates require reference and revision review.",
                         "Custom text and unrecognized schemas require a separate reviewed inventory."]}
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(tables & (_TABLES.keys() | _TEXT_TABLES)):
            count = db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            report["tables"][table] = {"rows": count, "scanned": 0}
            if table in _TEXT_TABLES:
                if count:
                    report["findings"].append({"entity": table, "state": "blocked", "reason": "custom_content_inventory_required", "rows": count})
                continue
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            selected = [name for name in _TABLES[table] if name in columns]
            if not selected:
                if count:
                    report["findings"].append({"entity": table, "state": "blocked", "reason": "table_schema_not_covered"})
                continue
            projection = ','.join('"' + column + '"' for column in selected)
            for ordinal, row in enumerate(db.execute(f'SELECT rowid,{projection} FROM "{table}"'), 1):
                rowid, *values = row
                document = dict(zip(selected, values))
                findings = []
                if table in _ACTIVE_WORK:
                    if document.get("status") in _ACTIVE_WORK[table]:
                        findings.append({"state": "blocked", "reason":
                            "active_compile_prevents_cutover" if table == "case_compile_jobs" else "active_task_prevents_cutover"})
                # Completed runs are immutable evidence, not migration targets.
                if table == "app_regression_runs":
                    payload = _decoded(document.get("payload"))
                    if document.get("status") in ("running", "queued"):
                        findings.append({"state": "blocked", "reason": "active_run_prevents_cutover"})
                    if isinstance(payload, dict) and _run_unresolved(payload):
                        findings.append({"state": "blocked", "reason": "unknown_write_prevents_cutover"})
                    if not findings:
                        report["tables"][table]["historical_preserved"] = report["tables"][table].get("historical_preserved", 0) + 1
                        continue
                if table in ("device_reservations", "device_resource_leases"):
                    findings.append({"state": "blocked", "reason": "reservation_requires_quiescence", "resource_key_preserved": True})
                elif table == "m_device":
                    extra = _decoded(document.get("extra"))
                    extra = extra if isinstance(extra, dict) else {}
                    suggestion = suggest_platform(document.get("platform"), authoritative_host=extra.get("os"))
                    if suggestion["state"] != "unchanged":
                        findings.append({"field": "platform", **suggestion})
                    sn, node = document.get("sn"), document.get("node_id")
                    if isinstance(sn, str) and sn.startswith("desktop:"):
                        if suggestion.get("new") in ("mac", "win") and isinstance(node, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", node) and sn == f"desktop:{node}:console":
                            findings.append({"field": "sn", "state": "candidate", "old": sn,
                                             "new": f'{suggestion["new"]}:{node}:console', "reason": "device_recorded_os",
                                             "resource_key_preserved": f"desktop:{node}:console"})
                        else:
                            findings.append({"field": "sn", "state": "blocked", "reason": "device_identity_requires_review"})
                    if extra.get("os"):
                        findings.append({"field": "extra.os", "state": "blocked", "reason": "duplicate_platform_consumer"})
                else:
                    for column, value in document.items():
                        parsed = _decoded(value)
                        # apps.platforms is deliberately stored as a comma string;
                        # nested platform arrays still require the array contract.
                        if table == "apps" and column == "platforms" and isinstance(value, str):
                            parsed = value.split(",") if value else [""]
                        if column in ("platform", "platforms", "platforms_json") or column in _SN_FIELDS:
                            _walk({"platforms" if column == "platforms_json" else column: parsed}, "row", findings)
                        elif isinstance(parsed, (dict, list)):
                            _walk(parsed, column, findings)
                        elif value and column not in ("sn", "status", "node_id"):
                            findings.append({"field": column, "state": "blocked", "reason": "unstructured_content_requires_review"})
                report["tables"][table]["scanned"] += 1
                report["findings"].extend({"entity": table, "rowid": rowid, "reviewer": None, **finding} for finding in findings)
        db.rollback()
    report["counts"] = dict(Counter(item["state"] for item in report["findings"]))
    report["migration_required"] = bool(report["findings"])
    return report


def _run_unresolved(payload):
    """Use the durable action ledger, not immutable case failure summaries.

    Legacy runs without a ledger keep the conservative recursive check. A
    present but malformed ledger fails closed. No result or lease is changed.
    """
    ledger = payload.get("write_safety") if isinstance(payload, dict) else None
    if ledger is None or ledger == {}:
        return _unresolved(payload)
    if not isinstance(ledger, dict):
        return True
    from mino_nexus.services.run_write_safety import _proof_mode_valid
    for key, item in ledger.items():
        if not isinstance(item, dict):
            return True
        ref = item.get("action_ref")
        if (not isinstance(ref, dict) or not isinstance(ref.get("run_id"), str)
                or not ref["run_id"] or type(ref.get("step_idx")) is not int
                or key != f'{ref["run_id"]}:{ref["step_idx"]}'
                or not isinstance(item.get("sn"), str) or not item["sn"]
                or item.get("stop_confirmed") is not True):
            return True
        if item.get("state") == "settled":
            if item.get("execution_started") not in ("yes", "no"):
                return True
        elif item.get("state") == "recovered":
            recovery = item.get("recovery")
            proof = recovery.get("proof") if isinstance(recovery, dict) else None
            if (not isinstance(proof, dict) or not recovery.get("reviewed_by")
                    or proof.get("contract") != "write_stop.v1" or proof.get("stop_confirmed") is not True
                    or proof.get("action_ref") != ref or proof.get("sn") != item["sn"]
                    or not isinstance(proof.get("scout_process_generation"), str)
                    or not proof["scout_process_generation"]
                    or not _proof_mode_valid(proof, str(item.get("scout_process_generation") or ""),
                                             proof["scout_process_generation"], payload)):
                return True
        else:
            return True
    # Only per-case summaries are immutable projections. Independent top-level
    # isolation/unknown state must not be hidden by a recovered action ledger.
    return _unresolved({k: v for k, v in payload.items() if k not in {"write_safety", "cases"}})


def _unresolved(payload):
    """Treat write intents conservatively without importing storage services."""
    if isinstance(payload, dict):
        if payload.get("state") == "recovered" and payload.get("stop_confirmed") is True:
            return False
        if payload.get("resource_isolated") is True or payload.get("execution_started") == "unknown":
            return True
        if payload.get("state") in ("pending", "isolated") and ("stop_confirmed" in payload or "capability_id" in payload):
            return True
        return any(_unresolved(v) for k, v in payload.items() if k not in _SKIP)
    return isinstance(payload, list) and any(_unresolved(v) for v in payload)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Read-only I04 platform migration preflight; does not apply changes")
    parser.add_argument("database", help="Explicit SQLite database path")
    args = parser.parse_args(argv)
    report = audit_database(args.database)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["counts"].get("blocked", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
