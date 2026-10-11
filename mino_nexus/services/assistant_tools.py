"""助手工具：从 prompts/assistant/tools 读取，进程内调用现有路由。"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from mino_nexus.ai.prompt_repo import list_assistant_tools

_APP: Any = None
_PARAM = re.compile(r"\{([^{}]+)\}")


def bind_app(app: Any) -> None:
    global _APP
    _APP = app


def _app() -> Any:
    if _APP is not None:
        return _APP
    from mino_nexus.app import create_app

    return create_app()


def assert_no_delete(route: str) -> None:
    method, _, path = str(route).partition(" ")
    if method.upper() == "DELETE" or "/delete" in path.lower():
        raise ValueError(f"删除类接口不能进助手工具表：{route}")


def all_tools() -> list[dict[str, Any]]:
    rows = []
    for spec in list_assistant_tools():
        if spec.get("enabled") is False:
            continue
        route = str(spec.get("route") or "")
        assert_no_delete(route)
        if spec.get("risk") not in ("read", "write"):
            raise ValueError(f"工具档位只有 read / write：{spec.get('id')}")
        rows.append(spec)
    return rows


def get_tool(tool_id: str) -> dict[str, Any] | None:
    for spec in all_tools():
        if spec.get("id") == tool_id:
            return spec
    return None


def list_tools_public() -> list[dict[str, Any]]:
    out = []
    for spec in all_tools():
        out.append({
            "id": spec.get("id"),
            "title": spec.get("title") or spec.get("id"),
            "description": spec.get("description") or "",
            "risk": spec.get("risk"),
            "surfaces": list(spec.get("surfaces") or ["im", "mcp"]),
            "route": spec.get("route"),
            "async": bool(spec.get("async")),
            "params_schema": spec.get("params_schema") or {"type": "object", "properties": {}},
            "source_path": spec.get("source_path") or "",
            "version": spec.get("version"),
            "sha": spec.get("sha256") or "",
        })
    return out


def openai_tools() -> list[dict[str, Any]]:
    tools = []
    for spec in all_tools():
        name = str(spec.get("id") or "").replace(".", "_")
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(spec.get("description") or spec.get("title") or name),
                "parameters": spec.get("params_schema") or {"type": "object", "properties": {}},
            },
        })
    tools.append({
        "type": "function",
        "function": {
            "name": "reply",
            "description": "不需要调用业务接口时，直接用这段文字回答用户。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    })
    return tools


def tool_by_function_name(name: str) -> dict[str, Any] | None:
    raw = str(name or "")
    if raw == "reply":
        return None
    for spec in all_tools():
        if str(spec.get("id") or "").replace(".", "_") == raw or spec.get("id") == raw:
            return spec
    return None


def route_allowed(method: str, path: str) -> bool:
    """助手 token 和 MCP token 只能打工具表里的路由，以及 MCP 端点本身。删除一律拒绝。"""
    method = method.upper()
    path = path.split("?", 1)[0]
    if method == "DELETE" or "/delete" in path.lower():
        return False
    if method == "POST" and path == "/mcp":
        return True
    for spec in all_tools():
        route = str(spec.get("route") or "")
        rmethod, _, rpath = route.partition(" ")
        if rmethod.upper() != method:
            continue
        pattern = "^" + _PARAM.sub(r"[^/]+", rpath) + "$"
        if re.match(pattern, path):
            return True
    return False


def write_allowed(method: str, path: str) -> bool:
    method = method.upper()
    path = path.split("?", 1)[0]
    if method == "DELETE" or "/delete" in path.lower():
        return False
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    for spec in all_tools():
        route = str(spec.get("route") or "")
        rmethod, _, rpath = route.partition(" ")
        if rmethod.upper() != method:
            continue
        pattern = "^" + _PARAM.sub(r"[^/]+", rpath) + "$"
        if re.match(pattern, path):
            return True
    return False


@dataclass
class ToolOutcome:
    ok: bool
    status: int = 0
    data: Any = None
    summary: str = ""
    ask: str = ""
    options: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    tool_id: str = ""


def _pick(data: Any, paths: list[str]) -> Any:
    if not paths:
        return data
    for path in paths:
        cur: Any = data
        for part in str(path).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, "", [], {}):
            return cur
    return data


def _match_name(rows: list[dict[str, Any]], text: str, *keys: str) -> list[dict[str, Any]]:
    needle = str(text or "").strip().lower()
    if not needle:
        return []
    exact, partial = [], []
    for row in rows:
        for key in keys:
            value = str(row.get(key) or "").strip()
            if not value:
                continue
            low = value.lower()
            if low == needle:
                exact.append(row)
                break
            if needle in low or (len(low) >= 2 and low in needle):
                partial.append(row)
                break
    return exact or partial


def _resolve_one(resolver: str, value: str) -> tuple[str, str, list[str], bool]:
    """返回 (id, label, 候选名)。多个候选时 id 为空。"""
    text = str(value or "").strip()
    if not text or resolver == "":
        return text, text, [], False
    if resolver == "project.by_name":
        from mino_nexus.services.project_store import list_projects

        rows = [{"id": p.get("id"), "name": p.get("name")} for p in list_projects()]
    elif resolver == "app.by_name":
        from mino_nexus.services.project_store import list_projects

        rows = []
        for project in list_projects():
            for app in project.get("apps") or []:
                rows.append({"id": app.get("id"), "name": app.get("name"), "project_id": project.get("id")})
    elif resolver == "device.by_alias":
        from mino_nexus.services.ui_devices import ui_devices

        rows = []
        for device in ui_devices():
            rows.append({
                "id": device.get("sn"),
                "name": device.get("alias") or device.get("model") or device.get("name") or device.get("sn"),
                "sn": device.get("sn"),
            })
    else:
        return text, text, [], False
    hits = _match_name(rows, text, "id", "name", "sn")
    if len(hits) == 1:
        return str(hits[0].get("id") or ""), str(hits[0].get("name") or hits[0].get("id") or ""), [], False
    if not hits:
        names = [str(r.get("name") or r.get("id") or "") for r in rows[:8] if r.get("name") or r.get("id")]
        return "", "", names, False
    options = [str(r.get("name") or r.get("id") or "") for r in hits[:6]]
    return "", "", options, True


_GENERIC_DEVICE = frozenset({"默认设备", "默认", "设备", "那台", "这台", "我的设备", "default", "默认手机", "手机"})


def resolve_args(spec: dict[str, Any], args: dict[str, Any]) -> tuple[dict[str, Any], str, list[str], list[str]]:
    """解析自然语言。返回 args, ask, options, 用了默认值的说明。"""
    notes: list[str] = []
    out = dict(args or {})
    resolvers = spec.get("param_resolvers") if isinstance(spec.get("param_resolvers"), dict) else {}
    for key, resolver in resolvers.items():
        raw = out.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, list):
            continue
        if key == "sn" and str(raw).strip().lower() in _GENERIC_DEVICE:
            out.pop(key, None)  # 「默认设备」「那台」不是设备名：删掉，交给默认值逻辑去填
            continue
        ident, label, options, ambiguous = _resolve_one(str(resolver), str(raw))
        if not ident:
            title = spec.get("title") or spec.get("id")
            if ambiguous:
                return out, f"{title}：{key}「{raw}」对上了多个，请选一个：{'、'.join(options)}", options, notes
            hint = f"，可选：{'、'.join(options)}" if options else ""
            return out, f"没有找到{key}「{raw}」{hint}", options, notes
        if ident != str(raw):
            notes.append(f"{key} 用的是 {label}")
        out[key] = ident
    return out, "", [], notes


def _fill_default_sn(spec: dict[str, Any], args: dict[str, Any], profile: dict[str, Any]) -> tuple[dict[str, Any], str]:
    props = (spec.get("params_schema") or {}).get("properties") or {}
    if "sn" not in props or args.get("sn"):
        return args, ""
    default_sn = str((profile or {}).get("default_sn") or "").strip()
    if default_sn:
        ident, label, options, _amb = _resolve_one("device.by_alias", default_sn)
        if ident:
            args = dict(args)
            args["sn"] = ident
            return args, f"设备用的是 {label or ident}（默认设备）"
        if options:
            return args, ""
    from mino_nexus.services.ui_devices import ui_devices

    online = [d for d in ui_devices() if str(d.get("status") or "") == "online" and d.get("sn")]
    if len(online) == 1:
        args = dict(args)
        args["sn"] = str(online[0].get("sn"))
        name = online[0].get("alias") or online[0].get("model") or args["sn"]
        return args, f"设备用的是 {name}（唯一在线）"
    return args, ""


def _asgi_request(method: str, url: str, rest: dict[str, Any], headers: dict[str, str]) -> "httpx.Response":
    """进程内调用现有路由。ASGITransport 只能配异步客户端，所以在自己的事件循环里跑；
    调用方线程里已经有事件循环（例如 MCP 的异步处理函数）时换一个线程再跑。"""
    import asyncio
    import concurrent.futures

    async def _once() -> "httpx.Response":
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://nexus", timeout=60) as client:
            if method == "GET":
                return await client.request(method, url, params=rest, headers=headers)
            return await client.request(method, url, json=rest, headers=headers)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_once())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_once())).result()


def run_tool(spec: dict[str, Any], args: dict[str, Any], *, user_id: str, profile: dict[str, Any] | None = None, surface: str = "im") -> ToolOutcome:
    from mino_nexus.services.assistant_store import ensure_assistant_token

    route = str(spec.get("route") or "")
    assert_no_delete(route)
    method, _, path = route.partition(" ")
    method = method.upper()
    args, ask, options, notes = resolve_args(spec, args)
    if ask:
        return ToolOutcome(ok=False, ask=ask, options=options, tool_id=str(spec.get("id") or ""), summary=ask)
    args, device_note = _fill_default_sn(spec, args, profile or {})
    if device_note:
        notes.append(device_note)
    props = (spec.get("params_schema") or {}).get("properties") or {}
    if "sn" in props and spec.get("risk") == "write" and not args.get("sn"):
        from mino_nexus.services.ui_devices import ui_devices

        names = [str(d.get("alias") or d.get("model") or d.get("sn")) + f"（{d.get('sn')}）"
                 for d in ui_devices() if str(d.get("status") or "") == "online" and d.get("sn")]
        if len(names) > 1:
            msg = f"用哪台设备？在线的有：{'、'.join(names)}。也可以在 Workbench 助手页设一个默认设备。"
            return ToolOutcome(ok=False, ask=msg, options=names, tool_id=str(spec.get("id") or ""), summary=msg)
        if not names:
            msg = "现在没有在线设备，没法执行。"
            return ToolOutcome(ok=False, summary=msg, tool_id=str(spec.get("id") or ""))
    names = _PARAM.findall(path)
    missing = [name for name in names if not args.get(name)]
    if missing:
        return ToolOutcome(ok=False, summary=f"还缺 {', '.join(missing)}", tool_id=str(spec.get("id") or ""))
    url = path
    for name in names:
        url = url.replace("{" + name + "}", str(args.get(name)))
    rest = {k: v for k, v in args.items() if k not in names and v not in (None, "")}
    token = ensure_assistant_token(user_id)
    started = time.time()
    headers = {"Authorization": f"Bearer {token}", "X-Mino-Client": "assistant"}
    try:
        response = _asgi_request(method, url, rest, headers)
    except Exception as exc:
        return ToolOutcome(ok=False, summary=f"调用失败：{type(exc).__name__}", tool_id=str(spec.get("id") or ""))
    elapsed = int((time.time() - started) * 1000)
    try:
        payload = response.json()
    except Exception:
        payload = {"detail": response.text[:500]}
    picked = _pick(payload, [str(x) for x in (spec.get("result_pick") or [])])
    ok = 200 <= response.status_code < 300
    summary = ""
    if isinstance(payload, dict):
        summary = str(payload.get("msg") or "")
        data = payload.get("data")
        if isinstance(data, dict) and data.get("task_id"):
            summary = summary or f"任务 {data.get('task_id')}"
    if notes:
        summary = (summary + "；" if summary else "") + "；".join(notes)
    if not ok and not summary:
        detail = payload.get("detail") if isinstance(payload, dict) else ""
        summary = str(detail or response.status_code)
    text = json.dumps(picked, ensure_ascii=False, default=str)
    if surface != "mcp" and len(text) > 4000:
        picked = {"preview": text[:4000]}
    return ToolOutcome(
        ok=ok,
        status=response.status_code,
        data=picked,
        summary=summary or ("完成" if ok else "失败"),
        elapsed_ms=elapsed,
        tool_id=str(spec.get("id") or ""),
    )
