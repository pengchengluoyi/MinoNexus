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
        values = [str(row.get(key) or "").strip().lower() for key in keys if row.get(key)]
        if needle in values:
            exact.append(row)
        elif any(needle in low or (len(low) >= 2 and low in needle) for low in values):
            partial.append(row)
    return exact or partial


def _visible_devices(user_id: str) -> list[dict[str, Any]]:
    from mino_nexus.services import auth_store
    from mino_nexus.services.node_registry import get_registry
    from mino_nexus.services.node_store import get_node, node_visible
    from mino_nexus.services.ui_devices import ui_devices

    sess = auth_store.api_session_for(user_id) or {}
    rows = []
    for device in ui_devices():
        node_id = str(device.get("node_id") or device.get("current_scout_id") or "")
        node = get_registry().get_node(node_id)
        snapshot = node.brief() if node else (get_node(node_id) or {})
        if node_visible(snapshot, sess):
            rows.append(device)
    return rows


def _resolve_one(resolver: str, value: str, *, user_id: str = "", scope: dict[str, Any] | None = None) -> tuple[str, str, list[str], bool]:
    """返回 (id, label, 候选名)。多个候选时 id 为空。"""
    text = str(value or "").strip()
    scope = scope or {}
    if not text or resolver == "":
        return text, text, [], False
    if resolver == "project.by_name":
        from mino_nexus.services.project_store import list_projects

        rows = [{"id": p.get("id"), "name": p.get("name")} for p in list_projects()]
    elif resolver == "app.by_name":
        from mino_nexus.services.project_store import list_projects

        rows = []
        for project in list_projects():
            if scope.get("project_id") and str(project.get("id")) != str(scope["project_id"]):
                continue
            for app in project.get("apps") or []:
                rows.append({"id": app.get("id"), "name": app.get("name"), "project_id": project.get("id")})
    elif resolver == "channel.by_name":
        from mino_nexus.services import project_store as ps

        pid = str(scope.get("project_id") or "")
        if scope.get("app_id"):
            pid = str(ps.require_app(str(scope["app_id"])).get("project_id") or "")
        if not pid:
            return "", "", [], False
        rows = [{"id": c.get("id"), "name": c.get("alias") or c.get("label") or c.get("id"),
                 "alias": c.get("alias"), "label": c.get("label"), "app_identifier": c.get("app_identifier")}
                for c in ps.project_env(pid).get("channels") or []]
    elif resolver == "device.by_alias":
        from mino_nexus.services.assistant_targets import compatible_devices, device_brief

        rows = []
        for device in compatible_devices(scope, _visible_devices(user_id)):
            if str(device.get("sn") or "") == text:
                return text, device_brief(device)["display_label"], [], False
            if not device.get("executable"):
                continue
            rows.append({
                "id": device.get("sn"),
                "name": device_brief(device)["display_label"],
                "alias": device.get("alias") or device.get("model") or device.get("name"),
                "sn": device.get("sn"),
            })
    else:
        return text, text, [], False
    hits = _match_name(rows, text, "id", "name", "sn", "alias", "label", "app_identifier")
    if len(hits) == 1:
        return str(hits[0].get("id") or ""), str(hits[0].get("name") or hits[0].get("id") or ""), [], False
    if not hits:
        names = [str(r.get("name") or r.get("id") or "") for r in rows[:8] if r.get("name") or r.get("id")]
        return "", "", names, False
    options = [str(r.get("name") or r.get("id") or "") for r in hits[:6]]
    return "", "", options, True


_GENERIC_DEVICE = frozenset({"默认设备", "默认", "设备", "那台", "这台", "我的设备", "default", "默认手机", "手机"})


def resolve_args(spec: dict[str, Any], args: dict[str, Any], *, user_id: str = "") -> tuple[dict[str, Any], str, list[str], list[str]]:
    """解析本次调用参数。返回 args, ask, options, 名称解析说明。"""
    notes: list[str] = []
    out = dict(args or {})
    resolvers = spec.get("param_resolvers") if isinstance(spec.get("param_resolvers"), dict) else {}
    for key in ("project_id", "app_id", "env_surface"):
        resolver = resolvers.get(key)
        if not resolver:
            continue
        if key == "env_surface" and not out.get("app_id") and out.get("project_id"):
            from mino_nexus.services.project_store import list_apps

            apps = list_apps(str(out["project_id"]))
            if len(apps) == 1:
                out["app_id"] = str(apps[0]["id"])
            elif "app_id" in ((spec.get("params_schema") or {}).get("properties") or {}):
                options = [str(a.get("name") or a["id"]) for a in apps]
                return out, "这个项目有多个应用，请明确本次应用：" + "、".join(options), options, notes
        raw = out.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, list):
            continue
        ident, label, options, ambiguous = _resolve_one(str(resolver), str(raw), user_id=user_id, scope=out)
        if not ident:
            title = spec.get("title") or spec.get("id")
            if ambiguous:
                return out, f"{title}：{key}「{raw}」对上了多个，请选一个：{'、'.join(options)}", options, notes
            hint = f"，可选：{'、'.join(options)}" if options else ""
            return out, f"没有找到{key}「{raw}」{hint}", options, notes
        if ident != str(raw):
            notes.append(f"{key} 用的是 {label}")
        out[key] = ident
    if out.get("app_id") and "env_surface" in ((spec.get("params_schema") or {}).get("properties") or {}):
        from mino_nexus.services import project_store as ps

        app = ps.require_app(str(out["app_id"]))
        pid = str(app.get("project_id") or "")
        if out.get("project_id") and str(out["project_id"]) != pid:
            return out, "所选应用不属于本次项目，请重新选择。", [], notes
        channels = ps.project_env(pid).get("channels") or []
        if not out.get("env_surface"):
            options = [f"{c.get('alias') or c.get('label') or c['id']}（{c['id']}）" for c in channels]
            if len(channels) == 1:
                out["env_surface"] = channels[0]["id"]
            elif channels:
                return out, "本次测试哪个应用渠道？请先选择：" + "、".join(options), options, notes
    raw = out.get("sn")
    resolver = resolvers.get("sn")
    if resolver and raw not in (None, ""):
        if str(raw).strip().lower() in _GENERIC_DEVICE:
            out.pop("sn", None)
        else:
            ident, label, options, ambiguous = _resolve_one(str(resolver), str(raw), user_id=user_id, scope=out)
            if not ident:
                msg = ("设备名称有歧义，请选择：" if ambiguous else "没有找到与本次渠道兼容的设备：") + "、".join(options)
                return out, msg, options, notes
            if ident != str(raw):
                notes.append(f"sn 用的是 {label}")
            out["sn"] = ident
    return out, "", [], notes


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


