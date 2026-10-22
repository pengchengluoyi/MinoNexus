from __future__ import annotations

from copy import deepcopy
import json
import re
from urllib.parse import urlsplit


def identifier(value, *, field=""):
    if type(value) not in (int, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise ValueError(f"source_api_{field}_id_invalid" if field else "source_api_id_invalid")
    return str(value)


from mino_nexus.core.url_norm import origin  # noqa: F401
def configuration(config, *, operation="read"):
    if not isinstance(config, dict) or set(config) - {
        "node_id", "service_url", "remote_project_id", "scope", "session_id", "app_id", "default_platform"
    }:
        raise ValueError("source_api_configuration_invalid")
    result = deepcopy(config)
    if not isinstance(result.get("node_id"), str) or not result["node_id"].strip() or len(result["node_id"]) > 200:
        raise ValueError("source_node_required")
    result["node_id"] = result["node_id"].strip()
    if operation == "read" or result.get("service_url"):
        result["service_url"] = origin(result.get("service_url"))
    result["scope"] = result.get("scope", "project")
    if not isinstance(result["scope"], str) or result["scope"] not in {"project", "session"}:
        raise ValueError("source_api_scope_invalid")
    if operation != "projects":
        result["remote_project_id"] = identifier(result.get("remote_project_id"))
    else:
        result.pop("remote_project_id", None)
    if operation == "read" and result["scope"] == "session":
        result["session_id"] = identifier(result.get("session_id"))
    else:
        result.pop("session_id", None)
    if not isinstance(result.get("app_id", ""), str) or len(result.get("app_id", "")) > 200:
        raise ValueError("source_api_configuration_invalid")
    if not isinstance(result.get("default_platform", ""), str) or result.get("default_platform", "") not in {"", "web", "android", "ios", "mac", "win", "linux"}:
        raise ValueError("source_platform_invalid")
    return result


def _text(value, *, structured=False):
    if value is None:
        return ""
    if isinstance(value, str):
        content = value
    elif structured and isinstance(value, list):
        try:
            content = "\n".join(value) if all(isinstance(item, str) for item in value) else json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("source_api_field_invalid") from None
    elif type(value) in (int, bool):
        content = str(value)
    else:
        raise ValueError("source_api_field_invalid")
    if len(content) > 60000:
        raise ValueError("source_cell_too_large")
    return content


def normalize(records, config):
    if not isinstance(records, list) or len(records) > 10000:
        raise ValueError("source_api_payload_invalid")
    rows, seen = [], set()
    for record in records:
        if not isinstance(record, dict) or not {"id", "name", "steps", "expected_result"} <= record.keys():
            raise ValueError("source_api_payload_invalid")
        number = identifier(record["id"], field="case")
        if number in seen:
            raise ValueError("source_api_duplicate_id")
        seen.add(number)
        raw_session_id = record.get("session_id")
        session_id = (identifier(raw_session_id, field="record_session") if raw_session_id is not None
            else config["session_id"] if config["scope"] == "session" else "")
        if config["scope"] == "session" and session_id != config["session_id"]:
            raise ValueError("source_api_session_mismatch")
        if record.get("project_id") is not None and identifier(record["project_id"], field="record_project") != config["remote_project_id"]:
            raise ValueError("source_api_project_mismatch")
        fields = {target: _text(record.get(source), structured=target in {"steps_raw", "expected_raw"}) for target, source in (
            ("name", "name"), ("precondition", "preconditions"), ("module", "module"),
            ("steps_raw", "steps"), ("expected_raw", "expected_result"))}
        fields.update(platform=config.get("default_platform", ""), app_id=config.get("app_id", ""))
        position = {"api_case_id": number, "remote_project_id": config["remote_project_id"], "session_id": session_id}
        for key in ("case_number", "priority", "remarks", "session_title", "test_result", "created_at"):
            if key in record:
                position[key] = _text(record[key])
        rows.append({"number": number, "fields": fields, "position": position})
    return rows
