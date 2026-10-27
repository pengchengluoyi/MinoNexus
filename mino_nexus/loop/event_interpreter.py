"""单一事件解释器：意图/块/计划步骤 → 事件序列 → 逐条派发。

规划 §3 把 ``prep`` / ``recover`` / ``do`` 降级为**触发标签**，不再各自拥有工具菜单。
所以这里没有 ``prep_menu`` / ``recover_menu``，只有一个入口：给一个意图或块或步骤，
解释成事件并派发。阶段只用来缩 prompt（见 ``runtime/menu.py``）。
"""
from __future__ import annotations

from typing import Any, Iterable

from mino_nexus.core.log import SLog
from mino_nexus.core.schemas import PlanEvent

TAG = "event_interpreter"
SCHEMA = "mino.event_interpreter.v1"

TRIGGERS = ("prep", "do", "check", "recover")


def _pkg_of(ctx: Any, target_package: str = "") -> str:
    return str(target_package or getattr(ctx, "target_package", "") or "")


def _rid_of(ctx: Any, run_id: str = "") -> str:
    """派发用的 run_id 必须是**会话级** id（``cr-…::case-…::sn``）。

    ``ctx.run_id`` 是批次 id、``ctx.session_id`` 才是会话 id；绑定准入按会话比较，
    拿批次 id 派发会被判 ``binding_run_mismatch``（cr-a390541e5dd2 十条用例）。
    """
    session = str(getattr(ctx, "session_id", "") or "")
    if session:
        return session
    return str(run_id or getattr(ctx, "run_id", "") or "")


def _when_ok(step: dict[str, Any], params: dict[str, Any]) -> bool:
    """参数化分支：``when.decision=accept`` 只放行匹配的步骤；无 when 恒真。"""
    when = step.get("when")
    if not isinstance(when, dict) or not when:
        return True
    for key, allowed in when.items():
        got = params.get(str(key))
        if got is None and str(key) == "decision":
            got = "accept"
        values = allowed if isinstance(allowed, (list, tuple, set)) else [allowed]
        if str(got) not in {str(item) for item in values}:
            return False
    return True


def events_from_steps(steps: Iterable[dict[str, Any]], *, ctx: Any, source_label: str = "",
                      target_package: str = "", params: dict[str, Any] | None = None) -> list[PlanEvent]:
    """把块步骤 / 计划步骤转成 PlanEvent；只认 hook_cap/cap，不猜语义。"""
    pkg = _pkg_of(ctx, target_package)
    bind = dict(params or {})
    out: list[PlanEvent] = []
    seq = 0
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if not _when_ok(step, bind):
            continue
        cap = str(step.get("hook_cap") or step.get("cap") or step.get("device_cap") or "").strip()
        if not cap:
            continue
        params = dict(step.get("params") or {})
        # 块的视觉匹配词是步骤级字段；展开成事件参数，参数化分支才能真的点对按钮。
        for key in ("match", "exclude", "selector_text", "text"):
            if key in step and key not in params:
                params[key] = step[key]
        # 块步骤只给候选文案时，DOM 锚点是空的，Scout 只能回「锚点未命中」
        # （cr-9f7f478af02c 的 21 条系统弹窗）。当前结构里正好只有一个候选时，
        # 把它落成 selector_text，DOM 才有得锚。
        if cap in ("tap_element", "long_press_element") and not params.get("selector_text"):
            from mino_nexus.action_space.scheme import structure_target_nodes

            target = params.get("target") if isinstance(params.get("target"), dict) else {}
            candidates = list(params.get("match") or ()) if isinstance(params.get("match"), (list, tuple)) else []
            # 编译出来的块常用 target.name 描述按钮（web 习惯），Android 的 DOM 只认文案；
            # 这里把 match 候选和 target.name 一起当成锚点候选。
            candidates += [target.get("name"), target.get("text"), target.get("content_desc")]
            for candidate in candidates:
                label = str(candidate or "").strip()
                if not label:
                    continue
                if len(structure_target_nodes("tap_element", {"selector_text": label}, ctx)) == 1:
                    params["selector_text"] = label
                    break
        if cap in ("launch_app", "open_app", "close_app") and pkg:
            params.setdefault("package", pkg)
        seq += 1
        out.append(PlanEvent(
            seq=seq,
            capability_id=cap,
            event_kind=cap,
            params=params,
            ai_reasoning=f"{source_label or '步骤'}：{step.get('title') or cap}",
            label=str(step.get("title") or cap),
        ))
    return out


def _dispatched_ok(res: Any) -> bool:
    raw = getattr(res, "status", "")
    return str(getattr(raw, "value", raw) or "").strip().lower() == "pass"


