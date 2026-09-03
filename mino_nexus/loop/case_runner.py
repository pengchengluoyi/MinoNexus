"""启动 AI-led 回归：立刻落 run_id，后台看图执行。"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Optional

from mino_nexus import app_automation as aas
from mino_nexus import project_store as ps
from mino_nexus import run_store
from mino_nexus import ui_devices
from mino_nexus.ai.llm_client import resolve_regression_provider
from mino_nexus.log import SLog
from mino_nexus.loop.agent_stream import emit_testing_task
from mino_nexus.loop.persist_run_finish import persist_run_finish
from mino_nexus.runtime.run_context import device_platform_kind

TAG = "CaseRunner"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_sns(sn: str = "", sns: Optional[list[str]] = None) -> list[str]:
    raw: list[str] = []
    if sn:
        raw.append(str(sn).strip())
    for item in sns or []:
        raw.append(str(item or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _instruction_case(instruction: str) -> dict[str, Any]:
    text = str(instruction or "").strip()
    cid = f"chat-{run_store.new_run_id()[3:]}"
    return {
        "case_id": cid,
        "name": (text[:80] or cid),
        "precondition": "",
        "steps": [text] if text else [],
        "expected": [],
        "steps_raw": text,
        "expected_raw": "",
        "source": "copilot",
    }


def _pick_online_sns() -> list[str]:
    return [str(d.get("sn") or "") for d in ui_devices.ui_devices() if d.get("status") == "online" and d.get("sn")]


def _platform_of(sn: str, fallback: str = "android") -> str:
    for d in ui_devices.ui_devices():
        if d.get("sn") == sn:
            return device_platform_kind(str(d.get("type") or ""), d.get("channels"), sn=sn)
    return fallback


def run_cases(
    app: dict[str, Any],
    *,
    sn: str = "",
    sns: Optional[list[str]] = None,
    coverage: str = "",
    platform: str = "android",
    case_ids: Optional[list[str]] = None,
    start_index: int = 0,
    async_exec: bool = True,
    run_type: str = "manual",
    requirement_id: str = "",
    release_id: str = "",
    slot_id: str = "",
    instruction: str = "",
    provider_id: str = "",
) -> dict[str, Any]:
    device_sns = _normalize_sns(sn, sns)
    cov = str(coverage or "").strip().lower()
    if cov not in ("", "once", "per_device"):
        raise ValueError("coverage 必须是 once 或 per_device")
    if device_sns and (not cov or len(device_sns) == 1):
        cov = "once"
    cov = cov or "once"

    all_cases = aas.list_app_cases(app)
    instruction = str(instruction or "").strip()
    if instruction:
        cases = [_instruction_case(instruction)]
        if not str(run_type or "").strip() or str(run_type).lower() == "manual":
            run_type = "copilot"
    elif case_ids:
        by_id = {str(c.get("case_id")): c for c in all_cases if c.get("case_id")}
        cases = [by_id[str(cid)] for cid in case_ids if str(cid) in by_id]
        missing = [cid for cid in case_ids if str(cid) not in by_id]
        if missing:
            SLog.w(TAG, f"missing case_ids: {missing[:5]} (total {len(missing)})")
    else:
        cases = list(all_cases)
    if start_index > 0:
        cases = cases[start_index:]
    if not cases:
        raise ValueError("没有可执行的用例草稿。请先在流程里生成用例，或指定 case_ids。")

    for dsn in device_sns:
        busy = run_store.busy_task_for_sn(dsn)
        if busy:
            raise DeviceBusy(dsn, busy)

    if not device_sns:
        device_sns = _pick_online_sns()[:1]

    platforms_by_sn = {dsn: _platform_of(dsn, platform) for dsn in device_sns}
    task_platform = platform
    if device_sns and len(set(platforms_by_sn.values())) == 1:
        task_platform = platforms_by_sn[device_sns[0]]
    elif len(set(platforms_by_sn.values())) > 1:
        task_platform = "mixed"

    cfg = aas.get_automation_config(app)
    package = aas.package_for_app(app, platform=task_platform if task_platform in ("android", "ios", "web") else "android")
    playbook = aas.get_playbook(app)
    run_id = run_store.new_run_id()
    seeded = [
        run_store.seed_case(run_id, c, i, sn=device_sns[0] if device_sns and cov != "per_device" else "", coverage=cov)
        for i, c in enumerate(cases)
    ]
    if cov == "per_device" and len(device_sns) > 1:
        seeded = [
            run_store.seed_case(run_id, c, i, sn=dsn, coverage=cov)
            for dsn in device_sns
            for i, c in enumerate(cases)
        ]

    provider, gate = resolve_regression_provider(str(provider_id or "").strip() or None)
    doc: dict[str, Any] = {
        "run_id": run_id,
        "task_id": run_id,
        "engine": "agent",
        "run_type": run_type or "manual",
        "app_id": str(app.get("id") or ""),
        "app_name": str(app.get("name") or ""),
        "sn": device_sns[0] if device_sns else "",
        "sns": device_sns,
        "coverage": cov,
        "platform": task_platform,
        "platforms_by_sn": platforms_by_sn,
        "env_profile": cfg.get("env_profile") or "test",
        "package": package,
        "playbook": playbook if isinstance(playbook, dict) else {},
        "requirement_id": str(requirement_id or "").strip(),
        "release_id": str(release_id or "").strip(),
        "slot_id": str(slot_id or "").strip(),
        "provider_id": (provider or {}).get("id") or gate.get("provider_id") or "",
        "provider_name": (provider or {}).get("name") or "",
        "model_name": (provider or {}).get("model") or "",
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "cases": seeded,
        "error": "",
        "total": len(seeded),
        "completed": 0,
        "passed": 0,
        "failed": 0,
    }
    run_store.put(doc)
    persist_run_finish(doc)
    emit_testing_task({"event": "task_created", "run_id": run_id, "task_id": run_id, "app_id": doc["app_id"]})

    fail_reason = ""
    if not device_sns:
        fail_reason = "没有在线设备。请在 Studio 安装并连接 Scout，再选一台设备下发。"
    elif not provider:
        fail_reason = gate.get("reason") or "未配置用例执行大模型（Studio → 密钥 → 大模型 Key → 可用 + 用例）"

    if fail_reason:
        for case in doc["cases"]:
            case["status"] = "fail"
            case["summary"] = fail_reason
            case["error"] = fail_reason
        doc["error"] = fail_reason
        run_store.put(doc)
        finished = run_store.finish(run_id, status="failed", error=fail_reason)
        persist_run_finish(finished)
        return run_store.to_task_json(finished)

    worker = threading.Thread(
        target=_run_in_background,
        kwargs={"run_id": run_id, "package": package, "playbook": playbook, "provider_id": doc["provider_id"]},
        daemon=True,
        name=f"case-run-{run_id}",
    )
    worker.start()
    if not async_exec:
        worker.join(timeout=600)
        latest = run_store.get(run_id) or doc
        return run_store.to_task_json(latest)
    return run_store.to_task_json(doc)


def _run_in_background(*, run_id: str, package: str, playbook: dict, provider_id: str) -> None:
    from mino_nexus.loop.agent_loop import run_case

    doc = run_store.get(run_id)
    if not doc:
        return
    try:
        for case in list(doc.get("cases") or []):
            if run_store.cancel_requested(run_id):
                break
            cid = str(case.get("case_id") or "")
            sn = str(case.get("sn") or doc.get("sn") or "")
            run_store.patch_case(run_id, cid, status="running")
            emit_testing_task({"event": "case_running", "run_id": run_id, "case_id": cid, "sn": sn})
            result = run_case(
                run_id=run_id,
                case=case,
                sn=sn,
                app_id=str(doc.get("app_id") or ""),
                package=package,
                provider_id=provider_id,
                playbook=playbook if isinstance(playbook, dict) else {},
                cancel_check=lambda: run_store.cancel_requested(run_id),
            )
            run_store.patch_case(
                run_id, cid,
                status=result.get("status") or "fail",
                summary=result.get("summary") or "",
                error=result.get("summary") or "",
                elapsed_ms=result.get("elapsed_ms") or 0,
                steps=result.get("steps") or [],
            )
            emit_testing_task({
                "event": "case_finished", "run_id": run_id, "case_id": cid,
                "status": result.get("status"),
            })
        latest = run_store.get(run_id) or doc
        if run_store.cancel_requested(run_id):
            finished = run_store.finish(run_id, status="cancelled", error="已取消")
        else:
            failed = int(latest.get("failed") or 0) + int(latest.get("blocked") or 0)
            status = "failed" if failed else "done"
            finished = run_store.finish(run_id, status=status, error=latest.get("error") or "")
        persist_run_finish(finished)
        emit_testing_task({"event": "task_finished", "run_id": run_id, "status": finished.get("status")})
    except Exception as exc:
        SLog.e(TAG, f"run {run_id} crashed: {exc}")
        try:
            finished = run_store.finish(run_id, status="failed", error=str(exc)[:240])
            persist_run_finish(finished)
        except Exception:
            pass


def retry_failed(task_id: str, *, sn: str = "") -> dict[str, Any]:
    doc = run_store.get(task_id)
    if not doc:
        return {"ok": False, "code": 404, "reason": "任务不存在"}
    failed_ids = [
        str(c.get("case_id"))
        for c in (doc.get("cases") or [])
        if str(c.get("status") or "") in ("fail", "blocked", "declined")
    ]
    if not failed_ids:
        return {"ok": False, "code": 400, "reason": "没有失败用例可重跑"}
    app = ps.require_app(str(doc.get("app_id") or ""))
    snapshot = run_cases(
        app,
        sn=sn or str(doc.get("sn") or ""),
        sns=[sn] if sn else list(doc.get("sns") or []),
        coverage=str(doc.get("coverage") or "once"),
        platform=str(doc.get("platform") or "android"),
        case_ids=failed_ids,
        run_type=str(doc.get("run_type") or "manual"),
        requirement_id=str(doc.get("requirement_id") or ""),
        release_id=str(doc.get("release_id") or ""),
        provider_id=str(doc.get("provider_id") or ""),
    )
    return {"ok": True, "code": 200, "case_ids": failed_ids, "data": snapshot}


class DeviceBusy(Exception):
    def __init__(self, sn: str, busy_task_id: str):
        super().__init__(f"device busy: {sn}")
        self.sn = sn
        self.busy_task_id = busy_task_id
