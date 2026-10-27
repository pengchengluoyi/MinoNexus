"""do 阶段：step_program_keys → 子里程碑 + do_program_plan（供 agent-vision-plan 只读）。"""
from __future__ import annotations

import json
from typing import Any

from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestones import (
    _log_milestones_snapshot,
    milestones_empty,
    milestones_from_flow_steps,
    read_state,
    write_state,
    _sync_scope,
)
from mino_nexus.casework.case_step_key_compiler import step_bundle
from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps


_NOOP_MARKERS = (
    "不做任何操作", "不做操作", "无需操作", "无需额外操作", "不执行操作",
    "保持不动", "停留在当前", "不进行任何操作", "不用操作",
)


def _specific_intent(clause: str, ctx: Any) -> str:
    """这段话有没有**专门**的意图条目（排除 general.navigate 兜底）。"""
    try:
        from mino_nexus.knowledge.intent_graph import intent_for_text

        node = intent_for_text(clause, app_id=str(getattr(ctx, "app_id", "") or "")) or {}
    except Exception:  # noqa: BLE001
        return ""
    iid = str(node.get("id") or "")
    return "" if iid in ("", "general.navigate") else iid


def _observation_only(clause: str) -> bool:
    """这一步原文是否只是观察/不操作。"""
    text = str(clause or "")
    return any(m in text for m in _NOOP_MARKERS)