def dispatch_events(events: list[PlanEvent], *, ctx: Any, router: Any, run_id: str = "",
                    purpose: str = "plan_action") -> dict[str, Any]:
    """顺序派发，遇失败即停；返回聚合（不抛异常，由调用方决定终态）。"""
    rid = _rid_of(ctx, run_id)
    actions: list[dict[str, Any]] = []
    last_error = ""
    for idx, event in enumerate(events, 1):
        try:
            res = router.dispatch(event, run_id=rid, step_idx=idx, ctx=ctx, diagnostic_purpose=purpose)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            actions.append({"seq": idx, "capability_id": event.capability_id, "ok": False, "error": last_error})
            break
        ok = _dispatched_ok(res)
        actions.append({
            "seq": idx,
            "capability_id": event.capability_id,
            "ok": ok,
            "status": str(getattr(res, "status", "") or ""),
            "summary": str(getattr(res, "summary", "") or "")[:400],
            "error": str(getattr(res, "error", "") or "")[:400],
        })
        if not ok:
            last_error = str(getattr(res, "error", "") or getattr(res, "summary", "") or "")
            break
    ok = bool(actions) and all(row.get("ok") for row in actions)
    return {"ok": ok, "status": "pass" if ok else "fail", "error": last_error,
            "summary": ("全部事件通过" if ok else (last_error or "事件未通过")),
            "actions": actions, "events": len(events)}


def run_steps(steps: Iterable[dict[str, Any]], *, ctx: Any, router: Any, run_id: str = "",
              purpose: str = "plan_action", target_package: str = "", source_label: str = "",
              params: dict[str, Any] | None = None) -> dict[str, Any]:
    events = events_from_steps(steps, ctx=ctx, source_label=source_label,
                               target_package=target_package, params=params)
    if not events:
        return {"ok": False, "status": "fail", "error": "block_empty",
                "reason_code": "block_empty", "attribution_class": "B",
                "attribution_label": "程序缺口", "layer": "intent",
                "summary": f"{source_label or '块'} 没有可执行事件", "actions": []}
    out = dispatch_events(events, ctx=ctx, router=router, run_id=run_id, purpose=purpose)
    out["source"] = source_label
    return out


def run_block(block_id: str, *, ctx: Any, router: Any, run_id: str = "",
              purpose: str = "plan_action", target_package: str = "",
              params: dict[str, Any] | None = None) -> dict[str, Any]:
    block = str(block_id or "").strip()
    if not block:
        return {"ok": False, "status": "fail", "error": "missing_block_id",
                "reason_code": "missing_ref", "attribution_class": "B",
                "attribution_label": "程序缺口", "layer": "intent",
                "summary": "没有给出逻辑块 id", "actions": []}
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps

    app_id = str(getattr(ctx, "app_id", "") or "")
    steps = list(resolve_effective_steps(app_id=app_id, block_id=block) or [])
    if not steps:
        SLog.w(TAG, f"逻辑块 {block} 没有可执行步骤")
        return {"ok": False, "status": "fail", "error": "block_empty",
                "reason_code": "block_empty", "attribution_class": "B",
                "attribution_label": "程序缺口", "layer": "intent",
                "summary": f"逻辑块 {block} 没有可执行步骤", "actions": []}
    return run_steps(steps, ctx=ctx, router=router, run_id=run_id, purpose=purpose,
                     target_package=target_package, source_label=f"逻辑块 {block}", params=params)


_OBSERVATION_CAPS = frozenset({"hierarchy", "screenshot", "probe_device_state",
                             "get_foreground_app", "get_app_version"})


def expand_intent(intent_id: str, *, ctx: Any, params: dict[str, Any] | None = None,
                  app_id: str = "", target_package: str = "") -> dict[str, Any]:
    """纯展开：意图 → 块步骤或事件；供编译/预演/解释器共用。"""
    from mino_nexus.knowledge.intent_graph import find_intent, intent_for_block

    app = str(app_id or getattr(ctx, "app_id", "") or "")
    params = dict(params or {})
    node = find_intent(intent_id, app_id=app) if str(intent_id or "").startswith(("app.", "auth.", "order.", "message.", "post.", "transfer", "bill.", "risk.", "general.")) else intent_for_block(intent_id, app_id=app)
    if node is None:
        return {"ok": False, "error": "intent_unknown", "intent_id": str(intent_id or ""),
                "reason_code": "intent_unknown", "attribution_class": "B", "layer": "intent",
                "summary": f"未知意图 {intent_id}"}
    block_id = str((node.get("variants") or {}).get(str(params.get("variant") or "")) or node.get("block_id") or "")
    if params.get("variant") and not params.get("decision"):
        # 旧调用方用 variant=accept|decline|allow|deny；参数化块用 decision 同名分支。
        params["decision"] = str(params["variant"])
    if block_id:
        from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps

        steps = list(resolve_effective_steps(app_id=app, block_id=block_id) or [])
        return {"ok": bool(steps), "intent_id": node["id"], "intent_label": node.get("label") or "",
                "domain": node.get("domain") or "", "block_id": block_id, "steps": steps,
                "params": params,
                "events": [{"capability_id": str(s.get("hook_cap") or s.get("cap") or ""),
                            "params": dict(s.get("params") or {})} for s in steps],
                "error": "" if steps else "block_empty",
                "reason_code": "" if steps else "block_empty", "attribution_class": "B" if not steps else "",
                "layer": "intent"}
    # hierarchy / screenshot 是程序每回合自己采的取证，不是模型能力。
    # 放进 caps 再走能力目录，会把 order.pay 整条判成「能力未启用: hierarchy」。
    def _model_caps(row: dict[str, Any]) -> list[str]:
        return [str(c) for c in (row.get("caps") or ()) if str(c) and str(c) not in _OBSERVATION_CAPS]

    caps = _model_caps(node)
    if not caps:
        fallback = find_intent("general.step", app_id=app)
        if fallback and fallback.get("id") != node.get("id"):
            model_caps = _model_caps(fallback)
            if model_caps:
                caps = model_caps
                node = fallback
    if not caps:
        return {"ok": True, "intent_id": node["id"], "intent_label": node.get("label") or "",
                "domain": node.get("domain") or "", "block_id": "", "steps": [], "params": params,
                "events": [],
                "summary": f"意图 {node['id']} 只有程序内部观察，不经能力目录派发",
                "error": "", "reason_code": "", "attribution_class": "", "layer": "intent"}
    return {"ok": True, "intent_id": node["id"], "intent_label": node.get("label") or "",
            "domain": node.get("domain") or "", "block_id": "", "steps": [], "params": params,
            "events": [{"capability_id": cap, "params": params} for cap in caps],
            "error": "", "reason_code": "", "attribution_class": "", "layer": "intent"}


