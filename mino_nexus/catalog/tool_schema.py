# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""能力菜单 → OpenAI function tools。agent-decide 用，其它 job 仍走 JSON。"""
from __future__ import annotations

import json
from typing import Any, Optional

COORD = {
    "type": "integer",
    "minimum": 0,
    "maximum": 1000,
    "description": "0-1000 归一化坐标（千分比，不是像素）",
}

PARAM_DEFAULTS: dict[str, dict[str, Any]] = {
    "tap_element": {
        "type": "object",
        "properties": {
            "x": COORD,
            "y": COORD,
            "selector_text": {
                "type": "string",
                "description": "目标上的可见文字，如 首页 / 我的。执行侧用层级定位，比纯坐标稳",
            },
            "tab_slot_index": {
                "type": "integer",
                "description": "底栏槽位序号（0 起，从左到右）；无文案图标槽可与 anchor_between 联用",
            },
            "anchor_between": {
                "type": "array",
                "items": {"type": "string"},
                "description": "左右锚点文案，定位两 Tab 之间的无文案控件",
            },
        },
        "required": ["x", "y"],
    },
    "long_press_element": {
        "type": "object",
        "properties": {
            "x": COORD,
            "y": COORD,
            "selector_text": {
                "type": "string",
                "description": "目标上的可见文字；执行侧用层级定位，比纯坐标稳",
            },
            "duration_ms": {"type": "integer", "description": "按住毫秒，默认 800"},
        },
        "required": ["x", "y"],
    },
    "multi_tap": {
        "type": "object",
        "properties": {
            "x": COORD,
            "y": COORD,
            "count": {
                "type": "integer",
                "minimum": 2,
                "maximum": 12,
                "description": "连点次数，默认 6。调试面板/版本号彩蛋用这条，不要拆成多次 tap_element",
            },
            "interval_ms": {
                "type": "integer",
                "minimum": 40,
                "maximum": 400,
                "description": "两次点击间隔毫秒，默认 80",
            },
        },
        "required": ["x", "y"],
    },
    "input_text": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要输入的文本；口令可写占位，值由系统填"},
            "x": COORD,
            "y": COORD,
            "field": {
                "type": "string",
                "enum": ["phone", "sms_code", "password", "text"],
                "description": "登录页手机号/口令时必填，值由资源网关填",
            },
        },
        "required": ["text", "x", "y"],
    },
    "swipe_element_to_element": {
        "type": "object",
        "properties": {
            "from_x": COORD,
            "from_y": COORD,
            "to_x": COORD,
            "to_y": COORD,
            "duration_ms": {"type": "integer"},
        },
        "required": ["from_x", "from_y", "to_x", "to_y"],
    },
    "swipe_direction": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down", "left", "right"],
                "description": "未给起止点时按方向在屏内默认区域滑动",
            },
            "from_x": COORD,
            "from_y": COORD,
            "to_x": COORD,
            "to_y": COORD,
            "duration_ms": {"type": "integer"},
        },
        "required": ["direction"],
        "description": "提供 from_x/from_y/to_x/to_y（0–1000 千分比）时沿该线段滑动；否则仅用 direction 走默认手势",
    },
    "press_key": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "enum": ["back", "home", "menu", "enter", "power"],
            },
        },
        "required": ["key"],
    },
    "wait_ms": {
        "type": "object",
        "properties": {
            "ms": {"type": "integer", "description": "等待毫秒"},
            "duration_ms": {"type": "integer", "description": "与 ms 同义"},
        },
    },
    "wait_screen_ready": {"type": "object", "properties": {}},
    "launch_app": {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "必须是本趟目标应用包名"},
        },
        "required": ["package"],
    },
    "close_app": {
        "type": "object",
        "properties": {"package": {"type": "string"}},
        "required": ["package"],
    },
    "kill_app": {
        "type": "object",
        "properties": {"package": {"type": "string"}},
        "required": ["package"],
    },
    "get_app_version": {
        "type": "object",
        "properties": {"package": {"type": "string"}},
    },
    "clear_app_cache": {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "必须是本趟目标应用包名"},
        },
        "required": ["package"],
    },
    "system_pkg_clear": {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "必须是本趟目标应用包名"},
        },
        "required": ["package"],
    },
    "get_foreground_app": {"type": "object", "properties": {}},
    "human_input_text": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "field": {"type": "string", "enum": ["sms_code", "phone", "text"]},
        },
        "required": ["question"],
    },
    "relogin": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "description": "对齐登录态：看当前屏决定登入或退出"},
        },
    },
    "check_run_env": {
        "type": "object",
        "properties": {},
        "description": "确认批次运行环境（env/platform/otp）。前置阶段不要调用，环境由 env_profile 确定",
    },
    "lease_account": {
        "type": "object",
        "properties": {
            "precondition": {
                "type": "string",
                "description": "用例前置原文；缺省使用 CaseScene。用于编译 Requirement DSL 选号",
            },
            "template_id": {
                "type": "string",
                "description": "号池业务模板 id（如 tpl_personal / tpl_ecommerce）；与 CaseScene.account_template_id 一致",
            },
            "account_template_id": {
                "type": "string",
                "description": "同 template_id 别名",
            },
            "requirements": {
                "type": "object",
                "description": "Requirement DSL：all/prefer 约束 facets，缺省由前置+模板编译",
            },
        },
    },
    "get_otp": {
        "type": "object",
        "properties": {},
        "description": "从已租账号或项目环境取验证码，不点设备",
    },
    "accept_legal_consent": {
        "type": "object",
        "properties": {},
        "description": "勾选长文案左侧的小同意框；输入框聚焦时先 BACK",
    },
    "request_sms_code": {
        "type": "object",
        "properties": {},
        "description": "手机号已填 11 位后，点输入框同行右侧短文案发送控件（再 get_otp 取码）",
    },
    "dismiss_ime": {
        "type": "object",
        "properties": {},
        "description": "有聚焦输入框时 BACK 收起输入法；无聚焦则不操作",
    },
    "fsm_navigate": {
        "type": "object",
        "properties": {
            "from_state": {
                "type": "string",
                "description": "当前逻辑页 state_id（page.sk*）或展示名/别名",
            },
            "to_state": {
                "type": "string",
                "description": "目标逻辑页 state_id 或展示名/别名",
            },
            "current_state": {
                "type": "string",
                "description": "同 from_state",
            },
            "expected_state": {
                "type": "string",
                "description": "同 to_state",
            },
        },
        "description": "按 NavFSM 路线图规划最短路并执行第一步点击（目标为逻辑页 target_page，非仅底栏 Tab）",
    },
}