def _case_with_meta(case: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(case, dict):
        return {}
    return case


def resolve_do_program_plan(
    case: dict[str, Any] | None,
    cursor: Any,
    *,
    ctx: Any = None,
) -> dict[str, Any]:
    case_step, phase = step_scope_key(cursor)
    if phase != "do" or case_step <= 0:
        return {}
    cached = getattr(cursor, "do_program_plan", None)
    if isinstance(cached, dict) and int(cached.get("case_step") or 0) == case_step:
        return cached
    c = _case_with_meta(case)
    bundle = step_bundle(c, case_step)
    plan = bundle.get("do_program_plan") if isinstance(bundle.get("do_program_plan"), dict) else {}
    setattr(cursor, "do_program_plan", plan)
    # 执行侧也带当前意图，守卫/事件日志/策略边不用再各自猜步骤语义。
    intent_id = next((str(step.get("intent_id") or "") for step in (plan.get("steps") or [])
                      if isinstance(step, dict) and step.get("intent_id")), "")
    if intent_id:
        setattr(cursor, "current_intent_id", intent_id)
    return plan


def milestones_from_do_program(plan: dict[str, Any], *, app_id: str = "", ctx: Any = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        kind = str(step.get("kind") or "visual_action").strip().lower()
        sid = str(step.get("id") or "").strip() or f"do_{len(rows)}"
        if kind == "remark" or str(step.get("run_policy") or "") == "skip":
            from mino_nexus.loop.intent_handoff import handoff_milestone

            clause = str(step.get("source_clause") or step.get("title") or sid)
            rows.append(handoff_milestone(clause, milestone_id=sid, reason="required_operation_skipped"))
            continue
        if kind == "flow_block":
            bid = str(step.get("block_id") or "").strip()
            if bid:
                block_steps = resolve_effective_steps(app_id=str(app_id or ""), block_id=bid)
                block_ms = milestones_from_flow_steps(block_steps, block_id=bid)
                for m in block_ms:
                    row = dict(m)
                    row["source"] = "do_program"
                    row["source_block"] = bid
                    row["key_ref"] = str(step.get("key_ref") or "")
                    from mino_nexus.action_space.locator_chain import attach_to_row, platform_of

                    attach_to_row(
                        row,
                        platform=platform_of(ctx, row),
                        app_id=str(app_id or getattr(ctx, "app_id", "") or ""),
                        project_id=str(getattr(ctx, "project_id", "") or ""),
                        ctx=ctx,
                    )
                    rows.append(row)
                continue
        row: dict[str, Any] = {
            "id": sid,
            "title": str(step.get("title") or sid)[:240],
            "kind": "hook" if kind == "hook" else ("visual_action" if kind == "nav" else kind),
            "status": "pending",
            "optional": bool(step.get("optional")),
            "source": "do_program",
            "key_ref": str(step.get("key_ref") or ""),
        }
        caps = step.get("exec_caps")
        if isinstance(caps, list) and caps:
            row["exec_caps"] = [str(x) for x in caps if str(x).strip()]
        if kind == "hook" or step.get("hook_cap"):
            row["kind"] = "hook"
            row["hook_cap"] = str(step.get("hook_cap") or "tap_element")
        if kind == "nav" and step.get("nav_target"):
            row["kind"] = "hook"
            row["tier"] = "policy"
            row["exec_caps"] = ["tap_element", "press_key"]
            row["nav_target"] = str(step.get("nav_target") or "")
            row["title"] = f"{row['title']}；到达项目导航目标 {row['nav_target']}，执行后核验目标页面"
        from mino_nexus.resources.step_requirements import contract
        row.update(contract(step))
        if row['requirement'] == 'conditional':
            row.update(optional=False, kind='hook')
        if step.get("observe"):
            row["observe"] = str(step.get("observe") or "exec")
        if isinstance(step.get("params"), dict) and step.get("params"):
            row["params"] = dict(step["params"])
        if step.get("control_id"):
            row["control_id"] = str(step.get("control_id") or "")
        if isinstance(step.get("locator_chain"), dict) and step.get("locator_chain"):
            row["locator_chain"] = dict(step["locator_chain"])
        from mino_nexus.action_space.locator_chain import attach_to_row, platform_of

        attach_to_row(
            row,
            platform=platform_of(ctx, row),
            app_id=str(app_id or getattr(ctx, "app_id", "") or ""),
            project_id=str(getattr(ctx, "project_id", "") or ""),
            ctx=ctx,
        )
        if row.get("hook_cap") == "input_text":
            # 原子输入也需要明确的聚焦动作，不能只给全局登录块配对。
            # 目标继承作者输入项，绝不把待写入值作为 selector。
            params = row.get("params") or {}
            target = params.get("element") or params.get("target") or {}
            if not isinstance(target, dict):
                target = {}
            focus_params = {"target": {**target, "role": "input"}}
            if params.get("field"):
                focus_params["field"] = params["field"]
            focus_row = {
                "id": sid + "__focus",
                "title": "聚焦当前输入项的目标输入框",
                "kind": "hook",
                "hook_cap": "tap_element",
                "status": "pending",
                "optional": row["optional"],
                "source": "do_program",
                "key_ref": "operation.tap",
                "focus_for": sid,
                "params": focus_params,
            }
            if row.get("control_id"):
                focus_row["control_id"] = row["control_id"]
            from mino_nexus.action_space.locator_chain import attach_to_row, platform_of

            attach_to_row(
                focus_row,
                platform=platform_of(ctx, focus_row),
                app_id=str(app_id or getattr(ctx, "app_id", "") or ""),
                project_id=str(getattr(ctx, "project_id", "") or ""),
                ctx=ctx,
            )
            rows.append(focus_row)
        rows.append(row)
    return rows


def seed_do_milestones_from_plan(
    cursor: Any,
    ctx: Any,
    case: dict[str, Any] | None,
    *,
    writer: Any = None,
    app_id: str = "",
) -> dict[str, Any] | None:
    _, phase = step_scope_key(cursor)
    if phase != "do":
        return None
    state = _sync_scope(cursor, read_state(cursor))
    if not milestones_empty(state):
        return getattr(cursor, "do_program_plan", None)
    plan = resolve_do_program_plan(case, cursor, ctx=ctx)
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    if not steps:
        if writer and plan.get("fallback_lines"):
            _log_key_fallback(writer, case, cursor, plan.get("fallback_lines") or [])
        from mino_nexus.loop.intent_handoff import handoff_milestone

        current = cursor.current() if callable(getattr(cursor, "current", None)) else None
        clause = str(getattr(current, "instruction", "") or "") if current is not None else ""
        if not clause and isinstance(case, dict):
            clause = str(case.get("steps_raw") or case.get("name") or "")
        row = handoff_milestone(clause, milestone_id="intent_handoff_do", reason="no_executable_steps")
        if not _specific_intent(clause, ctx):
            # 没有专门意图对上这段话时，不要再落到 general.navigate 兜底：
            # 通用导航需要一个锚点，绕一圈只会以 intent_target_unresolved 收场
            # （cr-af540b4a6ecf 的 api-1-211）。直接把这一步交给模型看图完成。
            row.update(kind="visual_action", status="pending", optional=False)
            row.pop("hook_cap", None)
            row["intent_handoff"] = False
            row["source"] = "do_program_model"
            row["title"] = f"{clause[:200]}（按截图自行完成这一步）"
            if writer:
                writer.append("do/model_step", {"milestone_id": "intent_handoff_do",
                              "clause": clause[:200], "reason": "no_specific_intent"})
        if _observation_only(clause):
            # 「在按钮界面不做任何操作」这类步骤本来就没有可执行动作。
            # 以前无条件派意图，模型被迫去点一个不存在的锚点，最后以
            # milestone_plan_incomplete 收场（cr-318e39386a3b 的 api-1-228）。
            row.update(status="pass", optional=True, noop=True,
                       title=f"{clause[:200]}（该步骤不要求操作）")
            row.pop("hook_cap", None)
            row["intent_handoff"] = False
            if writer:
                writer.append("do/noop_step", {"milestone_id": "intent_handoff_do",
                              "clause": clause[:200], "reason": "observation_only"})
        from mino_nexus.action_space.locator_chain import attach_to_row, platform_of

        attach_to_row(
            row,
            platform=platform_of(ctx, row),
            app_id=str(app_id or getattr(ctx, "app_id", "") or ""),
            project_id=str(getattr(ctx, "project_id", "") or ""),
            ctx=ctx,
        )
        rows = [row]
        state["milestones"] = rows
        state["status"] = "in_progress"
        write_state(cursor, state)
        return plan
    rows = milestones_from_do_program(plan, app_id=app_id, ctx=ctx)
    if not rows:
        return plan
    state["milestones"] = rows
    state["status"] = "in_progress"
    block_id = ""
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("kind") or "").strip().lower() != "flow_block":
            continue
        bid = str(step.get("block_id") or "").strip()
        if bid:
            block_id = bid
            break
    if not block_id:
        for row in rows:
            if isinstance(row, dict) and str(row.get("source_block") or "").strip():
                block_id = str(row["source_block"]).strip()
                break
    if block_id:
        state["block_ref"] = {
            "block_id": block_id,
            "from_milestone": "program_seed:do_keys",
        }
    write_state(cursor, state)
    _log_milestones_snapshot(writer, read_state(cursor), source="program_seed:do_keys")
    if writer:
        try:
            writer.append(
                "milestone/program_seed",
                {
                    "phase": "do",
                    "source": "do_keys",
                    "case_step": plan.get("case_step"),
                    "step_count": len(rows),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return plan


def do_program_plan_json(case: dict[str, Any] | None, cursor: Any, ctx: Any = None) -> str:
    plan = resolve_do_program_plan(case, cursor, ctx=ctx)
    return json.dumps(plan or {}, ensure_ascii=False, indent=2, default=str)


def has_do_program_steps(cursor: Any, case: dict[str, Any] | None) -> bool:
    plan = resolve_do_program_plan(case, cursor)
    return bool(plan.get("steps"))


def _log_key_fallback(writer: Any, case: dict[str, Any] | None, cursor: Any, lines: list[Any]) -> None:
    if not writer or not lines:
        return
    case_step, phase = step_scope_key(cursor)
    try:
        writer.append(
            "key_compile/fallback",
            {
                "telemetry": "red",
                "phase": phase,
                "case_step": case_step,
                "lines": lines[:12],
            },
        )
    except Exception:  # noqa: BLE001
        pass
