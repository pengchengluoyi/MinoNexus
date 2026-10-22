# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""能力菜单 → OpenAI function tools。agent-decide 用，其它 job 仍走 JSON。"""
from __future__ import annotations

import json
from copy import deepcopy
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
            "field": {
                "type": "string",
                "enum": ["email", "phone", "sms_code", "password", "text"],
                "description": "邮箱登录用 email；手机号登录用 phone；验证码 sms_code。值由租号网关填，勿填 display_name",
            },
            "mode": {
                "type": "string",
                "enum": ["replace", "append"],
                "description": "replace 替换当前焦点内容，append 在光标处追加。不要给 x,y",
            },
        },
        "required": ["text"],
        "description": "写入当前已聚焦的输入框，不要给坐标，也不要再点一次",
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
    "launch_app": {"type": "object", "properties": {}},
    "close_app": {"type": "object", "properties": {}},
    "kill_app": {"type": "object", "properties": {}},
    "get_app_version": {"type": "object", "properties": {}},
    "clear_app_cache": {"type": "object", "properties": {}},
    "system_pkg_clear": {"type": "object", "properties": {}},
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
CONFIRM_ACTION_RESULT = "confirm_action_result"
REPORT_SCREEN_OBSTACLE = "report_screen_obstacle"
CONTROL_TOOL_NAMES = frozenset({SIGNAL_DONE, SIGNAL_GIVE_UP, SIGNAL_ASK_HUMAN, SIGNAL_SKIP, CONFIRM_ACTION_RESULT, REPORT_SCREEN_OBSTACLE})

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
    "step_outcome": {
        "type": "string",
        "enum": ["", "pass", "give_up", "ask_human", "skip"],
        "description": "本用例步+phase 收工意图（check 全部里程碑 pass 后再 signal_done）",
    },
}


_TOOL_META_SLIM: dict[str, Any] = {
    "thought": _TOOL_META_PROPS["thought"],
    "prior_status": {
        "type": "string",
        "enum": ["pass", "fail"],
        "description": (
            "仅当 success_criteria.prior_action 存在、且画面已经稳定时填写，只能是 pass 或 fail。"
            "画面还在加载时不要填，改为调用 wait_screen_ready。"
            "pass：检查单上的位置、元素和预期都对上了。本次工具做下一步，不要重复上一步。"
            "fail：没对上。本次工具用来重做 prior_action，不要做下一步。"
        ),
    },
}


def _with_tool_meta(parameters: dict[str, Any], *, slim: bool = False) -> dict[str, Any]:
    spec = dict(parameters or {"type": "object", "properties": {}})
    props = dict(spec.get("properties") or {})
    meta = _TOOL_META_SLIM if slim else _TOOL_META_PROPS
    for key, schema in meta.items():
        if key not in props:
            props[key] = deepcopy(schema)
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


def _params_to_schema(rows: Any) -> dict[str, Any]:
    if not isinstance(rows,list): raise ValueError("catalog_params_required")
    props, required = {}, []
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get("name"),str) or not row["name"] or row["name"] in props:
            raise ValueError("catalog_param_identity_invalid")
        name=row["name"]
        if row.get("type") not in {"string","integer","number","boolean","array","object","null"}:
            raise ValueError("catalog_param_type_invalid")
        props[name]={k:v for k,v in row.items() if k not in {"name","required"}}
        if row.get("required"): required.append(name)
    return {"type":"object","properties":props,**({"required":required} if required else {})}


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
    """Schema is projected only from the catalog; defaults are registration inputs."""
    return _strip_target_app_params(str(cap_id or ""), _params_to_schema(catalog_params))


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
        row: dict[str, Any] = {**spec, "name":key, "required":key in required}
        rows.append(row)
    return rows


def cap_specific_param_names(cap_id: str, catalog_params: Any = None) -> list[str]:
    schema = params_schema_for(cap_id, catalog_params)
    return [
        str(k) for k in (schema.get("properties") or {})
        if str(k) not in _TOOL_META_PROPS
    ]


# Scout low_level 模板 `{package}` 从 EXECUTE.params 取值，不读 device_hint。
_CONTEXT_ADDRESS_CAPS = frozenset({
    "launch_app",
    "close_app",
    "kill_app",
    "get_app_version",
    "clear_app_cache",
    "system_pkg_clear",
    "open_url",
    "open_app",
})


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


