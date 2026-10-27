"""子里程碑（success_criteria.milestones）与 evaluate_milestones（P1+）。"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from mino_nexus.loop.llm_step_context import (
    SUCCESS_CRITERIA_SCHEMA,
    empty_success_criteria,
    step_scope_key,
)

AGENT_MILESTONE_KINDS = frozenset(
    {
        "visual_action",
        "hook",
        "checkpoint",
        "internal",
        "visual_tap",
        "visual_input",
    }
)
_TERMINAL = frozenset({"pass", "skipped", "failed"})


def _new_id(prefix: str = "m") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


from mino_nexus.runtime.cursor_state import read_state, write_state  # noqa: F401


def _sync_scope(cursor: Any, state: dict[str, Any]) -> dict[str, Any]:
    case_step, phase = step_scope_key(cursor)
    state = dict(state)
    state["schema"] = SUCCESS_CRITERIA_SCHEMA
    state["phase"] = phase
    state["case_step"] = case_step
    if "exit_allowed" not in state:
        state["exit_allowed"] = False
    ms = state.get("milestones")
    if not isinstance(ms, list):
        state["milestones"] = []
    return state


def milestones_empty(state: dict[str, Any]) -> bool:
    ms = state.get("milestones")
    return not isinstance(ms, list) or len(ms) == 0


def milestones_brief_lines(state: dict[str, Any], *, limit: int = 6) -> list[str]:
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    lines: list[str] = []
    for row in ms:
        if not isinstance(row, dict):
            continue
        st = str(row.get("status") or "pending").strip().lower()
        if st in _TERMINAL and st != "pending":
            continue
        title = str(row.get("title") or row.get("id") or "").strip()
        if not title:
            continue
        opt = "（可选）" if row.get("optional") else ""
        lines.append(f"- [{st}] {title}{opt}")
        if len(lines) >= limit:
            break
    return lines


def milestones_from_check_plan(expected: str, *, instruction: str = "") -> list[dict[str, Any]]:
    import json

    from mino_nexus.loop.check_plan import build_check_plan

    exp = str(expected or "").strip()
    if exp.startswith("{"):
        try:
            doc = json.loads(exp)
            if str(doc.get("schema") or "") == "mino.checkpoints.v1":
                out_v1: list[dict[str, Any]] = []
                for cp in doc.get("checkpoints") or []:
                    if not isinstance(cp, dict):
                        continue
                    cid = str(cp.get("id") or _new_id("c"))
                    title = str(cp.get("title") or cp.get("selector") or cid).strip()[:240]
                    out_v1.append(
                        {
                            "id": cid,
                            "title": title,
                            "kind": "checkpoint",
                            "status": "pending",
                            "optional": not bool(cp.get("required", True)),
                            "checkpoint_kind": str(cp.get("type") or "element_exists"),
                        }
                    )
                if out_v1:
                    return out_v1
        except json.JSONDecodeError:
            pass

    plan = build_check_plan(exp, instruction=str(instruction or ""))
    out: list[dict[str, Any]] = []
    for pt in plan.points:
        out.append(
            {
                "id": str(pt.id or _new_id("c")),
                "title": str(pt.natural_language or plan.raw_expected or "")[:200],
                "kind": "checkpoint",
                "status": "pending",
                "optional": not bool(pt.required),
                "checkpoint_kind": str(pt.kind or "nl"),
            }
        )
    if not out and str(expected or "").strip():
        out.append(
            {
                "id": _new_id("c"),
                "title": str(expected).strip()[:240],
                "kind": "checkpoint",
                "status": "pending",
                "optional": False,
            }
        )
    return out


def milestones_from_flow_steps(
    steps: list[dict[str, Any]],
    *,
    block_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = str(step.get("id") or step.get("cap") or _new_id("b"))
        kind = str(step.get("kind") or "hook").strip().lower()
        title = str(step.get("title") or step.get("label") or sid).strip()
        hook_cap = str(step.get("hook_cap") or "").strip()
        device_cap = str(step.get("cap") or "").strip()
        if kind in ("visual_tap", "visual_input", "visual_action") and not title:
            title = f"视觉步骤 {sid}"
        resolved_kind = kind if kind in AGENT_MILESTONE_KINDS else "hook"
        row: dict[str, Any] = {
            "id": sid,
            "title": title or sid,
            "kind": resolved_kind,
            "status": "pending",
            "optional": bool(step.get("optional")),
        }
        if kind == "nav" and step.get("nav_target"):
            row.update(kind="hook", tier="policy", nav_target=str(step["nav_target"]), exec_caps=["tap_element", "press_key"])
        from mino_nexus.resources.step_requirements import contract
        row.update(contract(step))
        if row['requirement'] == 'conditional':
            row.update(optional=False, kind='hook')
        if step.get("skip_policy"):
            row["skip_policy"] = str(step.get("skip_policy") or "llm")
        if isinstance(step.get("platforms"), list) and step.get("platforms"):
            row["platforms"] = [str(x) for x in step.get("platforms") or [] if str(x).strip()]
        if resolved_kind == "hook":
            if hook_cap:
                row["hook_cap"] = hook_cap
            elif device_cap and device_cap != "input_text":
                row["hook_cap"] = device_cap
            if device_cap == "input_text":
                row["device_cap"] = "input_text"
            elif device_cap and hook_cap and device_cap != hook_cap:
                row["device_cap"] = device_cap
        elif device_cap:
            row["device_cap"] = device_cap
        for key in ("match", "exclude", "match_when"):
            if step.get(key):
                row[key] = step.get(key)
        if isinstance(step.get("params"), dict) and step.get("params"):
            row["params"] = dict(step["params"])
        if step.get("control_id"):
            row["control_id"] = str(step.get("control_id") or "")
        if isinstance(step.get("locator_chain"), dict) and step.get("locator_chain"):
            row["locator_chain"] = dict(step["locator_chain"])
        from mino_nexus.action_space.locator_chain import attach_to_row

        attach_to_row(row)
        rows.append(row)
    if rows:
        return rows
    return rows


def login_block_id_for_ctx(ctx: Any) -> str:
    """登录只走一条链。凭证由 lease_account / get_otp 按用例区分。"""
    from mino_nexus.catalog.flow_block_seed import LOGIN_BLOCK_ID

    return LOGIN_BLOCK_ID


def should_seed_login_block_milestones(ctx: Any, cursor: Any) -> bool:
    """本步 instruction 含登录流且尚未 logged_in → 程序展开逻辑块里程碑（不依赖 LLM 排序）。"""
    cur = cursor.current() if cursor is not None else None
    if cur is None:
        return False
    instr = str(cur.instruction or "")
    from mino_nexus.loop.step_flow_scope import instruction_allows_login_flow

    if not instruction_allows_login_flow(instr, login_module_case=False):
        return False
    fact = dict(getattr(ctx, "session_fact", None) or {}) if ctx is not None else {}
    sess = str(fact.get("session") or "").strip().lower()
    from mino_nexus.navigation.login.evidence import can_commit
    if sess == "logged_in" and can_commit(ctx):
        return False
    return True


def _milestone_passed(row: dict[str, Any]) -> bool:
    st = str(row.get("status") or "pending").strip().lower()
    from mino_nexus.resources.step_requirements import condition_complete
    return condition_complete(row) if row.get("requirement") == "conditional" else st in _TERMINAL


def _prior_incomplete_required(state: dict[str, Any], before_id: str) -> list[str]:
    """before_id 之前仍有未完成的必填里程碑时，返回其 title/id。"""
    pending: list[str] = []
    for row in state.get("milestones") or []:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "")
        if rid == before_id:
            break
        if row.get("optional"):
            continue
        if not _milestone_passed(row):
            pending.append(str(row.get("title") or rid))
    return pending


def first_pending_hook_milestone(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    """返回当前该执行的那个 hook；没有焦点时才按列表顺序取第一个待执行。"""
    rows = state.get("milestones") or []
    # 优先当前焦点：中断栈插入、回看推进、程序跳步都会让「列表第一个」不等于
    # 「当前正在做的那一步」。只按顺序取会在旧里程碑上重复派发——api-1-227 里
    # 焦点已经在 privacy_confirm，调度却回头又发了一次 prep_launch_app。
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().lower() != "in_progress":
            continue
        if _milestone_passed(row):
            continue
        kind = str(row.get("kind") or "").strip().lower()
        if kind != "hook":
            if row.get("optional"):
                continue
            return None
        rid = str(row.get("id") or "")
        if _prior_incomplete_required(state, rid):
            return None
        return row
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _milestone_passed(row):
            continue
        kind = str(row.get("kind") or "").strip().lower()
        if kind == "hook":
            rid = str(row.get("id") or "")
            blockers = _prior_incomplete_required(state, rid)
            if blockers:
                return None
            return row
        if not row.get("optional"):
            return None
    return None


def login_flow_under_milestones(cursor: Any) -> bool:
    """登录逻辑块已展开为 milestones 时，旧 email_login_ui_chain / post_sms 链由 runner+LLM 接管。"""
    state = read_state(cursor)
    ref = state.get("block_ref") if isinstance(state.get("block_ref"), dict) else {}
    if str(ref.get("block_id") or "").strip():
        return True
    for row in state.get("milestones") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("source_block") or "").strip():
            return True
        if str(row.get("id") or "") == "otp_fill" and str(row.get("kind") or "").lower() == "hook":
            return True
    return False


def rewind_otp_field_for_refocus(cursor: Any, *, writer: Any = None) -> bool:
    """otp_fill 需焦点但 editable 未就绪：把焦点里程碑退回 otp_field，避免 otp_ready 空转。"""
    state = _sync_scope(cursor, read_state(cursor))
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    fill_row = milestone_by_id(state, "otp_fill")
    field_row = milestone_by_id(state, "otp_field")
    if fill_row is None or field_row is None:
        return False
    if str(fill_row.get("status") or "") in _TERMINAL:
        return False
    if str(field_row.get("status") or "") not in ("pass",):
        return False
    if not fill_row.get("otp_ready"):
        return False
    field_row["status"] = "in_progress"
    fill_row["status"] = "pending"
    state["milestones"] = ms
    write_state(cursor, state)
    setattr(cursor, "otp_field_refocus_after_rewind", True)
    store = getattr(cursor, "flow_block_tap_streak", None)
    if isinstance(store, dict):
        for k in list(store.keys()):
            if "digit" in k or "code" in k or "验证码" in k:
                store.pop(k, None)
        setattr(cursor, "flow_block_tap_streak", store)
    if writer:
        writer.append(
            "milestone/rewind",
            {
                "from_milestone_id": "otp_fill",
                "to_milestone_id": "otp_field",
                "reason": "web_editable_focus_not_ready",
            },
        )
    from mino_nexus.loop.milestone_orchestrator import ensure_single_in_progress

    ensure_single_in_progress(cursor, writer=writer)
    return True


def _record_tap_milli(ctx: Any, params: dict[str, Any] | None, attr: str) -> None:
    """记下最近一次点击的屏幕坐标。

    `attr` 只可能是下面两个字段之一（由调用点固定），所以显式赋值，
    不做 `setattr` —— 属性名写死才能被检索和类型检查看见。
    """
    if ctx is None or not isinstance(params, dict):
        return
    if attr not in ("web_sms_code_tap_milli", "web_input_tap_milli"):
        return
    try:
        x, y = params.get("x"), params.get("y")
        if x is not None and y is not None:
            value = (int(x), int(y))
            if attr == "web_sms_code_tap_milli":
                ctx.web_sms_code_tap_milli = value
            else:
                ctx.web_input_tap_milli = value
    except (TypeError, ValueError):
        pass


def _record_otp_field_tap_coords(ctx: Any, params: dict[str, Any] | None) -> None:
    _record_tap_milli(ctx, params, "web_sms_code_tap_milli")


def _record_email_field_tap_coords(ctx: Any, params: dict[str, Any] | None) -> None:
    _record_tap_milli(ctx, params, "web_email_tap_milli")


def milestone_by_id(state: dict[str, Any], milestone_id: str) -> Optional[dict[str, Any]]:
    mid = str(milestone_id or "").strip()
    if not mid:
        return None
    for row in state.get("milestones") or []:
        if isinstance(row, dict) and str(row.get("id") or "") == mid:
            return row
    return None


def otp_fill_device_complete(cursor: Any, history: list[str] | None = None) -> bool:
    """otp_fill 设备义务是否已准出（里程碑 pass + 证据），不单信 step_intents。"""
    state = read_state(cursor)
    row = milestone_by_id(state, "otp_fill")
    if not row:
        return False
    if not _milestone_passed(row):
        return False
    ev = str(row.get("evidence") or "")
    if ev == "input_text:sms_code" and str(row.get("exit_eval") or "") == "pass":
        return True
    if ev == "input_text:sms_code":
        from mino_nexus.loop.login_submit import _history_sms_code_filled, _lines_after_sms_sent

        after = _lines_after_sms_sent(list(history or []))
        return _history_sms_code_filled(after)
    return False


def exclusive_hook_caps_from_state(state: dict[str, Any]) -> set[str]:
    """逻辑块 hook 独占能力：从 function tools 菜单剔除（§2.8）。"""
    if not state.get("block_ref"):
        return set()
    caps: set[str] = set()
    for row in state.get("milestones") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("kind") or "").lower() != "hook":
            continue
        hook = str(row.get("hook_cap") or "").strip()
        if hook:
            caps.add(hook)
        rid = str(row.get("id") or "")
        if (
            rid == "otp_fill"
            and hook == "get_otp"
            and row.get("otp_ready")
            and not _milestone_passed(row)
        ):
            caps.add("get_otp")
    return caps


def _log_milestones_snapshot(writer: Any, state: dict[str, Any], *, source: str) -> None:
    if writer is None:
        return
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    writer.append(
        "decision/milestones",
        {
            "source": source,
            "milestones_count": len(ms),
            "status": state.get("status"),
            "pending_ids": [
                str(m.get("id") or "")
                for m in ms
                if isinstance(m, dict) and str(m.get("status") or "pending") not in _TERMINAL
            ][:12],
        },
    )


def _mark_milestone_pass(
    state: dict[str, Any],
    *,
    match_id: str = "",
    match_hook: str = "",
    evidence: str,
) -> bool:
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    changed = False
    for row in ms:
        if not isinstance(row, dict) or _milestone_passed(row):
            continue
        rid = str(row.get("id") or "")
        hook = str(row.get("hook_cap") or "")
        if match_id and rid == match_id:
            row["status"] = "pass"
            row["evidence"] = evidence[:400]
            changed = True
            break
        if match_hook and hook == match_hook:
            row["status"] = "pass"
            row["evidence"] = evidence[:400]
            changed = True
            break
    if changed:
        state["milestones"] = ms
        state["status"] = "in_progress"
    return changed


_LAUNCH_HOOK_CAPS = frozenset({"launch_app", "open_app", "open_url"})


def _hook_cap_matches_tool(hook: str, cap: str) -> bool:
    h = str(hook or "").strip()
    c = str(cap or "").strip()
    if not h or not c:
        return False
    if h == c:
        return True
    return h in _LAUNCH_HOOK_CAPS and c in _LAUNCH_HOOK_CAPS


def _mark_hook_ready_flag(state: dict[str, Any], milestone_id: str, flag: str) -> None:
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    for row in ms:
        if isinstance(row, dict) and str(row.get("id") or "") == milestone_id:
            row[flag] = True
            break
    state["milestones"] = ms


def _tool_matches_declared(row: dict[str, Any], cap: str) -> bool:
    """里程碑写了能力时，别的技能不能把它记成完成。"""
    declared = {
        str(row.get(key) or "").strip()
        for key in ("device_cap", "hook_cap", "cap")
        if str(row.get(key) or "").strip()
    }
    if row.get("fsm_unavailable"):
        listed = row.get("exec_caps") if isinstance(row.get("exec_caps"), list) else []
        actions = {str(item or "").strip() for item in listed if str(item or "").strip()}
        actions.discard("nav_target")
        if actions:
            declared = actions
    if not declared:
        return True
    return cap in declared or any(_hook_cap_matches_tool(item, cap) for item in declared)


def note_tool_pass_milestone(
    cursor: Any,
    *,
    capability_id: str,
    milestone_id: str = "",
    writer: Any = None,
    params: dict[str, Any] | None = None,
    tool_summary: str = "",
    ctx: Any = None,
) -> bool:
    """工具 pass 后推进对应子里程碑（优先 in_progress；hook_cap / id 匹配）。"""
    from mino_nexus.loop.login_verification import record_verification_send
    from mino_nexus.loop.intent_tap import tap_surface_blocks_pass
    cap = str(capability_id or "").strip()
    if cap == "wait_screen_ready":
        setattr(cursor, "wait_ready_passes", int(getattr(cursor, "wait_ready_passes", 0) or 0) + 1)
    elif cap and cap != "wait_ms":
        setattr(cursor, "wait_ready_passes", 0)
    mid = str(milestone_id or "").strip()
    p = dict(params or {})
    if cap == "input_text" and ctx is not None:
        from mino_nexus.loop.channel_observation import attach_input_readback

        p = attach_input_readback(ctx, p)
    state = _sync_scope(cursor, read_state(cursor))
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    if not ms:
        return False
    from mino_nexus.loop.vision_flags import execution_phase_active

    focus_id = ""
    if execution_phase_active() and not mid:
        for row in ms:
            if isinstance(row, dict) and str(row.get("status") or "") == "in_progress":
                focus_id = str(row.get("id") or "").strip()
                break
    changed = False
    for row in ms:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") in _TERMINAL:
            continue
        rid = str(row.get("id") or "")
        if focus_id and rid != focus_id:
            continue
        if row.get("nav_target") and cap in {"tap_element", "tap_xy", "press_key", "swipe_direction"}:
            # Only a matching current target observation can complete navigation.
            continue
        hook = str(row.get("hook_cap") or row.get("cap") or "")
        if mid and rid == mid:
            device = str(row.get("device_cap") or "").strip()
            hook = str(row.get("hook_cap") or "").strip()
            if device and hook and cap == hook and cap != device:
                if cap == "lease_account":
                    row["lease_ready"] = True
                elif cap == "get_otp":
                    row["otp_ready"] = True
                row["evidence"] = cap
                changed = True
                break
            if (
                cap == "get_otp"
                and rid == "otp_fill"
                and str(row.get("device_cap") or "") == "input_text"
            ):
                row["otp_ready"] = True
                row["evidence"] = cap
                changed = True
                break
            if cap == "input_text" and str(row.get("device_cap") or "") == "input_text":
                fld = str(p.get("field") or "").lower()
                from mino_nexus.loop.flow_block_exit import (
                    log_milestone_exit_eval,
                    milestone_device_exit_ok,
                )

                ok_exit, exit_st, exit_reason = milestone_device_exit_ok(
                    row,
                    capability_id=cap,
                    field=fld,
                    summary=str(tool_summary or ""),
                    params=p if isinstance(p, dict) else None,
                )
                log_milestone_exit_eval(
                    writer,
                    milestone_id=rid,
                    exit_status=exit_st,
                    reason=exit_reason,
                    capability_id=cap,
                    would_block=not ok_exit,
                )
                if not ok_exit:
                    break
                row["status"] = "pass"
                row["evidence"] = f"input_text:{fld or 'field'}"
                row["exit_eval"] = "pass"
                changed = True
                break
            if cap == "tap_element":
                from mino_nexus.loop.intent_tap import tap_surface_blocks_pass

                if tap_surface_blocks_pass(
                    cursor, row, params=p, summary=str(tool_summary or ""), writer=writer, ctx=ctx
                ):
                    break
            if not _tool_matches_declared(row, cap):
                break
            row["status"] = "pass"
            row["evidence"] = cap
            changed = True
            break
        if not mid and focus_id and rid == focus_id:
            if (
                cap == "lease_account"
                and rid == "account_fill"
                and str(row.get("device_cap") or "") == "input_text"
            ):
                row["lease_ready"] = True
                row["evidence"] = cap
                changed = True
                break
            if (
                cap == "get_otp"
                and rid == "otp_fill"
                and str(row.get("device_cap") or "") == "input_text"
            ):
                row["otp_ready"] = True
                row["evidence"] = cap
                changed = True
                break
            if cap == "input_text":
                fld = str(p.get("field") or "").lower()
                from mino_nexus.loop.flow_block_exit import (
                    log_milestone_exit_eval,
                    milestone_device_exit_ok,
                )

                ok_exit, exit_st, exit_reason = milestone_device_exit_ok(
                    row,
                    capability_id=cap,
                    field=fld,
                    summary=str(tool_summary or ""),
                    params=p if isinstance(p, dict) else None,
                )
                log_milestone_exit_eval(
                    writer,
                    milestone_id=rid,
                    exit_status=exit_st,
                    reason=exit_reason,
                    capability_id=cap,
                    would_block=not ok_exit,
                )
                if not ok_exit:
                    break
                row["status"] = "pass"
                row["evidence"] = f"input_text:{fld or 'field'}"
                row["exit_eval"] = "pass"
                changed = True
                break
            if cap == "tap_element" and rid == "otp_field":
                _record_otp_field_tap_coords(ctx, p)
                setattr(cursor, "otp_field_refocus_after_rewind", False)
            if cap == "tap_element" and rid == "account_field":
                _record_email_field_tap_coords(ctx, p)
            if cap == "tap_element":
                from mino_nexus.loop.intent_tap import tap_surface_blocks_pass

                if tap_surface_blocks_pass(
                    cursor, row, params=p, summary=str(tool_summary or ""), writer=writer, ctx=ctx
                ):
                    break
            if not _tool_matches_declared(row, cap):
                break
            row["status"] = "pass"
            row["evidence"] = cap
            changed = True
            if rid == "send_code" and ctx is not None:
                from mino_nexus.loop.login_verification import record_verification_send

                record_verification_send(ctx)
                done = getattr(cursor, "step_intents_done", None)
                if isinstance(done, set):
                    done.add("sms_send")
            break
        if not mid and not focus_id and hook and _hook_cap_matches_tool(hook, cap):
            if (
                cap == "get_otp"
                and rid == "otp_fill"
                and str(row.get("device_cap") or "") == "input_text"
            ):
                row["otp_ready"] = True
                row["evidence"] = cap
                changed = True
                break
            row["status"] = "pass"
            row["evidence"] = cap
            changed = True
            break
        if not mid and not focus_id and cap == "lease_account" and hook == "lease_account":
            if rid == "account_fill" and str(row.get("device_cap") or "") == "input_text":
                row["lease_ready"] = True
                row["evidence"] = cap
                changed = True
                break
            row["status"] = "pass"
            row["evidence"] = cap
            changed = True
            break
    if not changed and cap == "input_text":
        fld = str(p.get("field") or "").lower()
        if fld in ("email", "login_email"):
            from mino_nexus.loop.flow_block_exit import (
                log_milestone_exit_eval,
                milestone_device_exit_ok,
            )

            for row in ms:
                if not isinstance(row, dict) or _milestone_passed(row):
                    continue
                if str(row.get("id") or "") == "account_fill" or str(row.get("hook_cap") or "") == "lease_account":
                    ok_exit, exit_st, exit_reason = milestone_device_exit_ok(
                        row,
                        capability_id="input_text",
                        field=fld,
                        summary=str(tool_summary or ""),
                        params=p if isinstance(p, dict) else None,
                    )
                    log_milestone_exit_eval(
                        writer,
                        milestone_id=str(row.get("id") or "account_fill"),
                        exit_status=exit_st,
                        reason=exit_reason,
                        capability_id="input_text",
                        would_block=not ok_exit,
                    )
                    if not ok_exit:
                        break
                    row["status"] = "pass"
                    row["evidence"] = "input_text:email"
                    row["exit_eval"] = "pass"
                    changed = True
                    break
        elif fld in ("sms_code", "验证码", "otp"):
            from mino_nexus.loop.flow_block_exit import (
                log_milestone_exit_eval,
                milestone_device_exit_ok,
            )

            for row in ms:
                if not isinstance(row, dict) or _milestone_passed(row):
                    continue
                if str(row.get("id") or "") == "otp_fill" or str(row.get("hook_cap") or "") == "get_otp":
                    ok_exit, exit_st, exit_reason = milestone_device_exit_ok(
                        row,
                        capability_id="input_text",
                        field=fld,
                        summary=str(tool_summary or ""),
                    )
                    log_milestone_exit_eval(
                        writer,
                        milestone_id=str(row.get("id") or "otp_fill"),
                        exit_status=exit_st,
                        reason=exit_reason,
                        capability_id="input_text",
                        would_block=not ok_exit,
                        evidence=str(row.get("evidence") or ""),
                    )
                    if not ok_exit:
                        break
                    row["status"] = "pass"
                    row["evidence"] = "input_text:sms_code"
                    row["exit_eval"] = "pass"
                    changed = True
                    break
    if not changed and cap == "request_sms_code":
        for row in ms:
            if not isinstance(row, dict) or _milestone_passed(row):
                continue
            if str(row.get("id") or "") == "send_code" or str(row.get("device_cap") or "") == "request_sms_code":
                row["status"] = "pass"
                row["evidence"] = cap
                changed = True
                break
    if not changed and cap == "get_otp":
        for row in ms:
            if not isinstance(row, dict) or _milestone_passed(row):
                continue
            if str(row.get("id") or "") == "otp_fill" or str(row.get("hook_cap") or "") == "get_otp":
                if str(row.get("id") or "") == "otp_fill" and str(row.get("device_cap") or "") == "input_text":
                    row["otp_ready"] = True
                    row["evidence"] = cap
                else:
                    row["status"] = "pass"
                    row["evidence"] = cap
                changed = True
                break
    if not changed and cap == "operation.check_box":
        for row in ms:
            if not isinstance(row, dict) or _milestone_passed(row):
                continue
            if str(row.get("id") or "") == "legal_consent":
                row["status"] = "pass"
                row["evidence"] = cap
                changed = True
                break
    if not changed and cap == "tap_element" and focus_id:
        sel = str((p or {}).get("selector_text") or (p or {}).get("text") or "")[:120]
        for row in ms:
            if not isinstance(row, dict) or str(row.get("id") or "") != focus_id:
                continue
            rid = str(row.get("id") or "")
            device_cap = str(row.get("device_cap") or "").strip()
            if rid == "legal_consent" or device_cap == "operation.check_box":
                break
            kind = str(row.get("kind") or "").strip().lower()
            from mino_nexus.loop.flow_block_exit import hook_device_row

            if kind == "hook" and hook_device_row(row):
                break
            if rid == "otp_field" and isinstance(p, dict):
                _record_otp_field_tap_coords(ctx, p)
                setattr(cursor, "otp_field_refocus_after_rewind", False)
            if rid == "account_field" and isinstance(p, dict):
                _record_email_field_tap_coords(ctx, p)
            if rid == "send_code":
                from mino_nexus.action_space.scheme import execution_scheme as action_scheme

                if action_scheme(ctx) != "visual":
                    intents = getattr(cursor, "step_intents_done", None)
                    if isinstance(intents, set):
                        intents.add("sms_send")
                    cursor.login_post_sms_pending = True
                    cursor.login_post_sms_stall = 0
                    setattr(cursor, "send_code_tap_passed", True)
                    if ctx is not None:
                        from mino_nexus.loop.login_verification import record_verification_send

                        record_verification_send(ctx)
            if kind in ("visual_tap", "visual_action", "visual_input"):
                row["status"] = "pass"
                row["evidence"] = f"tap_element:{sel}" if sel else cap
                changed = True
                break
    if not changed:
        return False
    if cap == "get_otp":
        for row in ms:
            if isinstance(row, dict) and str(row.get("id") or "") == "otp_fill":
                if not _milestone_passed(row):
                    row["otp_ready"] = True
    if ctx is not None and isinstance(row, dict) and row.get("status") == "pass":
        from mino_nexus.navigation.login.bookkeeping import confirm_account_fill, note_submit_result
        attempt = f"{getattr(writer, 'session_id', '')}:{getattr(writer, 'turn', 0)}" if writer else ""
        observation = str(getattr(ctx, "decision_screenshot_hash", "") or "")
        if row.get("id") == "account_fill" and cap == "input_text":
            confirm_account_fill(ctx, p, attempt_ref=attempt, observation_ref=observation)
        elif row.get("id") == "submit_login" and cap in {"tap_element", "tap_xy"}:
            from mino_nexus.navigation.login.transition import bind_submit_frame
            bind_submit_frame(cursor, ctx, attempt_ref=attempt, writer=writer)
            note_submit_result(ctx, passed=True, attempt_ref=attempt, observation_ref=observation)
    state["milestones"] = ms
    state["status"] = "in_progress"
    write_state(cursor, state)
    if ctx is not None and cap == "clear_app_cache":
        from mino_nexus.casework.resource_transition import emit_clear_app_cache

        emit_clear_app_cache(ctx)
        # pm clear 是确定性重置事实，凡是程序派发的清理都要落标记，
        # 否则 confirm_login_state 会以为「还没重置过」，在非登录页上要求继续取证
        # （cr-d9c1b4ce4799 api-1-227 实测：clear 已 pass，prep_clear_done 仍为 False）。
        ctx.prep_clear_done = True
    if ctx is not None and cap in ("launch_app", "open_app"):
        # 程序派发的启动（prep 里程碑）也要算「已确认启动」，
        # 否则 prep_launch_app 仍 pending，逻辑块会再启动一次（api-1-218 实测两次）。
        ctx.app_launch_confirmed = True
    if ctx is not None and cap in ("close_app", "kill_app", "clear_app_cache"):
        # 关进程 / 清数据之后应用不再运行，之前的「已启动」事实作废；
        # 否则恢复块里的 launch_app 会被去重逻辑跳过，应用永远起不来。
        ctx.app_launch_confirmed = False
    if ctx is not None and cap == "close_app":
        plat = str(getattr(ctx, "platform", "") or "").strip().lower()
        if plat in ("web", "browser", "playwright"):
            from mino_nexus.casework.resource_transition import emit_device_logout

            emit_device_logout(ctx, source="browser_close")
            from mino_nexus.loop.login_state_probe import note_web_context_closed

            note_web_context_closed(ctx)
    if ctx is not None and cap in ("launch_app", "open_url", "open_app"):
        plat = str(getattr(ctx, "platform", "") or "").strip().lower()
        if plat in ("web", "browser", "playwright"):
            from mino_nexus.loop.login_state_probe import note_web_context_start

            note_web_context_start(ctx)
    if ctx is not None and not getattr(cursor, "login_state_committed", False):
        for row in ms:
            if (
                isinstance(row, dict)
                and str(row.get("id") or "") == "login_state"
                and str(row.get("status") or "") == "pass"
            ):
                from mino_nexus.casework.resource_transition import emit_login_complete

                cursor.login_state_committed = emit_login_complete(ctx)
                break
    if cap == "confirm_login_state" and ctx is not None:
        _seed_login_when_required_session_unmet(cursor, ctx, writer=writer)
    from mino_nexus.loop.milestone_orchestrator import advance_in_progress_focus

    advance_in_progress_focus(cursor, writer=writer)
    from mino_nexus.loop.fuse.interrupt_stack import maybe_pop_interrupt

    maybe_pop_interrupt(cursor, writer=writer)
    pg = getattr(cursor, "progress_gate", None)
    if pg is not None and hasattr(pg, "note_submilestone_pass"):
        pg.note_submilestone_pass()
    _log_milestones_snapshot(writer, state, source=f"tool_pass:{cap}")
    return True


def note_tool_fail_milestone(
    cursor: Any,
    ctx: Any,
    *,
    capability_id: str = "",
    summary: str = "",
    writer: Any = None,
) -> bool:
    """vision-exec 工具 fail：累计 fail_streak，必要时 push interrupt。"""
    from mino_nexus.loop.vision_flags import execution_phase_active

    if not execution_phase_active():
        return False
    from mino_nexus.loop.fuse.interrupt_stack import note_in_progress_tool_fail

    return note_in_progress_tool_fail(
        cursor,
        ctx,
        writer=writer,
        reason=summary or capability_id,
    )


def _case_steps_include_login(case: Any) -> bool:
    if not isinstance(case, dict):
        return False
    from mino_nexus.loop.step_contract import case_plan_has_login

    return case_plan_has_login(case)


def _seed_login_when_required_session_unmet(cursor: Any, ctx: Any, *, writer: Any = None) -> None:
    """前置要求已登录时独立准备登录，不被稍后的操作阶段登录覆盖。"""
    from mino_nexus.runtime.session_gate import required_session

    scene = getattr(ctx, "case_scene", None)
    if required_session(scene=scene if isinstance(scene, dict) else None) != "logged_in":
        return
    session = str(getattr(ctx, "device_session", "") or "").strip().lower()
    if session in ("", "logged_in"):
        return
    if str(getattr(cursor, "phase", "") or "") != "prep" and _case_steps_include_login(getattr(ctx, "case", None)):
        return
    if str(getattr(cursor, "phase", "") or "") == "prep":
        from mino_nexus.loop.step_flow_scope import prep_login_preservation_reason

        reason = prep_login_preservation_reason(ctx)
        if reason:
            if writer is not None:
                writer.append("prep/login_preserved", {"reason_code": "initial_session_required",
                              "summary": reason, "target_account_login_deferred": True})
            return
    state = read_state(cursor)
    app_id = str(getattr(ctx, "app_id", "") or "")
    seeded = prepend_login_block_milestones(state, ctx=ctx, app_id=app_id)
    if seeded is state:
        return
    write_state(cursor, seeded)
    if writer is not None:
        _log_milestones_snapshot(writer, seeded, source="login_seed_for_required_session")


def prepend_login_block_milestones(
    state: dict[str, Any],
    *,
    ctx: Any,
    app_id: str,
) -> dict[str, Any]:
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps

    block_id = login_block_id_for_ctx(ctx)
    if str((state.get("block_ref") or {}).get("block_id") or "") == block_id:
        return state
    steps = resolve_effective_steps(app_id=str(app_id or ""), block_id=block_id)
    block_ms = milestones_from_flow_steps(steps, block_id=block_id)
    if not block_ms:
        return state
    existing = [
        m
        for m in (state.get("milestones") or [])
        if isinstance(m, dict) and str(m.get("source_block") or "") != block_id
    ]
    tagged = [{**m, "source_block": block_id} for m in block_ms]
    state = dict(state)
    state["milestones"] = tagged + existing
    state["block_ref"] = {"block_id": block_id, "from_milestone": tagged[0].get("id") if tagged else ""}
    state["status"] = "in_progress"
    return state


def on_enter_check_phase(cursor: Any, *, expected: str = "", instruction: str = "") -> None:
    state = _sync_scope(cursor, read_state(cursor))
    state["milestones"] = milestones_from_check_plan(expected, instruction=instruction)
    state["status"] = "in_progress"
    state.pop("block_ref", None)
    write_state(cursor, state)


def ensure_milestones_before_decide(
    cursor: Any,
    ctx: Any,
    *,
    app_id: str = "",
    writer: Any = None,
    case: dict[str, Any] | None = None,
) -> None:
    from mino_nexus.loop.program_plan_seed import ensure_program_milestone_seeds

    state = _sync_scope(cursor, read_state(cursor))
    _, phase = step_scope_key(cursor)
    before = milestones_empty(state)
    ensure_program_milestone_seeds(
        cursor,
        ctx,
        case,
        writer=writer,
        app_id=app_id,
        include_prep=False,
    )
    state = read_state(cursor)
    seeded = before and not milestones_empty(state)
    if phase == "check" and milestones_empty(state):
        cur = cursor.current()
        exp = str(getattr(cur, "expected", "") or "") if cur else ""
        instr = str(getattr(cur, "instruction", "") or "")
        state = _sync_scope(cursor, state)
        state["milestones"] = milestones_from_check_plan(exp, instruction=instr)
        state["status"] = "in_progress"
        write_state(cursor, state)
        seeded = True
    if seeded:
        _log_milestones_snapshot(writer, read_state(cursor), source="program_seed")


def _normalize_milestone_row(raw: Any, *, warnings: list[str]) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    mid = str(raw.get("id") or "").strip() or _new_id()
    title = str(raw.get("title") or raw.get("name") or mid).strip()
    kind = str(raw.get("kind") or "visual_action").strip().lower()
    st = str(raw.get("status") or "pending").strip().lower()
    if st not in _TERMINAL and st != "in_progress":
        st = "pending"
    row = {
        "id": mid,
        "title": title[:240],
        "kind": kind,
        "status": st,
        "optional": bool(raw.get("optional")),
    }
    if raw.get("evidence"):
        row["evidence"] = str(raw.get("evidence") or "")[:400]
    if raw.get("skip_reason"):
        row["skip_reason"] = str(raw.get("skip_reason") or "")[:200]
    if raw.get("hook_cap"):
        row["hook_cap"] = str(raw.get("hook_cap") or "")
    if isinstance(raw.get("hook_params"), dict):
        row["hook_params"] = dict(raw["hook_params"])
    if raw.get("recovery_rule_id"):
        row["recovery_rule_id"] = str(raw["recovery_rule_id"])[:120]
    return row


def apply_decision_milestones(
    cursor: Any,
    decision: Any,
    *,
    writer: Any = None,
    ctx: Any = None,
    app_id: str = "",
) -> None:
    raw = getattr(decision, "raw_llm", None)
    payload = raw.get("llm_output") if isinstance(raw, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    state = _sync_scope(cursor, read_state(cursor))
    warnings: list[str] = []
    applied_fresh_llm = False

    fresh = payload.get("milestones")
    if not fresh and isinstance(getattr(decision, "milestones", None), list):
        fresh = decision.milestones
    if isinstance(fresh, list) and fresh and milestones_empty(state):
        rows = []
        for item in fresh:
            row = _normalize_milestone_row(item, warnings=warnings)
            if row:
                rows.append(row)
        if rows:
            state["milestones"] = rows
            state["status"] = "in_progress"
            applied_fresh_llm = True

    if (
        applied_fresh_llm
        and ctx is not None
        and should_seed_login_block_milestones(ctx, cursor)
    ):
        state = prepend_login_block_milestones(state, ctx=ctx, app_id=app_id)
        if writer:
            _log_milestones_snapshot(writer, state, source="login_block_top")

    write_state(cursor, state)
    if writer and (warnings or fresh):
        _log_milestones_snapshot(writer, state, source="llm_apply")


def apply_step_outcome(
    cursor: Any,
    decision: Any,
    ctx: Any,
    *,
    writer: Any = None,
) -> None:
    """§2.7：step_outcome 写入 session 并映射到 decision.status。"""
    raw = getattr(decision, "raw_llm", None)
    payload = raw.get("llm_output") if isinstance(raw, dict) else {}
    so = str(getattr(decision, "step_outcome", "") or "").strip().lower()
    if not so and isinstance(payload, dict):
        so = str(payload.get("step_outcome") or "").strip().lower()
    if so not in ("pass", "give_up", "ask_human", "skip"):
        return
    case_step, phase = step_scope_key(cursor)
    if writer:
        writer.append(
            "decision/step_outcome",
            {
                "step_outcome": so,
                "phase": phase,
                "case_step": case_step,
                "confidence": float(getattr(decision, "confidence", 0) or 0),
            },
        )
    if so == "pass":
        mv = evaluate_milestones(cursor, ctx)
        if mv.phase_complete:
            decision.status = "done"
        return
    if so == "skip":
        decision.status = "skip"
    elif so == "ask_human":
        decision.status = "ask_human"
    else:
        decision.status = "give_up"


def apply_check_evidences_to_milestones(
    cursor: Any,
    evidences: list[Any],
    *,
    writer: Any = None,
) -> None:
    """§2.9：程序化 check 证据写回 checkpoint 里程碑。"""
    if not evidences:
        return
    state = _sync_scope(cursor, read_state(cursor))
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    if not ms:
        return
    by_id = {str(m.get("id") or ""): m for m in ms if isinstance(m, dict)}
    changed = False
    for ev in evidences:
        pid = str(getattr(ev, "point_id", "") or "")
        if not pid or pid not in by_id:
            continue
        row = by_id[pid]
        st = str(getattr(ev, "status", "") or "").strip().lower()
        summ = str(getattr(ev, "summary", "") or "")[:400]
        if st == "pass":
            row["status"] = "pass"
            row["evidence"] = summ
            changed = True
        elif st == "fail":
            row["status"] = "failed"
            row["evidence"] = summ
            changed = True
    if not changed:
        return
    state["milestones"] = list(by_id.values())
    write_state(cursor, state)
    _log_milestones_snapshot(writer, state, source="check_programmatic")


def apply_assert_checkpoint_results(cursor: Any, result: Any, *, writer: Any = None) -> bool:
    """仅用本次断言逐项覆盖的有效证据推进当前检查点。"""
    state = _sync_scope(cursor, read_state(cursor))
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    pending = {
        str(row.get("id") or ""): row
        for row in milestones_for_phase_eval(state, cursor)
        if str(row.get("kind") or "") == "checkpoint"
        and str(row.get("status") or "pending") not in _TERMINAL
    }
    rows = getattr(result, "checkpoint_results", [])
    if not pending or not isinstance(rows, list) or len(rows) != len(pending):
        return False
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return False
        point_id = str(row.get("point_id") or "")
        if point_id not in pending or point_id in seen:
            return False
        if type(row.get("passed")) is not bool or not str(row.get("evidence") or "").strip():
            return False
        seen.add(point_id)
    if getattr(result, "error_kind", "") or getattr(result, "parse_warnings", []):
        return False
    if seen != set(pending) or not all(row["passed"] for row in rows if not pending[row["point_id"]].get("optional")):
        return False
    for row in rows:
        milestone = pending[row["point_id"]]
        milestone["status"] = "pass" if row["passed"] else "failed"
        milestone["evidence"] = str(row["evidence"]).strip()[:400]
        milestone["evidence_scope"] = str(row.get("evidence_scope") or "visible_full")
        milestone["hidden_content_verified"] = row.get("hidden_content_verified") is True
        if row.get("text_match"):
            from mino_nexus.loop.text_assert import checkpoint_report
            milestone["text_verification"] = checkpoint_report(row)
    state["milestones"] = ms
    write_state(cursor, state)
    _log_milestones_snapshot(writer, state, source="check_assert")
    return True


@dataclass
class MilestoneVerdict:
    phase_complete: bool = False
    status: str = "pending"
    summary: str = ""


def try_finish_do_if_milestones_complete(
    cursor: Any,
    ctx: Any,
    *,
    writer: Any = None,
) -> bool:
    """do 自动收工统一经里程碑裁决及阶段转换，不以意图/探针独立放行。"""
    if str(getattr(cursor, "phase", "") or "").strip().lower() != "do":
        return False
    mv = evaluate_milestones(cursor, ctx)
    if not mv.phase_complete:
        return False
    scoped = milestones_for_phase_eval(read_state(cursor), cursor)
    if not scoped:
        return False
    step_ops = int(getattr(cursor, "step_ops", 0) or 0)
    has_do_seed = any(
        str(m.get("source") or "") in ("do_program", "program_seed:do_keys") for m in scoped
    )
    if step_ops <= 0 and not has_do_seed:
        return False
    pending = [
        m
        for m in scoped
        if str(m.get("status") or "pending").strip().lower() not in _TERMINAL
        and not m.get("optional")
    ]
    if pending:
        return False
    cur = cursor.current() if hasattr(cursor, "current") else None
    instr = str(getattr(cur, "instruction", "") or "") if cur else ""
    allow_finish = getattr(cursor, "_login_completion_allows_do_finish", None)
    if callable(allow_finish) and not allow_finish(instr):
        return False
    from mino_nexus.loop.step_phase_fsm import apply_phase_transition

    if apply_phase_transition(cursor, ctx, mv, writer=writer) is None:
        return False
    if hasattr(cursor, "require_do_work_streak"):
        cursor.require_do_work_streak = 0
    if hasattr(cursor, "correction_hint"):
        cursor.correction_hint = ""
    if writer:
        writer.append(
            "decision/milestones_auto_finish",
            {"summary": mv.summary, "to_phase": str(getattr(cursor, "phase", "") or "")},
        )
    return True


_PREP_ONLY_SOURCES = frozenset({"prep_program"})


def milestones_for_phase_eval(state: dict[str, Any], cursor: Any) -> list[dict[str, Any]]:
    """收工判定只看当前 phase 的里程碑（避免 prep 已 pass 的列表误触发 do→check）。"""
    _, phase = step_scope_key(cursor)
    ms = [m for m in (state.get("milestones") or []) if isinstance(m, dict)]
    if phase == "do":
        scoped = [m for m in ms if str(m.get("source") or "") not in _PREP_ONLY_SOURCES]
        return scoped
    if phase == "prep":
        return [m for m in ms if str(m.get("source") or "") in _PREP_ONLY_SOURCES or not str(m.get("source") or "")]
    if phase == "check":
        return [
            m
            for m in ms
            if str(m.get("kind") or "") == "checkpoint"
            or str(m.get("source") or "").startswith("check")
        ]
    return ms


def evaluate_milestones(
    cursor: Any,
    ctx: Any,
    *,
    programmatic_pass: bool = False,
) -> MilestoneVerdict:
    state = read_state(cursor)
    ms = milestones_for_phase_eval(state, cursor)
    if not ms:
        return MilestoneVerdict(phase_complete=False, status="pending", summary="无里程碑")
    failed = []
    pending_required = []
    for row in ms:
        if not isinstance(row, dict):
            continue
        st = str(row.get("status") or "pending").strip().lower()
        from mino_nexus.resources.step_requirements import condition_complete
        if row.get('requirement') == 'conditional' and not condition_complete(row):
            pending_required.append(str(row.get('title') or row.get('id')))
            continue
        opt = bool(row.get("optional"))
        title = str(row.get("title") or row.get("id") or "")
        if st == "failed" and not (opt and row.get("kind") == "checkpoint"):
            failed.append(title)
        elif st not in _TERMINAL:
            if not opt:
                pending_required.append(title)
    if failed:
        return MilestoneVerdict(
            phase_complete=False,
            status="failed",
            summary=f"里程碑失败：{'; '.join(failed[:3])}",
        )
    if pending_required and not programmatic_pass:
        return MilestoneVerdict(
            phase_complete=False,
            status="in_progress",
            summary=f"待完成：{'; '.join(pending_required[:3])}",
        )
    if programmatic_pass and pending_required:
        return MilestoneVerdict(
            phase_complete=False,
            status="in_progress",
            summary="程序化总结果不能替代逐项校验",
        )
    if not pending_required:
        from mino_nexus.loop.fuse.interrupt_stack import stack_depth

        if stack_depth(cursor) > 0:
            return MilestoneVerdict(
                phase_complete=False,
                status="in_progress",
                summary="恢复栈未结束，不进入校验",
            )
        phase = str(state.get("phase") or getattr(cursor, "phase", "") or "").strip().lower()
        if phase == "check" and state.get("exit_allowed") is not True:
            state["exit_allowed"] = True
        if phase in ("prep", "do", "check") and state.get("exit_allowed") is not True:
            return MilestoneVerdict(
                phase_complete=False,
                status="in_progress",
                summary="里程碑已完成，等待准出",
            )
        state = _sync_scope(cursor, state)
        state["status"] = "pass"
        write_state(cursor, state)
        return MilestoneVerdict(phase_complete=True, status="pass", summary="里程碑已全部完成")
    return MilestoneVerdict(phase_complete=False, status="in_progress", summary="进行中")
