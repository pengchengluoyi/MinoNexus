"""MCP 端点（Streamable HTTP，JSON-RPC over POST）。工具来源是助手工具表，不是全量 OpenAPI。

鉴权：Workbench 生成的 MCP token。工具以 token 主人的身份经 run_tool 执行，
走的是和 IM 助手同一条路径，所以同样受门禁约束。
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import os
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from mino_nexus.core.http_util import bearer
from mino_nexus.services import assistant_store as store
from mino_nexus.services.assistant_tools import all_tools, run_tool, tool_by_function_name


def function_name(tool_id: str) -> str:
    return str(tool_id).replace(".", "_")

router = APIRouter(tags=["MCP"])

PROTOCOL = "2025-03-26"


def _rpc_error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tools_payload() -> list[dict[str, Any]]:
    return [
        {
            "name": function_name(t["id"]),
            "description": t.get("description", ""),
            "inputSchema": t.get("params_schema") or {"type": "object", "properties": {}},
        }
        for t in all_tools()
        if "mcp" in (t.get("surfaces") or ["mcp"])
    ]


def _call(uid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    import time

    spec = tool_by_function_name(name)
    if spec is None or "mcp" not in (spec.get("surfaces") or ["mcp"]):
        return {"isError": True, "content": [{"type": "text", "text": f"没有这个工具：{name}"}]}
    profile = store.get_profile(uid)
    started = time.time()
    out = run_tool(spec, args if isinstance(args, dict) else {}, user_id=uid, profile=profile, surface="mcp")
    body = {"ok": out.ok, "summary": out.summary, "data": out.data}
    if out.ask:
        body["ask"] = out.ask
        body["options"] = out.options
    store.save_turn({
        "turn_id": store.new_turn_id(), "user_id": uid, "surface": "mcp", "channel": "mcp",
        "status": "ok" if out.ok else "error", "intent": spec.get("title") or spec["id"],
        "steps": [{"tool": spec["id"], "label": spec.get("title") or spec["id"], "status": "ok" if out.ok else "error",
                   "ms": out.elapsed_ms, "args_brief": json.dumps(args, ensure_ascii=False)[:180]}],
        "elapsed_ms": int((time.time() - started) * 1000),
    })
    return {
        "isError": not out.ok,
        "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False, default=str)}],
    }


def _handle(uid: str, msg: dict[str, Any]) -> dict[str, Any] | None:
    method = str(msg.get("method") or "")
    rid = msg.get("id")
    if msg.get("jsonrpc") != "2.0" or not method or ("params" in msg and not isinstance(msg["params"], dict)) or isinstance(rid, bool) or (rid is not None and not isinstance(rid, (str, int))):
        return _rpc_error(None, -32600, "无效的 JSON-RPC 2.0 请求")
    if rid is None and method.startswith("notifications/"):
        return None
    if rid is None:
        return _rpc_error(None, -32600, "工具请求必须包含稳定的 id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mino-nexus", "version": "1"},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _tools_payload()}}
    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        return {"jsonrpc": "2.0", "id": rid, "result": _call(uid, str(params.get("name") or ""), params.get("arguments") or {})}
    return _rpc_error(rid, -32601, f"不支持的方法 {method}")


def _handle_owned(uid: str, msg: dict, token_scope: str):
    from mino_nexus.core.execution_user import execution_user
    from mino_nexus.ai import dispatch_log
    with execution_user(uid):
        token = dispatch_log.bind(assistant_mutation_scope=f"mcp:{token_scope}:{msg.get('id')}")
        try:
            return _handle(uid, msg)
        finally:
            dispatch_log.reset(token)


@router.post("/mcp")
async def mcp_post(request: Request, authorization: str = Header(default="")):
    origin = request.headers.get("origin", "")
    allowed = {value.strip().rstrip("/") for value in os.environ.get("MINO_MCP_ALLOWED_ORIGINS", "").split(",") if value.strip()}
    allowed.add(str(request.base_url).rstrip("/"))
    if origin and origin.rstrip("/") not in allowed:
        return JSONResponse({"detail": "MCP Origin 未获允许"}, status_code=403)
    requested_version = request.headers.get("mcp-protocol-version")
    if requested_version and requested_version != PROTOCOL:
        return JSONResponse({"detail": f"仅支持 MCP {PROTOCOL}"}, status_code=400)
    probe = store.resolve_probe_token(bearer(authorization))
    uid = "system:probe" if probe else store.resolve_mcp_token(bearer(authorization))
    if not uid:
        return JSONResponse({"detail": "MCP token 无效或已吊销"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, -32700, "不是合法 JSON"), status_code=400)
    items = body if isinstance(body, list) else [body]
    out = []
    for msg in items:
        if not isinstance(msg, dict):
            out.append(_rpc_error(None, -32600, "无效请求"))
            continue
        if probe:
            if msg.get("method") not in {"initialize", "ping", "tools/list"}:
                out.append(_rpc_error(msg.get("id"), -32601, "probe token 仅允许 initialize/ping/tools/list"))
            else:
                out.append(await asyncio.to_thread(_handle, uid, msg))
            continue
        scope = hashlib.sha256(bearer(authorization).encode()).hexdigest()
        res = await asyncio.to_thread(_handle_owned, uid, msg, scope)
        if res is not None:
            out.append(res)
    if not out:
        return Response(status_code=202)
    return JSONResponse(out if isinstance(body, list) else out[0])


@router.get("/mcp")
async def mcp_get() -> Response:
    # 不提供服务端主动推送流；客户端会回落到纯 POST
    return Response(status_code=405, headers={"Allow": "POST"})
