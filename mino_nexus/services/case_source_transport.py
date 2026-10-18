import inspect
import time

from mino_nexus.core import protocol as P
from mino_nexus.core.wire_identity import assign_execute_identity
from mino_nexus.loop.router_proxy import _run_sync
from mino_nexus.services.node_registry import get_registry


def read_source(snapshot, *, operation="read"):
    node = get_registry().get_node(snapshot["configuration"]["node_id"])
    if node is None or node.owner_user_id != snapshot["owner_user_id"] or not node.alive or node.send is None:
        raise ValueError("source_node_unavailable")
    contracts = [row for row in node.runtime_capabilities or [] if row.get("id") == "node.source_read"]
    if not any(row.get("contract_version") == 1 for row in contracts):
        raise ValueError("source_node_contract_unsupported")
    if snapshot["kind"] == "case_api" and not any(row.get("contract_version") == 1 and "case_api" in row.get("features", []) for row in contracts):
        raise ValueError("source_node_case_api_unsupported")
    if time.time() - node.last_seen > 2 * P.Registered.heartbeat_interval_sec:
        raise ValueError("source_node_heartbeat_stale")
    params = {**snapshot["configuration"], "kind": snapshot["kind"], "source_id": snapshot["source_id"],
        "read_id": snapshot["read_id"], "generation": snapshot["generation"], "contract_version": 1, "operation": operation}
    if snapshot["kind"] == "case_api":
        from mino_nexus.services.case_source_api import origin
        from mino_nexus.services.settings_store import get_case_api_settings
        service_url = get_case_api_settings()["base_url"]
        if params.get("service_url") and origin(params["service_url"]) != service_url:
            raise ValueError("source_api_service_mismatch")
        params["service_url"] = service_url
    request = P.Execute(run_id="", step_idx=-1, sn="", capability_id="node.source_read", params=params,
        timeout_sec=120, scout_process_generation=node.scout_process_generation,
        node_connection_generation=node.node_connection_generation, capability_revision=node.capability_revision)
    assign_execute_identity(request)
    async def once():
        result = node.send(P.MsgType.EXECUTE, request, timeout=125)
        return await result if inspect.isawaitable(result) else result
    result = _run_sync(once())
    if result is None:
        raise ValueError("source_node_read_failed")
    data = dict(getattr(result, "data", None) or getattr(result, "extra", None) or {})
    if str(getattr(result.status, "value", result.status)) != "pass":
        code = str(data.get("reason_code") or "source_node_read_failed")
        raise ValueError(code if code.startswith("source_") and code.replace("_", "").isalnum() else "source_node_read_failed")
    if any(data.get(key) != snapshot.get(key) for key in ("source_id", "read_id", "generation")):
        raise ValueError("source_read_identity_mismatch")
    return data
