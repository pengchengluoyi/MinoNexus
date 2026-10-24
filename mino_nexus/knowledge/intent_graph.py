"""意图图：业务意图节点 + 策略边 + 可执行块/事件。

规划口径（docs/架构/V8/覆盖度与架构优化-20261024.md §3）：

- **意图节点**是目标态（如 ``auth.login``），带域和状态；
- **策略边**读项目配置的候选通道，失败换下一条，全部失败才判意图失败；
- **事件展开**把选中的边展开为事件序列，走同一个事件解释器。

本模块只做图与映射（纯数据），不调模型、不碰设备。判断由
``mino_nexus.ai.intent_router``（Jev/Laya 的替代：现有 LLM/VLM）完成。
"""
from __future__ import annotations

from typing import Any, Iterable

SCHEMA = "mino.intent_graph.v1"

# 公共意图骨架：global 块是骨架，不是完整模型；App 私有块按 app_id 追加。
DOMAINS: tuple[dict[str, Any], ...] = (
    {
        "id": "auth",
        "label": "认证/会话",
        "intents": (
            {
                "id": "auth.login",
                "label": "登录",
                "block_id": "fb.global.login",
                "reverse": "auth.logout",
                "triggers": ("prep", "do", "recover"),
                "channels": ("phone", "email", "third_party"),
                # 登录顺序不再散落在守卫里：意图边携带这些守卫作为兜底元数据，
                # 主路径由 fb.global.login 块按 edge 顺序展开（规划 §6）。
                "guards": ("block_repeat_get_otp_when_ready", "require_sms_send_before_otp",
                           "block_repeat_email_tab", "require_otp_before_login_tap"),
            },
            {
                "id": "auth.logout",
                "label": "退出登录",
                "block_id": "fb.global.logout",
                "reverse": "auth.login",
                "triggers": ("prep",),
            },
            {
                "id": "auth.consent",
                "label": "隐私/协议同意",
                "block_id": "fb.global.consent",
                "param_block": True,
                "triggers": ("prep", "do"),
            },
            {
                "id": "auth.system_dialog",
                "label": "系统弹窗处理",
                "block_id": "fb.global.consent",
                "param_block": True,
                "triggers": ("prep", "do", "recover"),
            },
            {
                "id": "auth.session.check",
                "label": "确认登录态",
                "caps": ("confirm_login_state", "read_web_auth"),
                "triggers": ("prep", "check"),
            },
        ),
    },
    {
        "id": "commerce",
        "label": "电商",
        "intents": (
            {"id": "order.place", "label": "下单", "triggers": ("do",), "keywords": ("下单", "购买", "提交订单")},
            {"id": "order.cancel", "label": "退单/取消", "triggers": ("do",), "keywords": ("退单", "取消订单")},
            {"id": "order.address", "label": "修改地址", "triggers": ("do",), "keywords": ("地址", "收货")},
            {"id": "order.pay", "label": "支付确认", "triggers": ("do",), "keywords": ("支付", "付款", "结算")},
        ),
    },
    {
        "id": "social",
        "label": "社交",
        "intents": (
            {"id": "message.send", "label": "发消息", "triggers": ("do",), "keywords": ("发送消息", "发消息")},
            {"id": "message.receive", "label": "收消息", "triggers": ("do", "check"), "keywords": ("收到消息", "接收")},
            {"id": "group.manage", "label": "群组操作", "triggers": ("do",), "keywords": ("群组", "建群", "退群")},
        ),
    },
    {
        "id": "community",
        "label": "社区",
        "intents": (
            {"id": "post.create", "label": "发帖", "triggers": ("do",), "keywords": ("发帖", "发布")},
            {"id": "post.like", "label": "点赞", "triggers": ("do",), "keywords": ("点赞", "喜欢")},
            {"id": "post.vote", "label": "投票", "triggers": ("do",), "keywords": ("投票",)},
        ),
    },
    {
        "id": "banking",
        "label": "银行",
        "intents": (
            {"id": "transfer", "label": "转账", "triggers": ("do",), "keywords": ("转账", "汇款")},
            {"id": "bill.query", "label": "账单查询", "triggers": ("do", "check"), "keywords": ("账单", "流水", "余额")},
            {"id": "risk.confirm", "label": "风控确认", "triggers": ("do",), "keywords": ("风控", "安全验证")},
        ),
    },
    {
        "id": "general",
        "label": "通用",
        "intents": (
            {"id": "general.observe", "label": "观察界面", "caps": ("hierarchy", "screenshot"), "triggers": ("prep", "do", "check")},
            {"id": "general.navigate", "label": "页面导航", "caps": ("tap_element",), "triggers": ("prep", "do")},
            {"id": "general.step", "label": "通用步骤", "triggers": ("prep", "do", "check")},
        ),
    },
)

