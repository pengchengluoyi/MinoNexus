"""Session Replay / Fork 执行入口。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.loop.session_harness import build_fork_plan, build_replay_plan, fork_state_from_plan
from mino_nexus.services import app_automation as aas
from mino_nexus.services import project_store as ps
from mino_nexus.services import run_store
from mino_nexus.services import session_store

TAG = "SessionReplay"


def _case_from_run(run_id: str, case_id: str) -> dict[str, Any] | None:
    doc = run_store.get(run_id)
    if not doc:
        return None
    for case in doc.get("cases") or []:
        if str(case.get("case_id") or "") == str(case_id):
            return dict(case)
    return None


def replay_session(session_id: str, *, sn: str = "") -> dict[str, Any]:
    """整案 Replay：同 case 重新 run_case，session/start 链 parent。"""
    plan = build_replay_plan(session_id)
    if not plan:
        return {"ok": False, "code": 404, "reason": "session 不存在"}
    meta = session_store.get_meta(session_id) or {}
    app_id = str(plan.get("app_id") or meta.get("app_id") or "")
    case_id = str(plan.get("case_id") or "")
    run_id = str(plan.get("run_id") or "")
    if not app_id or not case_id:
        return {"ok": False, "code": 400, "reason": "session 缺少 app_id / case_id"}

    try:
        app = ps.require_app(app_id)
    except KeyError:
        return {"ok": False, "code": 404, "reason": "应用不存在"}

    src = _case_from_run(run_id, case_id)
    doc = run_store.get(run_id) or {}
    device_sn = sn or str((src or {}).get("sn") or doc.get("sn") or "")
    if not device_sn:
        return {"ok": False, "code": 400, "reason": "无法推断设备 sn，请传入 sn"}

    from mino_nexus.loop.agent_loop import run_case

    cfg = aas.get_automation_config(app)
    package = str(doc.get("package") or "")
    playbook = doc.get("playbook") if isinstance(doc.get("playbook"), dict) else {}
    provider_id = str(doc.get("provider_id") or "")
    new_run_id = run_store.new_run_id()
    case = dict(src or {"case_id": case_id})
    case["report_run_id"] = run_store.report_run_id(new_run_id, case_id, sn=device_sn)

    SLog.i(TAG, f"replay {session_id} → {case.get('report_run_id')}")
    result = run_case(
        run_id=new_run_id,
        case=case,
        sn=device_sn,
        app_id=app_id,
        app_name=str(app.get("name") or ""),
        package=package,
        provider_id=provider_id,
        playbook=playbook,
        playwright_headless=bool(doc.get("playwright_headless", True)),
        parent_session_id=session_id,
    )
    return {
        "ok": True,
        "code": 200,
        "mode": "replay",
        "parent_session_id": session_id,
        "session_id": case.get("report_run_id"),
        "run_id": new_run_id,
        "plan": plan,
        "result": result,
    }


def fork_session(
    session_id: str,
    *,
    from_turn: int,
    sn: str = "",
    provider_id: str = "",
) -> dict[str, Any]:
    """Fork：从 turn N 切开继续跑，写入 session/fork。"""
    plan = build_fork_plan(
        session_id,
        from_turn=from_turn,
        overrides={"provider_id": provider_id} if provider_id else None,
    )
    if not plan:
        return {"ok": False, "code": 404, "reason": "无法构建 fork 计划（session 或 turn 不存在）"}

    meta = session_store.get_meta(session_id) or {}
    app_id = str(plan.get("app_id") or "")
    case_id = str(plan.get("case_id") or "")
    run_id = str(plan.get("run_id") or "")

    try:
        app = ps.require_app(app_id)
    except KeyError:
        return {"ok": False, "code": 404, "reason": "应用不存在"}

    src = _case_from_run(run_id, case_id)
    doc = run_store.get(run_id) or {}
    device_sn = sn or str((src or {}).get("sn") or doc.get("sn") or "")
    if not device_sn:
        return {"ok": False, "code": 400, "reason": "无法推断设备 sn"}

    from mino_nexus.loop.agent_loop import run_case

    package = str(doc.get("package") or "")
    playbook = doc.get("playbook") if isinstance(doc.get("playbook"), dict) else {}
    prov = provider_id or str((plan.get("overrides") or {}).get("provider_id") or doc.get("provider_id") or "")
    fork_state = fork_state_from_plan(plan)
    new_run_id = run_store.new_run_id()
    case = dict(src or {"case_id": case_id})
    case["report_run_id"] = run_store.report_run_id(new_run_id, case_id, sn=device_sn)

    SLog.i(TAG, f"fork {session_id}@turn{from_turn} → {case.get('report_run_id')}")
    result = run_case(
        run_id=new_run_id,
        case=case,
        sn=device_sn,
        app_id=app_id,
        app_name=str(app.get("name") or ""),
        package=package,
        provider_id=prov,
        playbook=playbook,
        playwright_headless=bool(doc.get("playwright_headless", True)),
        parent_session_id=session_id,
        fork_state=fork_state,
    )
    return {
        "ok": True,
        "code": 200,
        "mode": "fork",
        "parent_session_id": session_id,
        "fork_from_turn": from_turn,
        "session_id": case.get("report_run_id"),
        "run_id": new_run_id,
        "plan": plan,
        "result": result,
    }
