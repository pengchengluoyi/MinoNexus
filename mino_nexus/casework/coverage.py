"""覆盖度新口径：业务意图闭合率（主分母）+ 事件×渠道可达性。

旧口径把「事件能不能被点到」当覆盖率，导致 FSM/逻辑块和业务意图被漏掉。
新口径（docs/架构/V8/覆盖度与架构优化-20261024.md §2）：

1. **主分母是需求点（业务意图）**：一个点至少有一条用例在最近一次跑批中通过，
   才算闭合；能力缺口/守卫误拦不改变分母，只改变归因。
2. **事件×渠道可达性**只回答「这个事件在这个渠道上有没有实现」，不是覆盖率本身；
   渠道包含 android/ios/web/mac/win/linux 与 api/rpc/websocket/db。
3. **FSM / 逻辑块**以业务意图粒度进入覆盖，不做随机事件组合。

纯读：不触发跑批、不写库。
"""
from __future__ import annotations

from typing import Any, Iterable

SCHEMA = "mino.coverage.v1"
UI_CHANNELS = ("android", "ios", "web", "mac", "win", "linux")
SERVER_CHANNELS = ("api", "rpc", "websocket", "db")
_PASS = {"pass", "passed", "ok", "success", "succeeded", "completed"}


def _status_of(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("status") or row.get("outcome") or row.get("overall_status") or "").strip().lower()


def _app_cases(project_id: str, app_id: str) -> list[dict[str, Any]]:
    from mino_nexus.casework import case_store

    out: list[dict[str, Any]] = []
    for case in case_store.list_cases(str(project_id or "")):
        if isinstance(case, dict) and str(case.get("deleted") or "") in ("", "0"):
            got = str(case.get("app_id") or "").strip()
            if not app_id or not got or got == str(app_id):
                out.append(case)
    return out


def _case_intents(case: dict[str, Any]) -> list[str]:
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    summary = meta.get("intent_summary") if isinstance(meta.get("intent_summary"), dict) else {}
    ids = [str(x) for x in (summary.get("intents") or []) if str(x)]
    if ids:
        return ids
    out: list[str] = []
    for bundle in (meta.get("step_program_keys") or {}).values():
        if not isinstance(bundle, dict):
            continue
        for node in (bundle.get("intent_graph") or {}).get("nodes") or []:
            iid = str(node.get("id") or "")
            if iid and iid not in out:
                out.append(iid)
    return out


