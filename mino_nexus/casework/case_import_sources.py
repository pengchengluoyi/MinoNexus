from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qs, urlsplit

from mino_nexus.core.database import session_scope
from mino_nexus.platform.project_guard import owned
from mino_nexus.casework.case_source_transport import read_source
from mino_nexus.resources.node_registry import get_registry


def nodes(project_id):
    with session_scope() as db:
        actor = owned(db, project_id)
    items = []
    for node in get_registry().nodes():
        if node.owner_user_id != actor or not node.alive or node.send is None:
            continue
        features = set()
        for contract in node.runtime_capabilities or []:
            if contract.get("id") == "node.source_read" and contract.get("contract_version") == 1:
                features.update(contract.get("features") or [])
        kinds = [kind for kind in ("sheets", "case_api") if kind in features]
        if kinds:
            items.append({"id": node.node_id, "label": node.hostname or node.node_id, "kinds": kinds})
    return {"items": items}


def sheets_configuration(config):
    if set(config) - {"node_id", "spreadsheet_url", "range"}:
        raise ValueError("source_sheets_configuration_invalid")
    try:
        url = urlsplit(str(config.get("spreadsheet_url") or ""))
        hostname = url.hostname or ""
        match = re.fullmatch(r"/sheets/([A-Za-z0-9_-]+)/?", url.path)
        if (url.scheme != "https" or url.username or url.password or url.port not in (None, 443)
                or not any(hostname == domain or hostname.endswith("." + domain) for domain in ("feishu.cn", "larksuite.com"))
                or not match):
            raise ValueError
        sheet_id = parse_qs(url.query).get("sheet", [""])[0]
    except ValueError:
        raise ValueError("source_sheets_url_invalid") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sheet_id):
        raise ValueError("source_sheets_tab_required")
    cell_range = str(config.get("range") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]+[1-9][0-9]*:[A-Z]+[1-9][0-9]*", cell_range):
        raise ValueError("source_explicit_bounded_range_required")
    return {"node_id": config["node_id"], "spreadsheet_token": match[1], "sheet_id": sheet_id, "range": cell_range}


def read(project_id, kind, config, operation="read"):
    if kind not in {"sheets", "case_api"} or operation not in {"read", "projects", "sessions"}:
        raise ValueError("source_kind_invalid")
    if not isinstance(config.get("node_id"), str) or not config["node_id"].strip():
        raise ValueError("source_node_required")
    with session_scope() as db:
        actor = owned(db, project_id)
    if kind == "case_api" and operation != "read":
        from mino_nexus.casework.case_sources import discover
        return discover(project_id, config, operation)
    if kind == "case_api":
        from mino_nexus.casework.case_source_api import configuration
        config = configuration(config)
    else:
        if operation != "read":
            raise ValueError("source_kind_invalid")
        config = sheets_configuration(config)
    from mino_nexus.core.ids import prefixed
    result = read_source({"configuration": config, "kind": kind, "owner_user_id": actor,
        "source_id": prefixed("src"), "read_id": prefixed("read"), "generation": 1})
    if result.get("complete") is not True:
        raise ValueError("source_snapshot_incomplete")
    if kind == "case_api":
        from mino_nexus.casework.case_source_api import normalize
        records = normalize(result.get("records"), config)
        metadata = ("case_number", "priority", "remarks", "session_id", "session_title", "test_result", "created_at")
        table = [["编号", "用例名称", "模块", "前置条件", "步骤", "预期结果", *metadata]]
        for row in records:
            fields = row["fields"]
            table.append([f"api-{config['remote_project_id']}-{row['number']}",
                *[fields[key] for key in ("name", "module", "precondition", "steps_raw", "expected_raw")],
                *[str(row["position"].get(key, "")) for key in metadata]])
        filename = "用例接口-项目" + config["remote_project_id"]
        if config["scope"] == "session":
            filename += "-会话" + config["session_id"]
    else:
        table = result.get("table")
        filename = "飞书表格-" + config["sheet_id"]
    if not isinstance(table, list) or len(table) > 10001:
        raise ValueError("source_table_size_invalid")
    for row in table:
        if not isinstance(row, list) or len(row) > 256:
            raise ValueError("source_row_invalid")
        if any(value is not None and not isinstance(value, (str, int, float, bool)) for value in row):
            raise ValueError("source_cell_invalid")
        if any(len(str(value)) > 60000 for value in row):
            raise ValueError("source_cell_too_large")
    from mino_nexus.casework.case_import import normalize_table
    table = normalize_table(table)
    if len(table) < 2 or not any(any(cell.strip() for cell in row) for row in table[1:]):
        raise ValueError("source_import_empty")
    return {"table": table, "filename": filename}