def _strip_target_app_params(cap_id: str, schema: dict[str, Any]) -> dict[str, Any]:
    """被测应用的打开、关闭、清缓存不把包名交给模型。"""
    if str(cap_id or "") not in _CONTEXT_ADDRESS_CAPS:
        return schema
    out = dict(schema or {})
    props = dict(out.get("properties") or {})
    props.pop("package", None)
    props.pop("url", None)
    out["properties"] = props
    required = [x for x in (out.get("required") or []) if str(x) not in ("package", "url")]
    if required:
        out["required"] = required
    else:
        out.pop("required", None)
    return out


def fill_target_package(
    params: dict[str, Any] | None,
    *,
    cap_id: str,
    target_package: str,
    low_level: Any = None,
) -> dict[str, Any]:
    """被测应用的包名和地址只取本趟上下文，模型多填的不采用。"""
    out = dict(params or {})
    pkg = str(target_package or "").strip()
    cid = str(cap_id or "").strip()
    if not pkg:
        return out
    address = cid in _CONTEXT_ADDRESS_CAPS or _low_level_needs_package(low_level)
    if not address:
        return out
    out.pop("package", None)
    out["package"] = pkg
    if cid in ("launch_app", "open_url", "open_app") and (
        pkg.startswith("http://") or pkg.startswith("https://")
    ):
        out.pop("url", None)
        out["url"] = pkg
    return out


def openai_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    *,
    slim: bool = False,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": str(name or "").strip(),
            "description": (description or name or "")[:240],
            "parameters": _with_tool_meta(
                parameters or {"type": "object", "properties": {}},
                slim=slim,
            ),
        },
    }