def _last_results(app_id: str, case_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not case_ids:
        return {}
    try:
        from mino_nexus.casework import run_store

        return run_store.last_case_results(str(app_id or ""), case_ids) or {}
    except Exception:  # noqa: BLE001 - 未登录/无历史时覆盖度仍要能看
        return {}


def intent_coverage(project_id: str, app_id: str = "") -> dict[str, Any]:
    """业务意图闭合率：分母是需求点，分子是最近一次跑批通过的用例覆盖到的点。"""
    cases = _app_cases(project_id, app_id)
    results = _last_results(app_id, [str(c.get("case_id") or "") for c in cases])
    points: dict[str, dict[str, Any]] = {}
    unidentified: list[dict[str, Any]] = []
    intent_points: dict[str, dict[str, Any]] = {}
    for case in cases:
        cid = str(case.get("case_id") or "")
        last = results.get(cid) or {}
        passed = _status_of(last) in _PASS
        from mino_nexus.casework.requirement_points import requirement_point_ids

        ids, pid_source = requirement_point_ids(case)
        if pid_source == "fallback":
            unidentified.append({"unit_id": ids[0] if ids else ("case:" + cid),
                                 "case_id": cid, "status": _status_of(last)})
        for pid in ids:
            row = points.setdefault(pid, {"point_id": pid, "cases": [], "covered": False,
                                          "attribution_class": ""})
            row["cases"].append(cid)
            if passed:
                row["covered"] = True
            elif not row["covered"]:
                from mino_nexus.contract.attribution import classify_reason
                reason = str(last.get("reason") or "")
                if reason:
                    row["attribution_class"] = classify_reason(reason, layer="execution")["attribution_class"]
                else:
                    row["attribution_class"] = "B" if _status_of(last) in ("", "unverified") else "C"
        for iid in _case_intents(case):
            row = intent_points.setdefault(iid, {"intent_id": iid, "cases": [], "covered": False})
            row["cases"].append(cid)
            if passed:
                row["covered"] = True
    total = len(points)
    covered = sum(1 for row in points.values() if row["covered"])
    i_total = len(intent_points)
    i_covered = sum(1 for row in intent_points.values() if row["covered"])
    return {
        "schema": SCHEMA,
        "scope": {"project_id": str(project_id or ""), "app_id": str(app_id or ""),
                  "cases": len(cases), "runs": "latest_per_case"},
        "business_intent": {
            "label": "业务意图闭合率",
            "denominator": "requirement_points",
            "total": total,
            "covered": covered,
            "closure_rate": round(covered / total, 4) if total else None,
            "uncovered": [row for row in points.values() if not row["covered"]][:200],
            "notes": "分母是需求点；能力缺口/守卫误拦只做归因，不改变分母。",
        },
        "intent": {
            "label": "意图闭合率",
            "total": i_total,
            "covered": i_covered,
            "closure_rate": round(i_covered / i_total, 4) if i_total else None,
            "uncovered": [row for row in intent_points.values() if not row["covered"]][:200],
        },
        "unidentified_cases": unidentified[:200],
        "axes": ["business_intent_closure", "event_channel_reachability"],
    }


def _iter_events(cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for case in cases:
        meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
        for bundle in (meta.get("step_program_keys") or {}).values():
            if not isinstance(bundle, dict):
                continue
            for phase, key, field in (
                ("do", "do_program_plan", "steps"),
                ("check", "check_program_plan", "checkpoints"),
            ):
                plan = bundle.get(key) if isinstance(bundle.get(key), dict) else {}
                for step in plan.get(field) or []:
                    if not isinstance(step, dict):
                        continue
                    events = [step]
                    bid = str(step.get("block_id") or "").strip()
                    if bid and not (step.get("hook_cap") or step.get("cap")):
                        try:
                            from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps
                            events = list(resolve_effective_steps(
                                app_id=str(case.get("app_id") or case.get("project_id") or ""),
                                block_id=bid)) or [{"block_id": bid}]
                        except Exception:  # noqa: BLE001 - 覆盖统计不因块解析失败整体报错
                            events = [{"block_id": bid}]
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        eid = str(event.get("hook_cap") or event.get("device_cap")
                                  or event.get("cap") or event.get("block_id") or "")
                        if not eid:
                            continue
                        row = seen.setdefault(eid, {"event_id": eid, "phases": set(), "cases": set(),
                                                    "intents": set()})
                        row["phases"].add(phase)
                        row["cases"].add(str(case.get("case_id") or ""))
                        if step.get("intent_id") or event.get("intent_id"):
                            row["intents"].add(str(step.get("intent_id") or event["intent_id"]))
    out: list[dict[str, Any]] = []
    for row in sorted(seen.values(), key=lambda x: x["event_id"]):
        out.append({**row, "phases": sorted(row["phases"]), "cases": sorted(row["cases"]),
                    "intents": sorted(row["intents"]), "case_count": len(row["cases"])})
    return out


def _ui_channels() -> list[str]:
    return list(UI_CHANNELS) + list(SERVER_CHANNELS)


def event_channel_matrix(project_id: str, app_id: str = "") -> dict[str, Any]:
    """事件×渠道可达性：静态判断（目录 + 平台声明），不伪装成真机验证。"""
    from mino_nexus.catalog.registry import get_capability

    cases = _app_cases(project_id, app_id)
    events = _iter_events(cases)
    channels = _ui_channels()
    rows: list[dict[str, Any]] = []
    totals = {"events": len(events), "reachable": 0, "blocked": 0, "unknown": 0}
    for event in events:
        cap = get_capability(event["event_id"])
        declared = {str(x) for x in (getattr(cap, "platforms", None) or [])} if cap is not None else set()
        impls = list(getattr(cap, "implementations", None) or []) if cap is not None else []
        for channel in channels:
            row = {"event_id": event["event_id"], "channel": channel, "status": "unknown",
                   "reason_code": "", "attribution_class": "", "layer": "dispatch",
                   "case_count": event["case_count"], "intents": event["intents"]}
            if channel in SERVER_CHANNELS:
                row["status"] = "not_applicable"
                row["reason_code"] = "not_ui_channel"
                row["detail"] = "非 UI 渠道：本轮只做可达性占位，不计入点击覆盖。"
            elif cap is None:
                row["status"] = "blocked"
                row["reason_code"] = "catalog_capability_missing"
                row["attribution_class"] = "B"
            elif channel not in declared:
                row["status"] = "blocked"
                row["reason_code"] = "platform_unsupported"
                row["attribution_class"] = "A"
            elif not impls:
                row["status"] = "blocked"
                row["reason_code"] = "unavailable_cap"
                row["attribution_class"] = "B"
            else:
                row["status"] = "reachable"
                row["reason_code"] = ""
                row["detail"] = "目录声明了该平台且有实现；真机可达性以 session 为准。"
            if row["status"] == "reachable":
                totals["reachable"] += 1
            elif row["status"] == "blocked":
                totals["blocked"] += 1
            else:
                totals["unknown"] += 1
            rows.append(row)
    return {
        "schema": SCHEMA,
        "scope": {"project_id": str(project_id or ""), "app_id": str(app_id or ""),
                  "cases": len(cases), "events": len(events)},
        "channels": channels,
        "ui_channels": list(UI_CHANNELS),
        "server_channels": list(SERVER_CHANNELS),
        "totals": totals,
        "rows": rows[:2000],
        "truncated": len(rows) > 2000,
        "notes": [
            "事件×渠道回答「这个事件在这个渠道有没有实现」，不是业务覆盖率。",
            "statics：目录平台声明 + implementations；没有实现就是 B，没有声明就是 A。",
        ],
    }


def summary(project_id: str, app_id: str = "") -> dict[str, Any]:
    return {"schema": SCHEMA, "intent": intent_coverage(project_id, app_id),
            "events": event_channel_matrix(project_id, app_id)}


def describe() -> dict[str, Any]:
    return {"schema": SCHEMA, "primary_denominator": "business_intent",
            "axes": ["business_intent_closure", "event_channel_reachability"],
            "ui_channels": list(UI_CHANNELS), "server_channels": list(SERVER_CHANNELS),
            "rules": ["ui_coverable 只影响统计，不影响派发",
                      "FSM/逻辑块以业务意图粒度进入覆盖",
                      "不做随机事件组合"]}


__all__ = [
    "SCHEMA",
    "SERVER_CHANNELS",
    "UI_CHANNELS",
    "describe",
    "event_channel_matrix",
    "intent_coverage",
    "summary",
]