# 这些能力故意不向模型要业务参数（值由 Nexus 从会话/设备注入）。
INTENTIONALLY_EMPTY_PARAM_CAPS = frozenset({
    "wait_screen_ready",
    "get_foreground_app",
    "check_run_env",
    "get_otp",
    "accept_legal_consent",
    "dismiss_ime",
    "request_sms_code",
    "probe_device_state",
    "read_device_data",
    "wake_screen",
    "dismiss_keyguard",
})

SIGNAL_DONE = "signal_done"
SIGNAL_GIVE_UP = "signal_give_up"
SIGNAL_ASK_HUMAN = "signal_ask_human"
SIGNAL_SKIP = "signal_skip"
CONTROL_TOOL_NAMES = frozenset({SIGNAL_DONE, SIGNAL_GIVE_UP, SIGNAL_ASK_HUMAN, SIGNAL_SKIP})

_TOOL_META_PROPS: dict[str, Any] = {
    "thought": {
        "type": "string",
        "description": "一两句：当前屏是什么、为什么调这个工具。不要写长推理。",
    },
    "expected_after": {
        "type": "string",
        "description": "执行后界面大概会变成什么样（给自己看，不是校验结论）",
    },
    "remember": {
        "type": "array",
        "items": {"type": "string"},
        "description": "本步要记住、后面还要用的事实",
    },
    "knowledge_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "还需某条知识原文时填写 id",
    },
}


