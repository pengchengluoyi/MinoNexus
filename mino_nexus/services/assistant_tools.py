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
            "permissions": list(spec.get("permissions") or ["account_bound", "resource_visible"]),
            "needs_clarification": bool((spec.get("params_schema") or {}).get("required")),
            "write": spec.get("risk") == "write",
            "render_template": spec.get("card") or "answer",
            "error_source": "business_route",
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
    args: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    field: str = ""


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
        from mino_nexus.services.assistant_targets import channel_candidates
        definitions = [{"id": channel.get("id"), "name": channel.get("alias") or channel.get("label") or channel.get("id"),
                        "alias": channel.get("alias"), "label": channel.get("label"), "app_identifier": channel.get("app_identifier")}
                       for channel in ps.project_env(pid).get("channels") or []]
        explicit = _match_name(definitions, text, "id", "name", "alias", "label", "app_identifier")
        if len(explicit) == 1:
            return str(explicit[0]["id"]), str(explicit[0]["name"]), [], False
        valid = {row["id"] for row in channel_candidates(scope, user_id, require_available=False)["items"]}
        rows = [row for row in definitions if row["id"] in valid]
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
    for key in ("project_id", "app_id", "env_surface", "env_profile"):
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
                return {**out, "_clarification_field": "app_id"}, "这个项目有多个应用，请明确本次应用：" + "、".join(options), options, notes
        raw = out.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, list):
            continue
        ident, label, options, ambiguous = _resolve_one(str(resolver), str(raw), user_id=user_id, scope=out)
        if not ident:
            title = spec.get("title") or spec.get("id")
            if ambiguous:
                return {**out, "_clarification_field": key}, f"{title}：这个名称「{raw}」匹配多个目标，请选择：{'、'.join(options)}", options, notes
            hint = f"，可选：{'、'.join(options)}" if options else ""
            label = {"project_id": "项目", "app_id": "应用", "env_surface": "应用渠道"}.get(key, "目标")
            return {**out, "_clarification_field": key}, f"没有找到{label}「{raw}」{hint}", options, notes
        if ident != str(raw):
            notes.append(f"{key} 用的是 {label}")
        out[key] = ident
    if out.get("app_id") and "env_surface" in ((spec.get("params_schema") or {}).get("properties") or {}) and (spec.get("risk") == "write" or spec.get("id") == "device.list"):
        from mino_nexus.services import project_store as ps

        app = ps.require_app(str(out["app_id"]))
        pid = str(app.get("project_id") or "")
        if out.get("project_id") and str(out["project_id"]) != pid:
            return out, "所选应用不属于本次项目，请重新选择。", [], notes
        from mino_nexus.services.assistant_targets import channel_candidates
        selection = channel_candidates(out, user_id)
        channels = [{"id": row["id"], "label": row["label"]} for row in selection["items"]]
        out["env_profile"] = selection["env_profile"]
        if out.get("env_surface"):
            blocked = next((row for row in selection["unavailable"] if row["id"] == out["env_surface"]), None)
            if blocked:
                return out, f"{blocked['label']}暂不可执行：{blocked['reason']}。", [], notes
        if not out.get("env_surface"):
            options = [f"{c.get('alias') or c.get('label') or c['id']}（{c['id']}）" for c in channels]
            if len(channels) == 1:
                out["env_surface"] = channels[0]["id"]
            elif channels:
                return {**out, "_clarification_field": "env_surface"}, "本次使用哪个应用渠道？" + "、".join(options), options, notes
            else:
                reasons = "\n".join(dict.fromkeys(f"- {row['label']}：{row['reason']}" for row in selection["unavailable"]))
                return out, "当前没有可立即执行的应用渠道。\n" + reasons, [], notes
    raw = out.get("sn")
    resolver = resolvers.get("sn")
    if resolver and raw not in (None, ""):
        if str(raw).strip().lower() in _GENERIC_DEVICE:
            out.pop("sn", None)
        else:
            ident, label, options, ambiguous = _resolve_one(str(resolver), str(raw), user_id=user_id, scope=out)
            if not ident:
                msg = ("设备名称有歧义，请选择：" if ambiguous else "没有找到与本次渠道兼容的设备：") + "、".join(options)
                return {**out, "_clarification_field": "sn"}, msg, options, notes
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
    from mino_nexus.ai.dispatch_log import ctx
    context = ctx()
    if context.get("assistant_origin") and context.get("assistant_generation"):
        from mino_nexus.services.assistant_conversation import require_generation
        require_generation(context["assistant_origin"], context["assistant_generation"])
    group = context.get("assistant_group_policy")
    if isinstance(group, dict):
        if not group.get("enabled") or (spec.get("risk") == "write" and not group.get("allow_execute")):
            return ToolOutcome(ok=False, summary="当前群没有执行权限，请在 Workbench 的群策略中设置或转到私聊。", tool_id=str(spec.get("id") or ""))

    route = str(spec.get("route") or "")
    assert_no_delete(route)
    method, _, path = route.partition(" ")
    method = method.upper()
    props = (spec.get("params_schema") or {}).get("properties") or {}
    args = dict(args or {})
    if spec.get("id") in {"scout.list", "scout.update", "scout.update_status", "scout.update_cancel"}:
        if set(args) - set(props):
            return ToolOutcome(ok=False, status=400, summary="Scout 工具含不支持的参数；更新须指定固定 release_tag/build_id，仅使用节点配置的发布源，不接受任意下载地址。", tool_id=str(spec["id"]))
        node_id = args.get("node_id")
        if node_id is not None and (not isinstance(node_id, str) or not re.fullmatch(r"[a-z0-9]+", node_id)):
            return ToolOutcome(ok=False, status=400, summary="请使用 scout_list 返回的准确 node_id，不是设备 SN 或路径。", tool_id=str(spec["id"]))
    for key in ("project_id", "app_id", "env_surface", "env_profile", "module", "modules", "ordinal"):
        if key in props and target and target.get(key):
            args.setdefault(key, target[key])
    try:
        args, ask, options, notes = resolve_args(spec, args, user_id=user_id)
    except (ValueError, KeyError) as exc:
        return ToolOutcome(ok=False, summary=str(exc) if isinstance(exc, ValueError) else "目标不存在或已经变化，请重新选择。", tool_id=str(spec.get("id") or ""), args=args)
    if isinstance(group, dict):
        pid = args.get("project_id")
        if args.get("app_id"):
            from mino_nexus.services.project_store import require_app
            pid = require_app(args["app_id"]).get("project_id")
        if not pid or pid not in group.get("project_ids", []):
            return ToolOutcome(ok=False, summary="当前群没有这个项目的访问授权，请转到私聊处理。", tool_id=str(spec.get("id") or ""))
    preflight_data: dict[str, Any] = {}
    if spec.get("id") == "case.run" and args.get("app_id") and args.get("_clarification_field") not in ("project_id", "app_id"):
        from mino_nexus.services.assistant_scope import resolve
        args, scope_problem, scope_candidates = resolve(args)
        if scope_problem:
            return ToolOutcome(ok=False, summary=scope_problem, ask=scope_problem if scope_candidates else "",
                tool_id="case.run", args=args, field="module" if scope_candidates else "", candidates=scope_candidates)
        from mino_nexus.services.case_preflight import preflight
        from mino_nexus.services.project_store import require_app

        preflight_data = preflight(require_app(str(args["app_id"])), args["case_ids"])
        if not preflight_data["runnable"]:
            return ToolOutcome(ok=False, status=400, summary="所选用例均无法 UI 自动化，本次未创建任务。",
                tool_id="case.run", args=args, data={"preflight": preflight_data})
    question_field = str(args.pop("_clarification_field", ""))
    if ask:
        if question_field == "env_surface" and spec.get("id") == "case.run":
            from mino_nexus.services.assistant_targets import channel_candidates, compatible_devices, device_brief

            choices = []
            devices = _visible_devices(user_id)
            for channel in channel_candidates(args, user_id)["items"]:
                scoped = {**args, "env_surface": channel["id"]}
                selected_sn = ""
                if args.get("sn") and str(args["sn"]).lower() not in _GENERIC_DEVICE:
                    selected_sn, _, _, _ = _resolve_one("device.by_alias", str(args["sn"]), user_id=user_id, scope=scoped)
                    if not selected_sn:
                        continue
                for device in compatible_devices(scoped, devices):
                    if not device.get("executable") or (selected_sn and str(device.get("sn")) != selected_sn):
                        continue
                    value = {"env_surface": channel["id"], "env_profile": str(args.get("env_profile") or ""), "sn": str(device["sn"])}
                    choices.append({"label": channel["label"] + " · " + device_brief(device)["display_label"],
                                    "value": json.dumps(value, ensure_ascii=False, sort_keys=True)})
            message = "请选择本次应用渠道和设备，回复一个序号即可。" if choices else "当前没有与本次要求匹配的应用渠道和可执行设备。"
            return ToolOutcome(ok=False, ask=message if choices else "", summary=message, tool_id="case.run", args=args,
                field="execution_target" if choices else "", candidates=choices,
                data={"preflight": preflight_data} if preflight_data else None)
        candidates = []
        resolver = (spec.get("param_resolvers") or {}).get(question_field)
        for label in options:
            value = label
            if resolver:
                candidate_id = re.search(r"[（(]([^（）()]+)[）)]$", label)
                value, _, _, _ = _resolve_one(resolver, candidate_id.group(1) if candidate_id else label, user_id=user_id, scope=args)
            if value:
                candidates.append({"label": label, "value": value})
        return ToolOutcome(ok=False, ask=ask if question_field else "", options=options,
            tool_id=str(spec.get("id") or ""), summary=ask, args=args, field=question_field, candidates=candidates,
            data={"preflight": preflight_data} if preflight_data else None)
    for key in ("project_id", "app_id", "env_surface", "env_profile"):
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
            available = [d for d in devices if d.get("executable") and d.get("sn")]
            names = [device_brief(d)["display_label"] for d in available]
            if not names:
                reasons = list(dict.fromkeys(str(d.get("blocked_reason") or "设备未就绪") for d in devices))
                msg = "当前应用渠道没有可执行设备。" + ("；".join(reasons) if reasons else "请连接同系统、兼容且属于当前账号的设备。")
                return ToolOutcome(ok=False, summary=msg, tool_id=str(spec.get("id") or ""))
            if len(available) == 1:
                args["sn"] = str(available[0]["sn"])
            else:
                msg = f"本次用哪台设备？当前应用渠道兼容且可执行的设备：{'、'.join(names)}。"
                return ToolOutcome(ok=False, ask=msg, options=names, tool_id=str(spec.get("id") or ""), summary=msg,
                    args=args, field="sn", candidates=[{"value": str(device["sn"]), "label": device_brief(device)["display_label"]}
                    for device in available], data={"preflight": preflight_data} if preflight_data else None)
    names = _PARAM.findall(path)
    required = list((spec.get("params_schema") or {}).get("required") or [])
    missing = [name for name in dict.fromkeys(names + required) if args.get(name) in (None, "", [])]
    if missing:
        labels = {"app_id": "应用", "project_id": "项目", "sn": "设备", "task_id": "任务", "env_surface": "应用渠道"}
        msg = "请补充本次的" + "、".join(labels.get(key, "所需信息") for key in missing) + "。"
        return ToolOutcome(ok=False, ask=msg, summary=msg, tool_id=str(spec.get("id") or ""), args=args, field=missing[0])
    url = path
    for name in names:
        url = url.replace("{" + name + "}", str(args.get(name)))
    rest = {k: v for k, v in args.items() if k not in names and k not in ("project_id", "module", "modules", "ordinal", "scope", "scope_revision", "all_cases", "intent") and v not in (None, "")}
    if spec.get("id") == "case.list":
        rest["limit"] = min(200, max(1, int(args.get("limit") or 50)))
        if args.get("module"):
            rest["module"] = args["module"]
    if spec.get("id") == "device.list":
        rest.setdefault("only_online", False)
        rest["compatible_only"] = bool(args.get("app_id"))
    token = ensure_assistant_token(user_id)
    started = time.time()
    headers = {"Authorization": f"Bearer {token}", "X-Mino-Client": "assistant"}
    receipt_key, receipt_token = "", ""
    if spec.get("risk") == "write":
        import hashlib
        from mino_nexus.ai.dispatch_log import ctx
        from mino_nexus.services import assistant_receipts as receipts
        scope = str(ctx().get("assistant_mutation_scope") or "")
        if not scope:
            return ToolOutcome(ok=False, summary="写操作缺少稳定请求标识，已拒绝执行", tool_id=str(spec.get("id") or ""))
        digest = hashlib.sha256(json.dumps({"owner": user_id, "scope": scope, "tool": spec.get("id"), "args": args}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        receipt_key = "tool:" + digest
        receipt_token = receipts.acquire(receipt_key, kind="tool", lease_seconds=3600)
        if not receipt_token:
            previous = receipts.get(receipt_key) or {}
            if previous.get("state") == "done":
                return ToolOutcome(ok=True, summary="已处理该写操作，返回原任务标识", data=previous.get("data") or {}, tool_id=str(spec.get("id") or ""))
            return ToolOutcome(ok=False, status=503, summary="该操作正在处理或结果未知，请先查询任务状态；不会重复创建", tool_id=str(spec.get("id") or ""), args=args)
        if context.get("assistant_origin") and context.get("assistant_generation"):
            from mino_nexus.services.assistant_conversation import require_generation
            require_generation(context["assistant_origin"], context["assistant_generation"])
            from mino_nexus.services.assistant_store import require_im_binding
            origin = context["assistant_origin"]
            if origin.get("channel"):
                require_im_binding(user_id, origin["channel"], origin["node_id"], origin)
        receipts.mark_side_effect(str(ctx().get("assistant_inbound_key") or ""), str(ctx().get("assistant_inbound_token") or ""))
        receipts.mark_side_effect(receipt_key, receipt_token)
    try:
        response = _asgi_request(method, url, rest, headers)
    except Exception as exc:
        if receipt_key:
            receipts.finish(receipt_key, receipt_token, "uncertain")
        return ToolOutcome(ok=False, status=503, summary="操作结果暂时无法确认，请先查询任务状态。", tool_id=str(spec.get("id") or ""), args=args)
    elapsed = int((time.time() - started) * 1000)
    try:
        payload = response.json()
    except Exception:
        payload = {"detail": response.text[:500]}
    picked = _pick(payload, [str(x) for x in (spec.get("result_pick") or [])])
    ok = 200 <= response.status_code < 300 and not (isinstance(payload, dict) and (payload.get("ok") is False or payload.get("code", 200) != 200))
    if ok and spec.get("id") == "case.run" and isinstance(payload.get("data"), dict):
        run = payload["data"]
        picked = {key: run[key] for key in ("task_id", "run_id", "requested_total", "total", "skipped_cases") if key in run}
        preflight_data = {"runnable": [str(case.get("case_id") or "") for case in run.get("cases") or []],
                          "blocked": run.get("skipped_cases") or [], "requested_total": run.get("requested_total", run.get("total", 0))}
        picked["preflight"] = preflight_data
    if ok and spec.get("id") == "device.list" and isinstance(picked, dict):
        from mino_nexus.services.assistant_targets import device_result

        rows = picked.get("items") or []
        if not args.get("app_id") and args.get("only_online", True):
            rows = [row for row in rows if row.get("status") == "online"]
        picked = device_result(rows, targeted=bool(args.get("app_id")))
    summary = ""
    if isinstance(payload, dict):
        summary = str(payload.get("msg") or payload.get("message") or "")
        data = payload.get("data")
        if isinstance(data, dict) and data.get("task_id"):
            summary = summary or f"任务 {data.get('task_id')}"
    if notes:
        summary = (summary + "；" if summary else "") + "；".join(notes)
    if not ok and not summary:
        detail = payload.get("detail") if isinstance(payload, dict) else ""
        if isinstance(detail, dict):
            summary = str(detail.get("message") or detail.get("user_message") or "当前操作无法完成")
            if isinstance(picked, dict):
                picked = {**picked, "user_message": summary, "blocking_code": "business_rejected"}
        elif isinstance(detail, list):
            summary = "请求参数不完整或格式不正确，请检查本次应用、范围和设备。"
        else:
            summary = str(detail or "服务没有返回可确认的结果")
    text = json.dumps(picked, ensure_ascii=False, default=str)
    if surface != "mcp" and len(text) > 4000 and spec.get("id") != "case.run":
        picked = {"preview": text[:4000]}
    if receipt_key:
        receipt_data = picked if isinstance(picked, dict) else {}
        if not any(k in receipt_data for k in ("task_id", "run_id", "id")) and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            receipt_data = payload["data"]
        identifiers = {k: receipt_data[k] for k in ("task_id", "run_id", "id", "status") if isinstance(receipt_data.get(k), (str, int))}
        if spec.get("id") in {"workbench.update", "workbench.update_cancel", "workbench.update_recover"}:
            identifiers.update({k: receipt_data[k] for k in (
                "installation_id", "operation_id", "build_id", "state", "observation", "helper_started", "recovery"
            ) if isinstance(receipt_data.get(k), (str, bool))})
        if spec.get("id") == "scout.update":
            identifiers.update({k: receipt_data[k] for k in (
                "node_id", "operation_id", "state", "command", "previous_version", "running_version_verified", "next_tool"
            ) if isinstance(receipt_data.get(k), (str, bool))})
        if isinstance(picked, str) and re.fullmatch(r"[\w.:-]{1,160}", picked):
            identifiers = {"task_id": picked}
        receipts.finish(receipt_key, receipt_token, "done" if ok else "rejected" if response.status_code < 500 else "uncertain", identifiers)
    return ToolOutcome(
        ok=ok,
        status=response.status_code,
        data=picked,
        summary=summary or ("完成" if ok else "失败"),
        elapsed_ms=elapsed,
        tool_id=str(spec.get("id") or ""),
        args=args,
    )
