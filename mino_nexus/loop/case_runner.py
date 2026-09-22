"""启动 AI-led 回归：立刻落 run_id，后台看图执行。"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Optional

from mino_nexus.services import app_automation as aas
from mino_nexus.services import project_store as ps
from mino_nexus.services import run_store
from mino_nexus.services import ui_devices
from mino_nexus.ai.llm_client import resolve_regression_provider
from mino_nexus.core.log import SLog
from mino_nexus.loop.agent_stream import emit_testing_task
from mino_nexus.loop.web_env import release_devices_for_run
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


def _sn_for_case_index(
    index: int,
    device_sns: list[str],
    *,
    coverage: str,
) -> str:
    """once + 多机：轮询分片；单机或 per_device 外层已展开。"""
    if not device_sns:
        return ""
    cov = str(coverage or "once").strip().lower()
    if cov == "once" and len(device_sns) > 1:
        return device_sns[index % len(device_sns)]
    return device_sns[0]


def _worker_sns_for_cases(cases: list[dict[str, Any]], doc: dict[str, Any]) -> list[str]:
    """本 run 上实际有 case 绑定的设备列表（保持 sns 顺序）。"""
    bound = {str(c.get("sn") or "").strip() for c in cases if str(c.get("sn") or "").strip()}
    if not bound:
        head = str(doc.get("sn") or "").strip()
        return [head] if head else []
    order: list[str] = []
    for sn in list(doc.get("sns") or []):
        s = str(sn or "").strip()
        if s and s in bound and s not in order:
            order.append(s)
    for s in sorted(bound):
        if s not in order:
            order.append(s)
    return order


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
    playwright_headless: bool = True,
    env_profile: str = "",
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
    project_id = str(app.get("project_id") or "")
    env_doc: dict[str, Any] = {}
    if project_id:
        try:
            env_doc = ps.project_env(project_id)
        except KeyError:
            env_doc = {}
    from mino_nexus.services.project_env import resolve_run_env_profile

    plat_for_pkg = task_platform if task_platform in ("android", "ios", "web") else "android"
    resolved_env = resolve_run_env_profile(
        env_doc,
        requested=str(env_profile or "").strip(),
        automation_default=str(cfg.get("env_profile") or "").strip(),
    )
    package = aas.package_for_app(app, env_profile=resolved_env, platform=plat_for_pkg)
    playbook = aas.get_playbook(app)
    run_id = run_store.new_run_id()
    seeded = [
        run_store.seed_case(
            run_id,
            c,
            i,
            sn=_sn_for_case_index(i, device_sns, coverage=cov) if cov != "per_device" else "",
            coverage=cov,
        )
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
        "env_profile": resolved_env,
        "package": package,
        "playbook": playbook if isinstance(playbook, dict) else {},
        "requirement_id": str(requirement_id or "").strip(),
        "release_id": str(release_id or "").strip(),
        "slot_id": str(slot_id or "").strip(),
        "provider_id": (provider or {}).get("id") or gate.get("provider_id") or "",
        "provider_name": (provider or {}).get("name") or "",
        "model_name": (provider or {}).get("model") or "",
        "playwright_headless": bool(playwright_headless),
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


def _task_still_running(run_id: str) -> bool:
    doc = run_store.get(run_id)
    return run_store.task_is_live(doc)


def _record_case_preflight_fail(
    *,
    run_id: str,
    case: dict[str, Any],
    reason: str,
    app_id: str,
    sn: str,
    provider_id: str = "",
) -> None:
    """开跑前失败（租约/拒跑）也落 session，避免 Studio Session Log 404。"""
    from mino_nexus.loop.session_log import open_session

    cid = str(case.get("case_id") or "")
    stream_id = str(case.get("report_run_id") or "").strip() or run_store.report_run_id(
        run_id, cid, sn=str(case.get("sn") or ""), coverage=str(case.get("coverage") or "")
    )
    summary = str(reason or "开跑前检查未通过")[:240]
    try:
        writer = open_session(
            session_id=stream_id,
            run_id=run_id,
            case_id=cid,
            app_id=str(app_id or ""),
            provider_id=str(provider_id or ""),
            sn=str(sn or ""),
            sop_id="run-case",
            extra={"goal": str(case.get("name") or cid), "preflight": True},
        )
        writer.append("preflight/block", {"reason": summary})
        writer.close(status="fail", summary=summary, step_count=0)
    except Exception as exc:
        SLog.w(TAG, f"preflight session log failed {stream_id}: {exc!r}")


def _run_case_list(
    *,
    run_id: str,
    package: str,
    playbook: dict,
    provider_id: str,
    cases: list[dict[str, Any]],
    doc: dict[str, Any],
) -> None:
    from mino_nexus.loop.agent_loop import run_case
    from mino_nexus.loop.device_run_continuity import (
        continuity_blockers,
        get_sn_state,
        update_after_case,
    )

    env_brief = str(doc.get("env_brief") or "")
    continuity_by_sn: dict = {}
    for case_seq, case in enumerate(cases):
        if not _task_still_running(run_id):
            break
        cid = str(case.get("case_id") or "")
        sn = str(case.get("sn") or doc.get("sn") or "")
        run_store.patch_case(run_id, cid, status="running")
        emit_testing_task({
            "event": "case_running",
            "run_id": run_id,
            "task_id": run_id,
            "case_id": cid,
            "sn": sn,
            "app_id": str(doc.get("app_id") or ""),
        })
        scene = case.get("case_scene") if isinstance(case.get("case_scene"), dict) else None
        if scene is None and isinstance(case.get("scene"), dict):
            scene = case.get("scene")
        from mino_nexus.services import project_store as ps
        from mino_nexus.services.case_resource_claim import (
            ensure_resource_key_on_case,
            preflight_device_app_gap,
        )
        from mino_nexus.services.resource_preflight import run_start_resource_blockers

        app_id = str(doc.get("app_id") or "")
        project_id = ""
        if app_id:
            try:
                project_id = str(ps.require_app(app_id).get("project_id") or "")
            except KeyError:
                project_id = ""
        env_doc = ps.project_env(project_id) if project_id else None
        ensure_resource_key_on_case(
            case,
            env_doc=env_doc,
            env=str(doc.get("env_profile") or "test"),
            package=str(package or ""),
        )
        rk = case.get("resource_key") if isinstance(case.get("resource_key"), dict) else None
        merged_scene = case.get("case_scene") if isinstance(case.get("case_scene"), dict) else scene
        sn_state = get_sn_state(continuity_by_sn, sn)
        cont_block = continuity_blockers(
            sn_state,
            precondition=str(case.get("precondition") or ""),
        )
        if cont_block:
            reason = cont_block[0]
            _record_case_preflight_fail(
                run_id=run_id,
                case=case,
                reason=reason,
                app_id=app_id,
                sn=sn,
                provider_id=str(doc.get("provider_id") or ""),
            )
            run_store.patch_case(run_id, cid, status="fail", summary=reason, error=reason)
            emit_testing_task({
                "event": "case_finished",
                "run_id": run_id,
                "task_id": run_id,
                "case_id": cid,
                "status": "fail",
                "app_id": app_id,
            })
            continue
        blockers = run_start_resource_blockers(
            rk,
            scene=merged_scene,
            precondition=str(case.get("precondition") or ""),
            sn=sn,
            package_id=str(package or ""),
        )
        if blockers:
            reason = blockers[0]
            _record_case_preflight_fail(
                run_id=run_id,
                case=case,
                reason=reason,
                app_id=app_id,
                sn=sn,
                provider_id=str(doc.get("provider_id") or ""),
            )
            run_store.patch_case(run_id, cid, status="fail", summary=reason, error=reason)
            emit_testing_task({
                "event": "case_finished",
                "run_id": run_id,
                "task_id": run_id,
                "case_id": cid,
                "status": "fail",
                "app_id": app_id,
            })
            continue
        gaps = preflight_device_app_gap(rk, sn=sn, package_id=str(package or ""))
        if gaps:
            from mino_nexus.core.log import SLog

            SLog.i("CaseRunner", f"resource preflight case={cid} " + "; ".join(gaps[:3]))
        platform = str(doc.get("platform") or case.get("platform") or "android")
        if isinstance(rk, dict) and rk.get("platform"):
            platform = str(rk.get("platform") or platform)
        from mino_nexus.services.device_resource_lease import (
            acquire_device_lease,
            release_device_lease,
        )

        device_held = False
        try:
            ok_dev, dev_err = acquire_device_lease(
                sn=sn,
                package_id=str(package or ""),
                run_id=run_id,
                case_id=cid,
                project_id=project_id,
                platform=platform,
            )
            if not ok_dev and dev_err:
                reason = f"设备租约：{dev_err}"
                _record_case_preflight_fail(
                    run_id=run_id,
                    case=case,
                    reason=reason,
                    app_id=app_id,
                    sn=sn,
                    provider_id=str(doc.get("provider_id") or ""),
                )
                run_store.patch_case(run_id, cid, status="fail", summary=reason, error=reason)
                emit_testing_task({
                    "event": "case_finished",
                    "run_id": run_id,
                    "task_id": run_id,
                    "case_id": cid,
                    "status": "fail",
                    "app_id": app_id,
                })
                continue
            device_held = True
            from mino_nexus.services.account_lease import ensure_case_account_lease

            ok, lease_err = ensure_case_account_lease(
                run_id,
                app_id=str(doc.get("app_id") or ""),
                env_profile=str(doc.get("env_profile") or "test"),
                platform=str(doc.get("platform") or "android"),
                target_package=str(package or ""),
                case=case if isinstance(case, dict) else {},
                scene=scene,
            )
            if not ok and lease_err:
                reason = f"账号租约：{lease_err}"
                _record_case_preflight_fail(
                    run_id=run_id,
                    case=case,
                    reason=reason,
                    app_id=app_id,
                    sn=sn,
                    provider_id=str(doc.get("provider_id") or ""),
                )
                run_store.patch_case(run_id, cid, status="fail", summary=reason, error=reason)
                emit_testing_task({
                    "event": "case_finished",
                    "run_id": run_id,
                    "task_id": run_id,
                    "case_id": cid,
                    "status": "fail",
                    "app_id": str(doc.get("app_id") or ""),
                })
                continue
            from mino_nexus.services.resource_task_card import build_resource_card

            lease_snap: dict = {}
            try:
                from mino_nexus.services.account_lease import restore_lease_for_run

                class _LeaseCtx:
                    pass

                lc = _LeaseCtx()
                lc.app_id = app_id
                row, _ = restore_lease_for_run(lc, run_id)
                if isinstance(row, dict):
                    lease_snap = {
                        "account_id": str(row.get("id") or ""),
                        "login": str(row.get("login") or row.get("name") or ""),
                    }
            except Exception:
                lease_snap = {}
            resource_card = build_resource_card(
                case=case if isinstance(case, dict) else {},
                resource_key=rk,
                sn=sn,
                package_id=str(package or ""),
                platform=platform,
                project_id=project_id,
                run_id=run_id,
                preflight_gaps=gaps,
                account_lease=lease_snap,
            )
            run_store.patch_case(run_id, cid, resource_card=resource_card)
            emit_testing_task({
                "event": "case_resource",
                "run_id": run_id,
                "task_id": run_id,
                "case_id": cid,
                "app_id": app_id,
                "resource_card": resource_card,
            })
            result = run_case(
                run_id=run_id,
                case=case,
                sn=sn,
                app_id=str(doc.get("app_id") or ""),
                app_name=str(doc.get("app_name") or ""),
                package=package,
                provider_id=provider_id,
                playbook=playbook if isinstance(playbook, dict) else {},
                cancel_check=lambda: run_store.task_cancelled(run_id),
                case_seq=case_seq,
                playwright_headless=bool(doc.get("playwright_headless", True)),
                run_env_brief=env_brief,
            )
            if not _task_still_running(run_id):
                break
            doc = run_store.get(run_id) or doc
            env_brief = str(doc.get("env_brief") or env_brief)
            run_store.patch_case(
                run_id, cid,
                status=result.get("status") or "fail",
                summary=result.get("summary") or "",
                error=result.get("summary") or "",
                elapsed_ms=result.get("elapsed_ms") or 0,
                engine_steps=result.get("steps") or [],
                skill_id=result.get("skill_id") or "",
                view_id=result.get("view_id") or "",
                slots=result.get("slots") if isinstance(result.get("slots"), dict) else {},
            )
            emit_testing_task({
                "event": "case_finished",
                "run_id": run_id,
                "task_id": run_id,
                "case_id": cid,
                "status": result.get("status"),
                "app_id": str(doc.get("app_id") or ""),
            })
            update_after_case(
                sn_state,
                status=str(result.get("status") or "fail"),
                precondition=str(case.get("precondition") or ""),
                case=case if isinstance(case, dict) else {},
                summary=str(result.get("summary") or ""),
            )
        finally:
            if device_held:
                release_device_lease(sn, str(package or ""), run_id)


def _run_in_background(*, run_id: str, package: str, playbook: dict, provider_id: str) -> None:
    doc = run_store.get(run_id)
    if not doc or not run_store.task_is_live(doc):
        return
    try:
        cases = list(doc.get("cases") or [])
        worker_sns = _worker_sns_for_cases(cases, doc)
        list_kwargs = {
            "run_id": run_id,
            "package": package,
            "playbook": playbook,
            "provider_id": provider_id,
            "doc": doc,
        }
        if len(worker_sns) <= 1:
            _run_case_list(cases=cases, **list_kwargs)
        else:
            threads: list[threading.Thread] = []
            for sn in worker_sns:
                subset = [c for c in cases if str(c.get("sn") or "").strip() == sn]
                if not subset:
                    continue
                t = threading.Thread(
                    target=_run_case_list,
                    kwargs={**list_kwargs, "cases": subset},
                    daemon=True,
                    name=f"case-run-{run_id}-{sn}",
                )
                threads.append(t)
                t.start()
            for t in threads:
                t.join()
        if not _task_still_running(run_id):
            return
        latest = run_store.get(run_id) or doc
        failed = int(latest.get("failed") or 0) + int(latest.get("blocked") or 0)
        status = "failed" if failed else "done"
        finished = run_store.finish(run_id, status=status, error=latest.get("error") or "")
        persist_run_finish(finished)
        emit_testing_task({
            "event": "task_finished",
            "run_id": run_id,
            "task_id": run_id,
            "status": finished.get("status"),
            "app_id": str((latest or doc).get("app_id") or ""),
        })
    except Exception as exc:
        SLog.e(TAG, f"run {run_id} crashed: {exc}")
        try:
            if _task_still_running(run_id):
                finished = run_store.finish(run_id, status="failed", error=str(exc)[:240])
                persist_run_finish(finished)
        except Exception:
            pass
    finally:
        from mino_nexus.services.account_lease import release_run_lease
        from mino_nexus.services.device_resource_lease import release_device_leases_for_run

        latest = run_store.get(run_id) or doc
        app_id = str((latest or doc or {}).get("app_id") or "")
        release_run_lease(run_id, app_id=app_id)
        release_device_leases_for_run(run_id)
        sns = list(latest.get("sns") or [])
        head = str(latest.get("sn") or "").strip()
        if head and head not in sns:
            sns = [head, *sns]
        release_devices_for_run(
            run_id,
            sns=sns,
            platforms_by_sn=latest.get("platforms_by_sn") if isinstance(latest.get("platforms_by_sn"), dict) else {},
        )


def run_explore(
    app: dict[str, Any],
    *,
    sn: str = "",
    max_steps: int = 80,
    max_idle_steps: int = 15,
    instruction: str = "",
    provider_id: str = "",
    async_exec: bool = True,
    platform: str = "android",
    playwright_headless: bool = True,
) -> dict[str, Any]:
    """发起应用探索：LLM 自由操作，被动采集拓展 Screen Atlas。"""
    from mino_nexus.loop.explore_case import build_explore_case

    device_sns = _normalize_sns(sn, None)
    if not device_sns:
        device_sns = _pick_online_sns()[:1]
    for dsn in device_sns:
        busy = run_store.busy_task_for_sn(dsn)
        if busy:
            raise DeviceBusy(dsn, busy)

    explore_case = build_explore_case(
        instruction=str(instruction or "").strip(),
        max_steps=max_steps,
        max_idle_steps=max_idle_steps,
    )
    cfg = aas.get_automation_config(app)
    package = aas.package_for_app(app, platform=platform if platform in ("android", "ios", "web") else "android")
    playbook = aas.get_playbook(app)
    run_id = run_store.new_run_id()
    seeded = run_store.seed_case(run_id, explore_case, 0, sn=device_sns[0] if device_sns else "", coverage="once")
    provider, gate = resolve_regression_provider(str(provider_id or "").strip() or None)
    doc: dict[str, Any] = {
        "run_id": run_id,
        "task_id": run_id,
        "engine": "agent",
        "run_type": "explore",
        "session_kind": "explore",
        "app_id": str(app.get("id") or ""),
        "app_name": str(app.get("name") or ""),
        "sn": device_sns[0] if device_sns else "",
        "sns": device_sns,
        "coverage": "once",
        "platform": platform,
        "env_profile": cfg.get("env_profile") or "test",
        "package": package,
        "playbook": playbook if isinstance(playbook, dict) else {},
        "provider_id": (provider or {}).get("id") or gate.get("provider_id") or "",
        "provider_name": (provider or {}).get("name") or "",
        "model_name": (provider or {}).get("model") or "",
        "playwright_headless": bool(playwright_headless),
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "cases": [seeded],
        "error": "",
        "total": 1,
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "explore": {
            "max_steps": int(explore_case.get("max_steps") or 80),
            "max_idle_steps": int(explore_case.get("max_idle_steps") or 15),
        },
    }
    run_store.put(doc)
    persist_run_finish(doc)
    emit_testing_task({"event": "task_created", "run_id": run_id, "task_id": run_id, "app_id": doc["app_id"], "kind": "explore"})

    fail_reason = ""
    if not device_sns:
        fail_reason = "没有在线设备。请在 Studio 连接 Scout 后再发起探索。"
    elif device_sns and not any(
        d.get("status") == "online" and d.get("sn") == device_sns[0]
        for d in ui_devices.ui_devices()
    ):
        fail_reason = f"设备 {device_sns[0]} 已离线，请连接 Scout 后重试。"
    elif not provider:
        fail_reason = gate.get("reason") or "未配置用例执行大模型（Studio → 密钥 → 大模型 Key）"

    if fail_reason:
        run_store.patch_case(run_id, explore_case["case_id"], status="fail", summary=fail_reason, error=fail_reason)
        finished = run_store.finish(run_id, status="failed", error=fail_reason)
        persist_run_finish(finished)
        return run_store.to_task_json(finished)

    worker = threading.Thread(
        target=_run_in_background,
        kwargs={"run_id": run_id, "package": package, "playbook": playbook, "provider_id": doc["provider_id"]},
        daemon=True,
        name=f"explore-run-{run_id}",
    )
    worker.start()
    if not async_exec:
        worker.join(timeout=1200)
        latest = run_store.get(run_id) or doc
        return run_store.to_task_json(latest)
    return run_store.to_task_json(doc)


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
        playwright_headless=bool(doc.get("playwright_headless", True)),
    )
    return {"ok": True, "code": 200, "case_ids": failed_ids, "data": snapshot}


class DeviceBusy(Exception):
    def __init__(self, sn: str, busy_task_id: str):
        super().__init__(f"device busy: {sn}")
        self.sn = sn
        self.busy_task_id = busy_task_id