def tools_for_menu(
    menu: list[dict[str, Any]],
    *,
    slim_envelope: bool = False,
    include_control_tools: bool = True,
    require_prior_status: bool = False,
    include_review_confirmation: bool = False,
    review_transition: dict[str, Any] | None = None,
    review_only: bool = False,
    execution_scheme: str = "visual",
) -> list[dict[str, Any]]:
    """当前 Run 可用能力；可选控制信号。assert / 资源网关能力不进 tools。"""
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in menu or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        from mino_nexus.catalog.skill_channel import V5_EXCLUDED_IDS
        from mino_nexus.contract.business_event_contract import RETIRED
        if not cid or cid.startswith("guard.") or cid in seen or cid in CONTROL_TOOL_NAMES or cid in V5_EXCLUDED_IDS or cid in RETIRED or cid.startswith("plugin."):
            continue
        if review_only and cid not in {"wait_ms", "wait_screen_ready"}:
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
        tool = openai_tool(
            cid,
            str(row.get("summary") or cid),
            deepcopy(row["parameters"]) if isinstance(row.get("parameters"), dict) else params_schema_for(cid, catalog_params),
            slim=slim_envelope,
        )
        if execution_scheme == "dom" and cid in {"tap_element", "long_press_element", "multi_tap"}:
            parameters = tool["function"]["parameters"]
            parameters["required"] = [key for key in parameters.get("required", []) if key not in {"x", "y"}]
        if execution_scheme == "dom" and cid == "input_text":
            parameters = tool["function"]["parameters"]
            parameters["description"] = "按本轮唯一可编辑 DOM 节点输入；text 是输入值，selector_text/node_ref 定位输入框。无法定位时交给截图聚焦兜底。"
            props = parameters.setdefault("properties", {})
            props["selector_text"] = {"type": "string", "description": "本轮输入框的真实标签，不是要填入的值"}
            props["target"] = {"type": "object", "properties": {
                "text": {"type": "string"}, "content_desc": {"type": "string"}, "resource_id": {"type": "string"},
            }, "description": "本轮输入框结构锚点"}
        if require_prior_status and cid in {"wait_screen_ready", "wait_ms"}:
            tool["function"]["description"] += " 仅当前画面仍在加载或缺少 prior_action.expected 的具体证据时等待；若结果已可见，选择结果确认或 if_pass，不能以旧 in_progress 状态继续等待。"
        if cid == "tap_element" or (execution_scheme == "dom" and cid == "input_text"):
            props = tool["function"]["parameters"].setdefault("properties", {})
            props["node_ref"] = {"type": "string", "description": "本轮结构观察的真实节点引用；不得编造"}
            props["snapshot_id"] = {"type": "string", "description": "与 node_ref 同一轮观察的 snapshot_id"}
        if slim_envelope:
            parameters = tool["function"]["parameters"]
            if require_prior_status and cid not in {"wait_screen_ready", "wait_ms"}:
                parameters["required"] = list(dict.fromkeys([
                    *(parameters.get("required") or []), "prior_status",
                ]))
            else:
                parameters["properties"].pop("prior_status", None)
        if slim_envelope and require_prior_status and isinstance(review_transition, dict) and cid not in {"wait_screen_ready", "wait_ms"}:
            # Publish the same branch contract the result consumer enforces.
            # A device redo cannot simultaneously confirm a terminal result.
            statuses = []
            following = review_transition.get("if_pass") or {}
            failed = review_transition.get("if_fail") or {}
            if not following.get("program_owned") and cid in following.get("capabilities", []):
                statuses.append("pass")
            if cid in failed.get("capabilities", []):
                statuses.append("fail")
            if not statuses:
                continue
            tool["function"]["parameters"]["properties"]["prior_status"]["enum"] = statuses
            if statuses == ["fail"]:
                tool["function"]["description"] += " 待回看时此工具只用于 prior_status=fail 的原目标重做；画面已满足 prior_action.expected 必须调用 confirm_action_result，不再操作设备。"
        tools.append(tool)
    if include_control_tools and not slim_envelope:
        tools.extend(tool for tool in CONTROL_TOOLS if tool["function"]["name"] != SIGNAL_ASK_HUMAN)
    if include_review_confirmation and require_prior_status:
        tools.append(openai_tool(
            CONFIRM_ACTION_RESULT,
            "确认已执行动作的可见结果，不操作设备。当前图已满足 prior_action.expected，且 review_transition.if_pass 为空或 program_owned=true 时，调用本工具并说明画面证据；只有 if_pass 明确给出下一设备目标时，才可直接调用那个目标并填 prior_status=pass。恢复弹窗只确认恢复结果，不确认原业务完成。",
            {"type": "object", "properties": {
                "prior_status": {"type": "string", "enum": ["pass", "fail"] if review_only else ["pass"]},
            }, "required": ["thought", "prior_status"], "additionalProperties": False},
            slim=True,
        ))
    if review_only and include_review_confirmation and require_prior_status:
        tools[-1]["function"]["description"] = "只核验 prior_action.expected 的可见结果，必须给 pass 或 fail 和画面证据；不操作设备。证据不足用等待或挡屏报告，不猜测。"
    # 本地控制工具不依赖设备能力目录，也不要求回看结论。挡屏不能证明业务动作成败。
    tools.append(openai_tool(
        REPORT_SCREEN_OBSTACLE,
        "报告当前目标被弹窗或加载层遮挡，不操作设备、不完成业务里程碑；程序安排独立恢复后继续原目标。",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["consent", "permission", "overlay", "loading"]},
            "summary": {"type": "string", "description": "可见挡屏及被阻挡的当前目标，不得包含账号或验证码"},
            "dialog_region": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                              "minItems": 4, "maxItems": 4, "description": "当前截图对话框区域 [左,上,右,下]，坐标归一到0至1；不能用整页代替弹窗"},
            "dialog_evidence": {"type": "string", "description": "当前区域中的对话框结构及遮挡证据，不填账号或验证码"},
        }, "required": ["kind", "summary", "thought"], "additionalProperties": False},
        slim=True,
    ))
    tools[-1]["function"]["parameters"]["properties"].pop("prior_status", None)
    return tools


def tools_chat_payload(tools: list[dict[str, Any]], *, required: bool = True) -> dict[str, Any]:
    if not tools:
        return {}
    return {
        "tools": tools,
        "tool_choice": "required" if required else "auto",
        "parallel_tool_calls": False,
    }


_DECISION_ENVELOPE_KEYS = (
    "screen_layout",
    "vlm_hierarchy",
    "milestones",
    "step_outcome",
)
from mino_nexus.contract.tool_calls import _VISUAL_ENVELOPE_KEYS  # noqa: F401,E402