def _with_tool_meta(parameters: dict[str, Any]) -> dict[str, Any]:
    spec = dict(parameters or {"type": "object", "properties": {}})
    props = dict(spec.get("properties") or {})
    for key, schema in _TOOL_META_PROPS.items():
        if key not in props:
            props[key] = schema
    spec["type"] = spec.get("type") or "object"
    spec["properties"] = props
    return spec


CONTROL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": SIGNAL_DONE,
            "description": "本步操作已经做完，不要再点。不是整案完成，也不是校验通过。",
            "parameters": _with_tool_meta({
                "type": "object",
                "properties": {
                    "thought": {"type": "string"},
                    "expected_after": {"type": "string"},
                    "remember": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "knowledge_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            }),
        },
    },
    {
        "type": "function",
        "function": {
            "name": SIGNAL_GIVE_UP,
            "description": "客观做不到本步（缺账号、缺入口、设备不对）。不要用这个表示校验失败。",
            "parameters": _with_tool_meta({
                "type": "object",
                "properties": {"thought": {"type": "string"}},
                "required": ["thought"],
            }),
        },
    },
    {
        "type": "function",
        "function": {
            "name": SIGNAL_ASK_HUMAN,
            "description": "需要人提供能填进界面的信息。禁止让人去设备上点。已租账号时不要用这个要手机号或验证码。",
            "parameters": _with_tool_meta({
                "type": "object",
                "properties": {
                    "thought": {"type": "string"},
                    "question": {"type": "string"},
                    "field": {"type": "string", "enum": ["sms_code", "phone", "text"]},
                },
                "required": ["question"],
            }),
        },
    },
    {
        "type": "function",
        "function": {
            "name": SIGNAL_SKIP,
            "description": "本条用例在当前设备/渠道客观无法执行（渠道不对、缺专属入口等），跳过并写明原因。",
            "parameters": _with_tool_meta({
                "type": "object",
                "properties": {
                    "thought": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "description": "跳过原因，会展示在任务详情",
                    },
                },
                "required": ["reason"],
            }),
        },
    },
]