_GLOBAL_INTENTS: dict[str, dict[str, Any]] = {}
_BY_BLOCK: dict[str, str] = {}
for _domain in DOMAINS:
    for _intent in _domain["intents"]:
        row = dict(_intent)
        row["domain"] = _domain["id"]
        row["domain_label"] = _domain["label"]
        row.setdefault("private", False)
        _GLOBAL_INTENTS[row["id"]] = row
        bid = str(row.get("block_id") or "")
        if bid:
            _BY_BLOCK[bid] = row["id"]

# 兼容旧块 id / 别名：旧覆盖叙事里的 login 变体归到同一意图。
_BLOCK_ALIASES: dict[str, str] = {
    "fb.global.login.email_web": "auth.login",
    "login_email": "auth.login",
    "email_web_login": "auth.login",
    "fb.global.privacy.accept": "auth.consent",
    "fb.global.privacy.decline": "auth.consent",
    "fb.global.system_dialog": "auth.system_dialog",
    "fb.global.system_dialog.deny": "auth.system_dialog",
}


def domains() -> list[dict[str, Any]]:
    return [{"id": d["id"], "label": d["label"],
             "intent_count": len(d["intents"])} for d in DOMAINS]


def global_intents() -> list[dict[str, Any]]:
    return [dict(row) for row in _GLOBAL_INTENTS.values()]


def _private_intents(app_id: str, project_id: str = "") -> list[dict[str, Any]]:
    app = str(app_id or "").strip()
    if not app:
        return []
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import list_catalog

    metrics: dict[str, dict[str, Any]] = {}
    if str(project_id or "").strip():
        from mino_nexus.casework.private_blocks import reverse_metrics

        metrics = {str(row.get("block_id") or ""): row
                   for row in (reverse_metrics(str(project_id), app).get("blocks") or [])}
    out: list[dict[str, Any]] = []
    for row in list_catalog(app_id=app):
        if str(row.get("app_id") or "") != app:
            continue
        bid = str(row.get("block_id") or "")
        if not bid:
            continue
        iid = "app." + app + "." + bid.replace("fb.", "").replace(".", "_")
        out.append({
            "id": iid,
            "label": str(row.get("display_name") or bid),
            "domain": "app_private",
            "domain_label": "App 私有",
            "block_id": bid,
            "private": True,
            "origin": str(row.get("block_origin") or ""),
            "description": str(row.get("description") or ""),
            "triggers": ("prep", "do", "recover"),
            # 块→用例反查：私有块不是孤儿，能直接看到由哪些用例沉淀、被引用多少次。
            "case_ids": list((metrics.get(bid) or {}).get("case_ids") or []),
            "case_count": len((metrics.get(bid) or {}).get("case_ids") or []),
            "executions": int((metrics.get(bid) or {}).get("executions") or 0),
            "branch_points": int((metrics.get(bid) or {}).get("branch_points") or 0),
        })
    return out


def intents(*, app_id: str = "", project_id: str = "") -> list[dict[str, Any]]:
    rows = global_intents()
    rows += _private_intents(app_id, project_id)
    return rows


def find_intent(intent_id: str, *, app_id: str = "", project_id: str = "") -> dict[str, Any] | None:
    iid = str(intent_id or "").strip()
    if not iid:
        return None
    if iid in _GLOBAL_INTENTS:
        return dict(_GLOBAL_INTENTS[iid])
    for row in _private_intents(app_id):
        if row["id"] == iid:
            return row
    return None


def intent_for_block(block_id: str, *, app_id: str = "", project_id: str = "") -> dict[str, Any] | None:
    """块 → 意图。旧块 id 通过别名归一，不要求调用方记住历史。"""
    bid = str(block_id or "").strip()
    if not bid:
        return None
    if bid in _BLOCK_ALIASES:
        return find_intent(_BLOCK_ALIASES[bid], app_id=app_id)
    if bid in _BY_BLOCK:
        return find_intent(_BY_BLOCK[bid], app_id=app_id)
    # 私有块：任何非 global 命中都算 App 私有意图。
    for row in _private_intents(app_id, project_id):
        if row.get("block_id") == bid:
            return row
    return None