def run_intent(intent_id: str, *, ctx: Any, router: Any, params: dict[str, Any] | None = None,
               run_id: str = "", purpose: str = "plan_action", target_package: str = "") -> dict[str, Any]:
    """意图 → 展开 → 派发。块走 resolve，能力走 dispatch，只有这一条路。"""
    plan = expand_intent(intent_id, ctx=ctx, params=params, target_package=target_package)
    if not plan.get("ok"):
        return {"ok": False, "status": "fail", "error": plan.get("error") or "intent_unknown",
                "reason_code": plan.get("reason_code") or "intent_unknown",
                "attribution_class": plan.get("attribution_class") or "B", "layer": "intent",
                "summary": plan.get("summary") or "", "actions": [], "intent_id": plan.get("intent_id")}
    if plan.get("block_id"):
        out = run_block(plan["block_id"], ctx=ctx, router=router, run_id=run_id,
                        purpose=purpose, target_package=target_package,
                        params=dict(plan.get("params") or params))
    elif not plan.get("events"):
        return {"ok": True, "status": "pass", "error": "", "reason_code": "",
                "attribution_class": "", "layer": "intent",
                "summary": plan.get("summary") or "意图没有需要派发的模型能力",
                "actions": [], "intent_id": plan.get("intent_id"),
                "domain": plan.get("domain"), "block_id": ""}
    else:
        events = events_from_steps([{"hook_cap": row["capability_id"], "params": row.get("params") or {}} for row in plan["events"]],
                                   ctx=ctx, source_label="意图 " + str(plan.get("intent_id") or ""),
                                   target_package=target_package)
        out = dispatch_events(events, ctx=ctx, router=router, run_id=run_id, purpose=purpose)
    out["intent_id"] = plan.get("intent_id")
    out["domain"] = plan.get("domain")
    out["block_id"] = plan.get("block_id") or ""
    return out


def interpret_plan_step(step: dict[str, Any], *, ctx: Any, router: Any, run_id: str = "",
                        purpose: str = "plan_action", target_package: str = "") -> dict[str, Any]:
    """计划步骤的统一入口：优先意图图，其次块，最后单事件。"""
    data = dict(step or {})
    block_id = str(data.get("block_id") or "").strip()
    intent_id = str(data.get("intent_id") or "").strip()
    if block_id:
        return run_block(block_id, ctx=ctx, router=router, run_id=run_id, purpose=purpose,
                         target_package=target_package, params=dict(data.get("params") or {}))
    if intent_id:
        return run_intent(intent_id, ctx=ctx, router=router, params=dict(data.get("params") or {}),
                          run_id=run_id, purpose=purpose, target_package=target_package)
    return run_steps([data], ctx=ctx, router=router, run_id=run_id, purpose=purpose,
                     target_package=target_package, source_label="计划步骤")


def intents_for_trigger(trigger: str, *, app_id: str = "") -> list[dict[str, Any]]:
    """触发标签 → 候选意图。prep/recover 与 do 共用同一张图。"""
    from mino_nexus.knowledge.intent_graph import intents

    tag = str(trigger or "do")
    return [row for row in intents(app_id=app_id) if tag in tuple(row.get("triggers") or ())]


def describe() -> dict[str, Any]:
    return {"schema": SCHEMA, "triggers": list(TRIGGERS),
            "entry": "run_intent | run_block | run_steps | interpret_plan_step",
            "note": "prep/do/check/recover 只是触发标签，不是权限或工具菜单"}


__all__ = [
    "SCHEMA",
    "TRIGGERS",
    "describe",
    "dispatch_events",
    "events_from_steps",
    "expand_intent",
    "interpret_plan_step",
    "intents_for_trigger",
    "run_block",
    "run_intent",
    "run_steps",
]