def _params_to_schema(rows: Any) -> Optional[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        return None
    props: dict[str, Any] = {}
    required: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        spec: dict[str, Any] = {"type": str(row.get("type") or "string")}
        if row.get("description"):
            spec["description"] = str(row.get("description"))
        if row.get("enum"):
            spec["enum"] = list(row.get("enum") or [])
        if row.get("minimum") is not None:
            spec["minimum"] = row.get("minimum")
        if row.get("maximum") is not None:
            spec["maximum"] = row.get("maximum")
        props[name] = spec
        if row.get("required"):
            required.append(name)
    if not props:
        return None
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def _merge_param_schemas(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    base_props = dict(base.get("properties") or {})
    over_props = dict(overlay.get("properties") or {})
    props = {**base_props, **over_props}
    required = list(dict.fromkeys(
        [str(x) for x in (base.get("required") or []) if str(x)]
        + [str(x) for x in (overlay.get("required") or []) if str(x)]
    ))
    required = [r for r in required if r in props]
    out: dict[str, Any] = {"type": "object", "properties": props}
    desc = overlay.get("description") or base.get("description")
    if desc:
        out["description"] = desc
    if required:
        out["required"] = required
    return out


def params_schema_for(cap_id: str, catalog_params: Any = None) -> dict[str, Any]:
    """目录 params 与 PARAM_DEFAULTS 合并：目录为空时用默认，避免无参工具进模型。"""
    default = dict(PARAM_DEFAULTS.get(str(cap_id or ""), {"type": "object", "properties": {}}))
    from_catalog = _params_to_schema(catalog_params)
    if not from_catalog:
        return default
    if not (default.get("properties") or {}):
        return from_catalog
    return _merge_param_schemas(default, from_catalog)


def schema_to_param_rows(schema: dict[str, Any] | None) -> list[dict[str, Any]]:
    """OpenAI JSON Schema → catalog payload.params 行。"""
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        return []
    required = {str(x) for x in (schema.get("required") or [])}
    rows: list[dict[str, Any]] = []
    for name, spec in props.items():
        key = str(name or "").strip()
        if not key or key in _TOOL_META_PROPS:
            continue
        if not isinstance(spec, dict):
            spec = {"type": "string"}
        row: dict[str, Any] = {
            "name": key,
            "type": str(spec.get("type") or "string"),
            "required": key in required,
        }
        if spec.get("description"):
            row["description"] = str(spec["description"])
        if spec.get("enum"):
            row["enum"] = list(spec.get("enum") or [])
        if spec.get("minimum") is not None:
            row["minimum"] = spec["minimum"]
        if spec.get("maximum") is not None:
            row["maximum"] = spec["maximum"]
        rows.append(row)
    return rows


def cap_specific_param_names(cap_id: str, catalog_params: Any = None) -> list[str]:
    schema = params_schema_for(cap_id, catalog_params)
    return [
        str(k) for k in (schema.get("properties") or {})
        if str(k) not in _TOOL_META_PROPS
    ]


# Scout low_level 模板 `{package}` 从 EXECUTE.params 取值，不读 device_hint。
PACKAGE_PARAM_CAPS = frozenset({
    "launch_app",
    "close_app",
    "kill_app",
    "get_app_version",
    "clear_app_cache",
    "system_pkg_clear",
})


def _low_level_needs_package(low_level: Any) -> bool:
    if isinstance(low_level, dict):
        blob = json.dumps(low_level, ensure_ascii=False)
    else:
        blob = str(low_level or "")
    return "{package}" in blob


def fill_input_text_from_ctx(
    params: dict[str, Any] | None,
    *,
    cap_id: str,
    ctx: Any = None,
) -> dict[str, Any]:
    """LLM 只填 field 时，由租号/OTP 网关补 params.text（Scout adb 必填）。"""
    out = dict(params or {})
    if str(cap_id or "").strip() != "input_text":
        return out
    if str(out.get("text") or "").strip():
        return out
    acc = dict(getattr(ctx, "picked_account", None) or {}) if ctx is not None else {}
    field = str(out.get("field") or "text").strip().lower()
    if field == "phone":
        import re

        from mino_nexus.services.project_env import account_ident

        raw = str(acc.get("phone") or account_ident(acc) or "").strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 11:
            out["text"] = digits[-11:]
        elif raw:
            out["text"] = raw
        return out
    if field in ("sms_code", "验证码", "password"):
        code = str(acc.get("otp") or acc.get("sms_code") or acc.get("password") or "").strip()
        if not code and field in ("sms_code", "验证码") and ctx is not None:
            from mino_nexus.loop.local_executors import _resolve_otp

            code, _ = _resolve_otp(ctx)
        if code:
            out["text"] = code
    return out


def fill_target_package(
    params: dict[str, Any] | None,
    *,
    cap_id: str,
    target_package: str,
    low_level: Any = None,
) -> dict[str, Any]:
    """派单前补本趟包名。模型漏填时仍能 pm clear / force-stop。"""
    out = dict(params or {})
    pkg = str(target_package or "").strip()
    if not pkg:
        return out
    if str(out.get("package") or "").strip():
        return out
    cid = str(cap_id or "").strip()
    if cid in PACKAGE_PARAM_CAPS or _low_level_needs_package(low_level):
        out["package"] = pkg
    return out


def openai_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": str(name or "").strip(),
            "description": (description or name or "")[:240],
            "parameters": _with_tool_meta(parameters or {"type": "object", "properties": {}}),
        },
    }