def intent_options(*, domain: str = "", app_id: str = "", project_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """给模型的选择项。单次 ≤20，大类先两级分流（先域再意图）。"""
    dom = str(domain or "").strip()
    rows = [row for row in intents(app_id=app_id, project_id=project_id) if not dom or row.get("domain") == dom]
    out = [
        {
            "id": row["id"],
            "label": row.get("label") or row["id"],
            "domain": row.get("domain") or "",
            "block_id": row.get("block_id") or "",
            "description": str(row.get("description") or "")[:160],
        }
        for row in rows
    ]
    return out[: max(1, min(int(limit or 20), 20))]


def edges_for(intent_id: str, *, app_id: str = "") -> list[dict[str, Any]]:
    """策略边：happy / fallback / reverse。渠道顺序由节点声明，失败换下一条。"""
    node = find_intent(intent_id, app_id=app_id)
    if not node:
        return []
    edges: list[dict[str, Any]] = []
    if node.get("block_id"):
        edge = {"id": node["id"] + ".happy", "kind": "happy", "to": node["id"],
                "block_id": node["block_id"], "condition": ""}
        if node.get("guards"):
            edge["guards"] = list(node["guards"])
        edges.append(edge)
    for cap in node.get("caps") or ():
        edges.append({"id": node["id"] + ".cap:" + str(cap), "kind": "happy", "to": node["id"],
                      "capability_id": str(cap), "condition": ""})
    for variant, block_id in (node.get("variants") or {}).items():
        edges.append({"id": node["id"] + ".variant:" + str(variant), "kind": "fallback", "to": node["id"],
                      "block_id": str(block_id), "condition": "variant=" + str(variant)})
    for channel in node.get("channels") or ():
        edges.append({"id": node["id"] + ".channel:" + str(channel), "kind": "fallback", "to": node["id"],
                      "channel": str(channel), "condition": "channel=" + str(channel)})
    if node.get("reverse"):
        edges.append({"id": node["id"] + ".reverse", "kind": "reverse", "to": node["reverse"],
                      "condition": "intent=reverse"})
    return edges


def guards_for_intent(intent_id: str, *, app_id: str = "") -> tuple[str, ...]:
    """意图边上的守卫是执行期的唯一声明源。

    守卫挂在 ``auth.login.happy`` 这类边上，而不是散落在 loop 的 if/elif 里。
    执行器只消费这里返回的顺序，新增/删除守卫不需要再改执行分支。
    """
    out: list[str] = []
    for edge in edges_for(intent_id, app_id=app_id):
        for name in edge.get("guards") or ():
            value = str(name or "").strip()
            if value and value not in out:
                out.append(value)
    return tuple(out)


def guards_for_block(block_id: str, *, app_id: str = "", project_id: str = "") -> tuple[str, ...]:
    """块 → 意图 → 边守卫；块没有对应意图时返回空，不猜默认值。"""
    node = intent_for_block(block_id, app_id=app_id, project_id=project_id)
    if not node:
        return ()
    return guards_for_intent(str(node.get("id") or ""), app_id=app_id)


def intent_for_text(text: str, *, app_id: str = "") -> dict[str, Any] | None:
    """文本 → 业务意图：先按目录 keywords 做最长匹配，不调模型。

    这一步让业务意图闭合率有真实分母；LLM/VLM 只在文本匹配不到时兜底。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for node in intents(app_id=app_id):
        for kw in node.get("keywords") or ():
            word = str(kw or "").strip()
            if word and word in raw and (best is None or len(word) > best[0]):
                best = (len(word), node)
    return dict(best[1]) if best else None


def annotate_step(step: dict[str, Any], *, app_id: str = "", index: int = 0) -> dict[str, Any]:
    """给编译产物的一步补意图标注；只补字段，不改事件。"""
    out = dict(step or {})
    if out.get("intent_id"):
        return out
    node = None
    bid = str(out.get("block_id") or "").strip()
    if bid:
        node = intent_for_block(bid, app_id=app_id)
    if node is None:
        text = " ".join(str(out.get(k) or "") for k in
                        ("title", "source_line", "source_clause", "instruction", "action", "target_desc"))
        node = intent_for_text(text, app_id=app_id)
    if node is None:
        cap = str(out.get("hook_cap") or out.get("device_cap") or out.get("cap") or "").strip()
        if cap in ("confirm_login_state", "read_web_auth"):
            node = find_intent("auth.session.check", app_id=app_id)
        elif cap in ("hierarchy", "screenshot", "probe_device_state", "get_foreground_app"):
            node = find_intent("general.observe", app_id=app_id)
        elif cap in ("tap_element", "press_key", "swipe_direction", "swipe_element_to_element"):
            node = find_intent("general.navigate", app_id=app_id)
    if node is None:
        node = find_intent("general.step", app_id=app_id)
    out["intent_id"] = node["id"] if node else "general.step"
    out["intent_domain"] = (node or {}).get("domain") or "general"
    out["intent_label"] = (node or {}).get("label") or "通用步骤"
    out["intent_edge"] = "happy"
    return out


def build_case_graph(steps: Iterable[dict[str, Any]], *, app_id: str = "",
                     case_id: str = "", case_step: int = 0) -> dict[str, Any]:
    """按用例步骤生成意图图切片：节点、边、入口与覆盖目标。"""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    annotated: list[dict[str, Any]] = []
    for idx, raw in enumerate(steps or [], 1):
        if not isinstance(raw, dict):
            continue
        step = annotate_step(raw, app_id=app_id, index=idx)
        annotated.append(step)
        iid = str(step.get("intent_id") or "general.step")
        node = find_intent(iid, app_id=app_id) or {"id": iid, "label": iid, "domain": "general"}
        nodes.setdefault(iid, {
            "id": iid,
            "label": node.get("label") or iid,
            "domain": node.get("domain") or "general",
            "block_id": node.get("block_id") or "",
            "private": bool(node.get("private")),
            "step_refs": [],
        })
        nodes[iid]["step_refs"].append(str(step.get("id") or f"step_{idx}"))
        for edge in edges_for(iid, app_id=app_id):
            if edge["id"] not in {e["id"] for e in edges}:
                edges.append(edge)
    return {
        "schema": SCHEMA,
        "case_id": str(case_id or ""),
        "case_step": int(case_step or 0),
        "app_id": str(app_id or ""),
        "nodes": list(nodes.values()),
        "edges": edges,
        "entry": next(iter(nodes), ""),
        "intent_ids": list(nodes),
        "domains": sorted({str(n.get("domain") or "") for n in nodes.values()}),
        "covered_targets": [
            {"intent_id": n["id"], "node": "step", "total": len(n["step_refs"])} for n in nodes.values()
        ],
        "steps": annotated,
    }


def validate(graph: dict[str, Any], *, app_id: str = "") -> list[dict[str, Any]]:
    """引用可达校验：只报图中真正引用、但解析不到步骤的块。"""
    from mino_nexus.catalog.flow_block_seed import verify_referenced_blocks

    refs = [str(n.get("block_id") or "") for n in graph.get("nodes") or [] if n.get("block_id")]
    refs += [str(e.get("block_id") or "") for e in graph.get("edges") or [] if e.get("block_id")]
    missing = verify_referenced_blocks(sorted(set(refs)), app_id=app_id)
    return [
        {"reason_code": "missing_ref", "attribution_class": "B", "attribution_label": "程序缺口",
         "layer": "compile", "block_id": bid, "message": f"驱动的意图块 {bid} 没有可执行步骤"}
        for bid in missing
    ]


def describe(*, app_id: str = "", project_id: str = "") -> dict[str, Any]:
    rows = intents(app_id=app_id, project_id=project_id)
    keys = ("id", "label", "domain", "block_id", "triggers", "guards", "private", "case_ids", "case_count", "executions")
    return {"schema": SCHEMA, "domains": domains(),
            "intents": [{k: row.get(k) for k in keys if k in row} for row in rows]}


__all__ = [
    "DOMAINS",
    "SCHEMA",
    "annotate_step",
    "build_case_graph",
    "describe",
    "domains",
    "edges_for",
    "find_intent",
    "global_intents",
    "guards_for_block",
    "guards_for_intent",
    "intent_for_text",
    "intent_for_block",
    "intent_options",
    "intents",
    "validate",
]
