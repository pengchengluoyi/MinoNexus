"""入口选择、单跳探针与到达判定。

运行时只允许做一件事：从当前页沿一条入口档案走到目标页。没有入口就报缺口，
不得用“先点点看”替代规划。每条入口最多探 1 次点击，下一次观察必须确认到达。
"""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.navigation.entries import store
from mino_nexus.navigation.entries.fingerprint import compare_fingerprints, page_fingerprint
from mino_nexus.navigation.entries.text import labels_for_page, norm_key, norm_text, parse_step

TAG = "NavEntryPlan"
_ARRIVE_FP = 0.62
_ARRIVE_STATE = 0.55


def _nodes(ctx: Any) -> list[dict[str, Any]]:
    raw = getattr(ctx, "nav_hierarchy_nodes", None)
    return list(raw) if isinstance(raw, list) else []


def _localized(ctx: Any) -> dict[str, Any]:
    raw = getattr(ctx, "nav_localized", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _entry_labels(entry: dict[str, Any]) -> list[str]:
    expect = entry.get("expect") if isinstance(entry.get("expect"), dict) else {}
    out: list[str] = []
    for item in expect.get("target_label") or []:
        out.extend(labels_for_page(str(item)))
    out.extend(labels_for_page(str(entry.get("target_label") or "")))
    return [item for item in dict.fromkeys(out) if item]


def _selector(entry: dict[str, Any]) -> dict[str, Any]:
    trigger = entry.get("trigger") if isinstance(entry.get("trigger"), dict) else {}
    selector = trigger.get("selector") if isinstance(trigger.get("selector"), dict) else {}
    return selector


def _selector_values(entry: dict[str, Any]) -> list[tuple[str, str]]:
    trigger = entry.get("trigger") if isinstance(entry.get("trigger"), dict) else {}
    selector = _selector(entry)
    values: list[tuple[str, str]] = []
    for key in ("resource_id", "content_desc", "text", "class_name"):
        value = str(selector.get(key) or "").strip()
        if value:
            values.append((key, value))
    for row in trigger.get("locators") or []:
        if isinstance(row, dict):
            by = str(row.get("by") or "")
            value = str(row.get("value") or "").strip()
            if by and value:
                values.append((by, value))
    text = str(trigger.get("text") or "").strip()
    if text:
        values.append(("text", text))
    seen: set[tuple[str, str]] = set()
    return [item for item in values if not (item in seen or seen.add(item))]


def _node_text(node: dict[str, Any]) -> tuple[str, str, str]:
    return (
        norm_text(node.get("text") or ""),
        norm_text(node.get("content_desc") or node.get("desc") or ""),
        str(node.get("resource_id") or node.get("id") or ""),
    )


def find_trigger(nodes: list[dict[str, Any]], entry: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
    """按 resource-id > content-desc > text > class 的优先级找入口控件。

    resource-id 最稳；没有 id 时退到 content-desc，再到可见文案，最后才用 class。
    这个顺序也写进入口证据，运行时不临时换策略。
    """
    values = _selector_values(entry)
    if not values:
        return None, 0.0
    best: tuple[float, dict[str, Any]] | None = None
    for node in nodes or []:
        if not isinstance(node, dict) or node.get("visible") is False:
            continue
        text, desc, rid = _node_text(node)
        if not (text or desc or rid):
            continue
        clickable = bool(node.get("clickable"))
        for by, value in values:
            want = norm_text(value)
            if not want:
                continue
            score = 0.0
            if by == "resource_id" or by == "id":
                if rid and (rid == value or rid.endswith("/" + value) or value.endswith("/" + rid.rsplit("/", 1)[-1])):
                    score = 1.0
            elif by in ("content_desc", "desc"):
                if desc and desc == want:
                    score = 0.98
                elif desc and want and want in desc:
                    score = 0.82
                elif desc and len(desc) >= 2 and desc in want:
                    score = 0.72
            elif by == "text":
                if text and text == want:
                    score = 0.96
                elif text and want and want in text:
                    score = 0.78
                elif text and len(text) >= 2 and text in want:
                    score = 0.7
            elif by in ("contains_text", "contains"):
                if (text and want in text) or (desc and want in desc):
                    score = 0.74
            elif by == "class_name":
                if str(node.get("class") or "").endswith(value):
                    score = 0.55
            if score <= 0:
                continue
            if clickable:
                score += 0.04
            if best is None or score > best[0]:
                best = (score, node)
    return (best[1], round(min(1.0, best[0]), 4)) if best else (None, 0.0)


def _is_arrived(entry: dict[str, Any], ctx: Any, nodes: list[dict[str, Any]], current_fp: dict[str, Any]) -> dict[str, Any]:
    localized = _localized(ctx)
    chosen = str(localized.get("chosen") or getattr(ctx, "nav_localized_state", "") or "")
    conf = float(localized.get("confidence") or getattr(ctx, "nav_localized_confidence", 0) or 0)
    target_state = str(entry.get("target_state_id") or "")
    if target_state and chosen and chosen == target_state and conf >= _ARRIVE_STATE:
        return {"arrived": True, "by": "localized_state", "score": conf}
    fingerprint = entry.get("fingerprint") if isinstance(entry.get("fingerprint"), dict) else {}
    if fingerprint.get("hash") and current_fp.get("hash"):
        cmp = compare_fingerprints(fingerprint, current_fp)
        if float(cmp.get("score") or 0.0) >= _ARRIVE_FP:
            return {"arrived": True, "by": "fingerprint", "score": cmp.get("score"), "compare": cmp}
    labels = _entry_labels(entry)
    if labels and _label_as_page_header(nodes, labels):
        return {"arrived": True, "by": "label_header", "score": 0.62, "labels": labels}
    return {"arrived": False, "by": "", "score": 0.0}


def _label_as_page_header(nodes: list[dict[str, Any]], labels: list[str]) -> bool:
    """页名出现在标题/头部才算到达；底栏同名 Tab 不算。

    用例入口可能没有指纹，只能用页名兜底。底栏「我的」在每一页都可见，
    如果把它当到达，会刚点完就误判成功。
    """
    wanted = [norm_key(label) for label in labels if len(norm_key(label)) >= 2]
    if not wanted:
        return False
    max_y = 0
    for node in nodes or []:
        bounds = node.get("bounds") if isinstance(node, dict) else None
        if isinstance(bounds, (list, tuple)) and len(bounds) >= 4:
            max_y = max(max_y, int(bounds[3]))
    for node in nodes or []:
        if not isinstance(node, dict) or node.get("visible") is False:
            continue
        if bool(node.get("clickable")):
            continue
        text = norm_key(f"{node.get('text') or ''} {node.get('content_desc') or ''}")
        if not text:
            continue
        if not any(label in text for label in wanted):
            continue
        bounds = node.get("bounds")
        if isinstance(bounds, (list, tuple)) and len(bounds) >= 4 and max_y:
            center_y = (int(bounds[1]) + int(bounds[3])) / 2
            if center_y >= max_y * 0.82:
                continue
        return True
    return False


def _target_from_step(instruction: str, expected: str = "") -> str:
    for raw in (instruction, expected):
        if not raw:
            continue
        parsed = parse_step(raw)
        target = str(parsed.get("target") or "")
        if target:
            return target
    return ""


def _target_from_instruction(instruction: str) -> str:
    return _target_from_step(instruction)


def _current_state(ctx: Any) -> str:
    localized = _localized(ctx)
    return str(localized.get("chosen") or getattr(ctx, "nav_localized_state", "") or "")


def _platform(ctx: Any) -> str:
    return str(getattr(ctx, "platform", "") or getattr(ctx, "env_surface", "") or "")


def _gap(
    *,
    app_id: str,
    project_id: str,
    target: str,
    target_state: str = "",
    from_state: str = "",
    kind: str,
    detail: str,
    ctx: Any,
    sample: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return store.record_gap(
            app_id=app_id,
            project_id=project_id,
            target_label=target,
            target_state_id=target_state,
            from_state_id=from_state,
            platform=_platform(ctx),
            kind=kind,
            detail=detail,
            sample_ref=sample,
        )
    except Exception as exc:  # noqa: BLE001
        SLog.w(TAG, f"record gap failed: {type(exc).__name__}: {exc}")
        return {"gap_id": "", "kind": kind, "detail": detail, "status": "open"}


def plan_entry(
    *,
    app_id: str,
    project_id: str,
    ctx: Any,
    target: str = "",
    instruction: str = "",
    expected: str = "",
) -> dict[str, Any]:
    """选出当前页最合适的入口；不执行设备动作。"""
    target = str(target or "").strip() or _target_from_step(instruction, expected)
    from_state = _current_state(ctx)
    nodes = _nodes(ctx)
    current_fp = page_fingerprint(nodes, platform=_platform(ctx)) if nodes else {}
    if not target:
        return {
            "ok": False,
            "kind": "no_target",
            "target": "",
            "summary": "当前步骤没有写明目标页；入口档案不猜目标。",
            "gap": None,
        }
    try:
        rows = store.candidates(
            app_id,
            target=target,
            from_state_id=from_state,
            platform=_platform(ctx),
            fingerprint=current_fp,
        )
    except Exception as exc:  # noqa: BLE001
        SLog.w(TAG, f"load entries failed: {type(exc).__name__}: {exc}")
        rows = []
    if not rows:
        gap = _gap(
            app_id=app_id,
            project_id=project_id,
            target=target,
            from_state=from_state,
            kind="no_entry",
            detail="用例提到了目标页，但入口档案里没有可点击的入口；运行时不得盲点。",
            ctx=ctx,
            sample={"instruction": instruction[:500], "expected": expected[:500]},
        )
        return {
            "ok": False,
            "kind": "no_entry",
            "target": target,
            "from_state": from_state,
            "gap": gap,
            "summary": (
                f"目标「{target}」没有入口档案：不要盲点页面。"
                "只允许返回/等待/补采集，或先把用例里的入口写清楚。"
            ),
        }

    skipped: list[dict[str, Any]] = []
    for entry in rows:
        if not _selector_values(entry):
            skipped.append({"entry_id": entry.get("entry_id"), "reason": "trigger_empty"})
            continue
        arrival = _is_arrived(entry, ctx, nodes, current_fp)
        if arrival.get("arrived"):
            try:
                store.record_result(str(entry.get("entry_id") or ""), success=True, detail="arrived")
                store.resolve_gaps(
                    app_id,
                    target_label=str(entry.get("target_label") or ""),
                    target_state_id=str(entry.get("target_state_id") or ""),
                )
            except Exception:  # noqa: BLE001
                pass
            return {
                "ok": True,
                "kind": "arrived",
                "entry": entry,
                "target": target,
                "from_state": from_state,
                "arrival": arrival,
                "summary": f"已在目标页「{entry.get('target_label') or target}」，不重复点击入口。",
            }
        node, score = find_trigger(nodes, entry)
        if node is not None:
            selector = _selector(entry)
            trigger_text = str(
                selector.get("content_desc")
                or selector.get("text")
                or (entry.get("trigger") or {}).get("text")
                or ""
            ).strip()
            return {
                "ok": True,
                "kind": "tap",
                "entry": entry,
                "target": target,
                "from_state": from_state,
                "trigger_node": node,
                "trigger_score": score,
                "trigger_text": trigger_text,
                "summary": (
                    f"入口档案：{entry.get('from_state_id') or '任意页'} → "
                    f"「{entry.get('target_label') or target}」，点击「{trigger_text}」。"
                    if entry.get("from_state_id")
                    else f"入口档案：点击「{trigger_text}」进入「{entry.get('target_label') or target}」。"
                ),
            }
        skipped.append({
            "entry_id": entry.get("entry_id"),
            "reason": "trigger_not_visible",
            "trigger": str((entry.get("trigger") or {}).get("text") or ""),
        })

    top = rows[0] if rows else {}
    trigger = str((top.get("trigger") or {}).get("text") or "")
    detail = (
        f"入口「{trigger or top.get('target_label') or target}」不在当前层级。"
        if trigger else "入口档案缺少可定位的入口文案。"
    )
    gap = _gap(
        app_id=app_id,
        project_id=project_id,
        target=target,
        target_state=str(top.get("target_state_id") or ""),
        from_state=from_state,
        kind="trigger_not_visible",
        detail=detail + " 不得换一个相似文案盲点。",
        ctx=ctx,
        sample={"instruction": instruction[:500], "expected": expected[:500], "skipped": skipped[:5]},
    )
    return {
        "ok": False,
        "kind": "trigger_not_visible",
        "target": target,
        "from_state": from_state,
        "entry": top,
        "gap": gap,
        "skipped": skipped,
        "summary": detail + " 请返回/滑动到入口所在页，或补一条真实入口；不要盲点。",
    }


def hint_for_plan(plan: dict[str, Any]) -> str:
    """给 prompt 的短提示。强调“只能点哪一个/没有就停”。"""
    if not isinstance(plan, dict) or not plan.get("ok"):
        if not plan or plan.get("kind") == "no_target":
            return ""
        target = str(plan.get("target") or "")
        return (
            f"【页面入口】目标「{target}」没有可用入口。"
            "禁止在页面上盲点或试探；只允许返回/等待，或说明缺口。"
        )
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    expect = entry.get("expect") if isinstance(entry.get("expect"), dict) else {}
    prior = (entry.get("preconditions") or {}).get("prior") if isinstance(entry.get("preconditions"), dict) else {}
    prior_note = str((prior or {}).get("note") or "") if isinstance(prior, dict) else ""
    if plan.get("kind") == "arrived":
        return (
            f"【页面入口】已确认到达「{entry.get('target_label') or plan.get('target')}」"
            f"（{plan.get('arrival', {}).get('by')}）。不要重复点击入口。"
        )
    target = str(entry.get("target_label") or plan.get("target") or "")
    trigger = str(plan.get("trigger_text") or "")
    confidence = float(entry.get("confidence") or 0.0)
    text = (
        f"【页面入口】目标「{target}」。当前页只按入口档案点击「{trigger}」"
        f"（{entry.get('entry_id') or 'entry'}，来源 {entry.get('source_kind') or '-'}，"
        f"置信度 {confidence:.2f}）。点击一次后必须用 DOM/指纹确认到达；"
        "入口不在当前页就返回/等待并说明缺口，不要换相似文案盲点。"
    )
    expected_text = str(expect.get("expected_text") or "").strip()
    if expected_text:
        text += f" 预期：{expected_text[:160]}"
    if prior_note:
        text += f" 顺序提示：{prior_note}"
    return text


def hint_for_step(
    *,
    app_id: str,
    project_id: str,
    ctx: Any,
    instruction: str,
    expected: str = "",
) -> str:
    if not app_id or not (instruction or expected):
        return ""
    try:
        plan = plan_entry(
            app_id=app_id,
            project_id=project_id,
            ctx=ctx,
            instruction=instruction,
            expected=expected,
        )
    except Exception as exc:  # noqa: BLE001
        SLog.w(TAG, f"hint failed: {type(exc).__name__}: {exc}")
        return ""
    return hint_for_plan(plan)


def dispatch_entry(
    *,
    event: Any,
    ctx: Any,
    router: Any,
    t0: float,
    app_id: str,
    project_id: str,
    target: str,
    nav_attempt: dict[str, Any],
) -> Any:
    """在 FSM 缺失/无路时执行一条入口探针；返回 EventResult 或 None。"""
    from mino_nexus.loop.local_executors import _result, _hierarchy_nodes
    from mino_nexus.core.protocol import EventStatus

    plan = plan_entry(app_id=app_id, project_id=project_id, ctx=ctx, target=target)
    nav_attempt.update({
        "entry_plan": plan.get("kind"),
        "entry_target": plan.get("target"),
        "entry_id": (plan.get("entry") or {}).get("entry_id") if isinstance(plan.get("entry"), dict) else "",
        "entry_confidence": (plan.get("entry") or {}).get("confidence") if isinstance(plan.get("entry"), dict) else None,
        "entry_gap_id": (plan.get("gap") or {}).get("gap_id") if isinstance(plan.get("gap"), dict) else "",
        "entry_arrived": bool(plan.get("kind") == "arrived"),
    })
    if plan.get("kind") == "arrived":
        nav_attempt["arrived_at_target"] = True
        nav_attempt["plan_ok"] = True
        nav_attempt["plan_error"] = ""
        return _result(
            event,
            status=EventStatus.PASS,
            summary=str(plan.get("summary") or "已到达目标页"),
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={
                "nav_attempt": nav_attempt,
                "correction_hint": "已在目标页；不要重复点击入口，继续当前步骤的检查/操作。",
            },
        )
    if plan.get("kind") != "tap":
        summary = str(plan.get("summary") or "入口缺失")
        return _result(
            event,
            status=EventStatus.DECLINED,
            summary=summary,
            error="entry_gap",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={
                "local_reason": "entry_gap",
                "correction_hint": summary,
                "nav_attempt": nav_attempt,
            },
        )
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    entry_id = str(entry.get("entry_id") or "")
    if entry_id and str(getattr(ctx, "_nav_entry_tapped", "") or "") == entry_id:
        summary = (
            f"入口「{entry.get('target_label') or target}」已经点击过一次，"
            "但当前帧仍未确认到达。不得重复点击，按未到达缺口处理。"
        )
        nav_attempt["plan_error"] = "entry_arrival_unverified"
        try:
            store.record_result(entry_id, success=False, detail="arrival_unverified")
        except Exception:  # noqa: BLE001
            pass
        return _result(
            event,
            status=EventStatus.DECLINED,
            summary=summary,
            error="entry_gap",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={"local_reason": "entry_gap", "correction_hint": summary, "nav_attempt": nav_attempt},
        )
    node = plan.get("trigger_node") if isinstance(plan.get("trigger_node"), dict) else {}
    selector = _selector(entry)
    text = str(plan.get("trigger_text") or "").strip()
    params: dict[str, Any] = {"selector_text": text, "text": text}
    rid = str(selector.get("resource_id") or "").strip()
    desc = str(selector.get("content_desc") or "").strip()
    if rid:
        params["resource_id"] = rid
    if desc and desc != text:
        params["content_desc"] = desc
    nodes = _nodes(ctx) or _hierarchy_nodes(ctx)
    if isinstance(nodes, list) and nodes:
        from mino_nexus.loop.tap_enrich import enrich_tap_params
        from mino_nexus.contract.coords import lift_selector_target

        params = enrich_tap_params(params, nodes, hint=text)
        lift_selector_target(params)
    if not node and not params.get("x") and not params.get("y") and not params.get("point"):
        summary = str(plan.get("summary") or "入口没有可点击坐标")
        return _result(
            event,
            status=EventStatus.DECLINED,
            summary=summary,
            error="entry_trigger_unlocatable",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={"local_reason": "entry_gap", "correction_hint": summary, "nav_attempt": nav_attempt},
        )
    from mino_nexus.core.schemas import PlanEvent
    from mino_nexus.loop.web.web_env import agent_step_idx

    exec_event = PlanEvent(
        seq=int(getattr(event, "seq", 0) or 0),
        capability_id="tap_element",
        event_kind="tap_element",
        params=params,
        ai_reasoning=str(plan.get("summary") or "entry_probe"),
        label=f"Entry→{text}",
    )
    scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
    case_seq = int(getattr(ctx, "case_seq", 0) or 0)
    nav_attempt["exec_cap"] = "tap_element"
    nav_attempt["entry_trigger"] = text
    if router is None:
        return _result(
            event,
            status=EventStatus.PASS,
            summary=str(plan.get("summary") or "未连接设备，仅返回入口规划"),
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={"nav_attempt": nav_attempt},
        )
    exec_result = router.dispatch(
        exec_event,
        run_id=scout_run_id,
        step_idx=agent_step_idx(case_seq, int(getattr(event, "seq", 0) or 0)),
        ctx=ctx,
    )
    exec_st = exec_result.status.value if hasattr(exec_result.status, "value") else str(exec_result.status)
    nav_attempt["tap_status"] = exec_st
    if exec_st in ("pass", "done"):
        if entry_id:
            setattr(ctx, "_nav_entry_tapped", entry_id)
        return _result(
            event,
            status=EventStatus.PASS,
            summary=f"{plan.get('summary')}；已点击，等待下一帧确认到达",
            executor="internal",
            elapsed_ms=int((time.time() - t0) * 1000),
            raw_response={
                "nav_attempt": nav_attempt,
                "correction_hint": "入口已点击；下一步必须观察并确认目标页 DOM/指纹，不得把点击成功当成到达。",
            },
        )
    try:
        store.record_result(str(entry.get("entry_id") or ""), success=False, detail=exec_st)
    except Exception:  # noqa: BLE001
        pass
    return _result(
        event,
        status=EventStatus.DECLINED,
        summary=f"入口点击失败：{exec_result.summary or exec_result.error or exec_st}",
        error="entry_tap_failed",
        executor="internal",
        elapsed_ms=int((time.time() - t0) * 1000),
        raw_response={"local_reason": "entry_gap", "correction_hint": str(exec_result.summary or ""), "nav_attempt": nav_attempt},
    )