def run_tool(spec: dict[str, Any], args: dict[str, Any], *, user_id: str, profile: dict[str, Any] | None = None, surface: str = "im", target: dict[str, Any] | None = None) -> ToolOutcome:
    from mino_nexus.core.execution_user import execution_user

    with execution_user(user_id):
        return _run_tool_owned(spec, args, user_id=user_id, profile=profile, surface=surface, target=target)


def _run_tool_owned(spec: dict[str, Any], args: dict[str, Any], *, user_id: str, profile: dict[str, Any] | None = None, surface: str = "im", target: dict[str, Any] | None = None) -> ToolOutcome:
    from mino_nexus.services.assistant_store import ensure_assistant_token

    route = str(spec.get("route") or "")
    assert_no_delete(route)
    method, _, path = route.partition(" ")
    method = method.upper()
    props = (spec.get("params_schema") or {}).get("properties") or {}
    args = dict(args or {})
    for key in ("project_id", "app_id", "env_surface"):
        if key in props and target and target.get(key):
            args.setdefault(key, target[key])
    args, ask, options, notes = resolve_args(spec, args, user_id=user_id)
    if ask:
        return ToolOutcome(ok=False, ask=ask, options=options, tool_id=str(spec.get("id") or ""), summary=ask)
    for key in ("project_id", "app_id", "env_surface"):
        if key in props and target and target.get(key) and args.get(key) != target[key]:
            msg = "工具选择的项目、应用或渠道与本次用户明确指定的目标不一致，请按本次目标重新选择。"
            return ToolOutcome(ok=False, ask=msg, summary=msg, tool_id=str(spec.get("id") or ""))
    if "sn" in props and spec.get("risk") == "write":
        from mino_nexus.services.assistant_targets import compatible_devices, device_brief

        device_args = dict(args)
        if spec.get("id") == "case.retry_failed" and args.get("task_id"):
            from mino_nexus.services.run_store import get

            previous = get(str(args["task_id"])) or {}
            device_args = {**{k: previous.get(k) for k in ("app_id", "env_surface", "env_profile", "action_scheme")}, **args}
        devices = compatible_devices(device_args, _visible_devices(user_id))
        if args.get("sn"):
            device = next((row for row in devices if str(row.get("sn") or "") == str(args["sn"])), None)
            if device is None or not device.get("executable"):
                msg = str((device or {}).get("blocked_reason") or "这台设备不可执行本次应用渠道，或你没有访问权限，请选择兼容且就绪的设备。")
                return ToolOutcome(ok=False, summary=msg, tool_id=str(spec.get("id") or ""))
        else:
            names = [device_brief(d)["display_label"] for d in devices if d.get("executable") and d.get("sn")]
            if not names:
                reasons = list(dict.fromkeys(str(d.get("blocked_reason") or "设备未就绪") for d in devices))
                msg = "当前应用渠道没有可执行设备。" + ("；".join(reasons) if reasons else "请连接同系统、兼容且属于当前账号的设备。")
                return ToolOutcome(ok=False, summary=msg, tool_id=str(spec.get("id") or ""))
            msg = f"本次用哪台设备？当前应用渠道兼容且可执行的设备：{'、'.join(names)}。"
            return ToolOutcome(ok=False, ask=msg, options=names, tool_id=str(spec.get("id") or ""), summary=msg)
    names = _PARAM.findall(path)
    required = list((spec.get("params_schema") or {}).get("required") or [])
    missing = [name for name in dict.fromkeys(names + required) if args.get(name) in (None, "", [])]
    if missing:
        msg = f"请明确本次的 {', '.join(missing)}。"
        return ToolOutcome(ok=False, ask=msg, summary=msg, tool_id=str(spec.get("id") or ""))
    url = path
    for name in names:
        url = url.replace("{" + name + "}", str(args.get(name)))
    rest = {k: v for k, v in args.items() if k not in names and k != "project_id" and v not in (None, "")}
    if spec.get("id") == "device.list":
        rest.setdefault("only_online", False)
        rest["compatible_only"] = bool(args.get("app_id"))
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
    if ok and spec.get("id") == "device.list" and isinstance(picked, dict):
        from mino_nexus.services.assistant_targets import device_result

        rows = picked.get("items") or []
        if not args.get("app_id") and args.get("only_online", True):
            rows = [row for row in rows if row.get("status") == "online"]
        picked = device_result(rows, targeted=bool(args.get("app_id")))
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