def tools_for_menu(menu: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """当前 Run 可用能力 + 三个控制信号。assert / 资源网关能力不进 tools。"""
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in menu or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        if not cid or cid in seen or cid in CONTROL_TOOL_NAMES:
            continue
        if cid in {"release_account", "pick_account", "get_phone"}:
            continue
        seen.add(cid)
        catalog_params = None
        try:
            from mino_nexus.catalog import registry as plugin_registry

            cap = plugin_registry.get_capability(cid)
            catalog_params = getattr(cap, "params", None) if cap is not None else None
        except Exception:
            catalog_params = None
        tools.append(openai_tool(
            cid,
            str(row.get("summary") or cid),
            params_schema_for(cid, catalog_params),
        ))
    tools.extend(CONTROL_TOOLS)
    return tools


def tools_chat_payload(tools: list[dict[str, Any]], *, required: bool = True) -> dict[str, Any]:
    if not tools:
        return {}
    return {
        "tools": tools,
        "tool_choice": "required" if required else "auto",
        "parallel_tool_calls": False,
    }


_VISUAL_ENVELOPE_KEYS = ("screen_layout", "vlm_hierarchy")


def _pop_visual_envelope(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _VISUAL_ENVELOPE_KEYS:
        if key not in args:
            continue
        val = args.pop(key)
        if isinstance(val, dict) and val:
            out[key] = val
    return out


def merge_decision_visual_fields(
    decision: dict[str, Any],
    *,
    content: str = "",
) -> dict[str, Any]:
    """tool call 决策与正文 JSON 中的 screen_layout / vlm_hierarchy 合并（正文补全 tool 缺项）。"""
    if not isinstance(decision, dict):
        return decision
    extra: Optional[dict[str, Any]] = None
    text = str(content or "").strip()
    if text:
        try:
            from mino_nexus.ai.llm_client import _extract_first_json_object

            extra = _extract_first_json_object(text)
        except Exception:
            extra = None
    if isinstance(extra, dict):
        for key in _VISUAL_ENVELOPE_KEYS:
            val = extra.get(key)
            if not isinstance(val, dict) or not val:
                continue
            if not decision.get(key):
                decision[key] = val
    return decision


def _args_of(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_tool_call(tool_calls: Any) -> Optional[dict[str, Any]]:
    rows = tool_calls if isinstance(tool_calls, list) else []
    for row in rows:
        if isinstance(row, dict) and str((row.get("function") or {}).get("name") or "").strip():
            return row
    return None


def decision_from_tool_calls(
    tool_calls: Any,
    *,
    content: str = "",
) -> Optional[dict[str, Any]]:
    """把 OpenAI tool_calls 收成 decide JSON。只取第一个 call。"""
    call = _first_tool_call(tool_calls)
    if call is None:
        return None
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(fn.get("name") or "").strip()
    args = _args_of(call)
    envelope = _pop_visual_envelope(args)
    thought = str(args.pop("thought", "") or content or "").strip()
    remember = args.pop("remember", None)
    knowledge_ids = args.pop("knowledge_ids", None)
    out: dict[str, Any] = {
        "thought": thought,
        "status": "continue",
        "action": None,
        "expected_after": str(args.pop("expected_after", "") or ""),
        "remember": remember if isinstance(remember, list) else [],
        "knowledge_ids": knowledge_ids if isinstance(knowledge_ids, list) else [],
        "_tool_name": name,
    }
    out.update(envelope)
    if name == SIGNAL_DONE:
        out["status"] = "done"
        return out
    if name == SIGNAL_GIVE_UP:
        out["status"] = "give_up"
        return out
    if name == SIGNAL_ASK_HUMAN:
        out["status"] = "ask_human"
        out["action"] = {
            "capability_id": "human_input_text",
            "params": {
                "question": str(args.get("question") or thought or "需要你提供信息"),
                "field": str(args.get("field") or "text"),
            },
        }
        return out
    if name == SIGNAL_SKIP:
        reason = str(args.get("reason") or thought or "当前渠道无法执行本条用例").strip()
        out["status"] = "skip"
        out["thought"] = reason
        return out
    out["action"] = {"capability_id": name, "params": args}
    return out


def merge_tool_call_deltas(acc: list[dict[str, Any]], deltas: Any) -> None:
    """把流式 delta.tool_calls 拼进 acc（按 index）。"""
    rows = deltas if isinstance(deltas, list) else []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        try:
            idx = int(raw.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx < 0:
            idx = 0
        while len(acc) <= idx:
            acc.append({
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
        row = acc[idx]
        if raw.get("id"):
            row["id"] = str(raw.get("id") or "")
        if raw.get("type"):
            row["type"] = str(raw.get("type") or "function")
        fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        dest = row.setdefault("function", {"name": "", "arguments": ""})
        if fn.get("name"):
            dest["name"] = str(fn.get("name") or "")
        if fn.get("arguments"):
            dest["arguments"] = str(dest.get("arguments") or "") + str(fn.get("arguments") or "")
