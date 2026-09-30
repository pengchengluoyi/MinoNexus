"""按任务文档逐条执行用例。任务的创建和落库在 case_runner。"""
from __future__ import annotations

import threading
from typing import Any

from mino_nexus.services import run_store
from mino_nexus.core.log import SLog
from mino_nexus.loop.observe.agent_stream import emit_testing_task
from mino_nexus.loop.web.web_env import release_devices_for_run
from mino_nexus.loop.observe.persist_run_finish import persist_run_finish

TAG = "CaseRunner"


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
    from mino_nexus.loop.observe.session_log import open_session

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
                plugin_user_id=str(doc.get("plugin_user_id") or ""),
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
            next_case = cases[case_seq + 1] if case_seq + 1 < len(cases) else None
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
                next_case=next_case if isinstance(next_case, dict) else None,
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
    from mino_nexus.loop.case_runner import _worker_sns_for_cases

    doc = run_store.get(run_id)
    if not doc or not run_store.task_is_live(doc):
        return
    try:
        from mino_nexus.services.resource_transition import mark_web_run_devices

        mark_web_run_devices(run_id, "guest", source="task_start", package=package, doc=doc)
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
        from mino_nexus.services.resource_transition import mark_web_run_devices

        mark_web_run_devices(
            run_id,
            "logged_out",
            source="task_end",
            package=str((latest or doc or {}).get("package") or package or ""),
            doc=latest if isinstance(latest, dict) else doc,
        )
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

