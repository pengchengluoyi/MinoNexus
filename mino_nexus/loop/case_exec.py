"""按任务文档逐条执行用例。任务的创建和落库在 case_runner。"""
from __future__ import annotations
from mino_nexus.platform.identity import user_thread, require_user_id
from mino_nexus.casework.job_owner import owned_job


import threading
from typing import Any

from mino_nexus.casework import run_store
from mino_nexus.core.log import SLog
from mino_nexus.loop.observe.agent_stream import emit_testing_task
from mino_nexus.loop.web.web_env import release_devices_for_run
from mino_nexus.loop.observe.persist_run_finish import persist_run_finish

TAG = "CaseRunner"


def _task_still_running(run_id: str) -> bool:
    doc = run_store.get(run_id)
    return run_store.task_is_live(doc)


def _case_trace_fields(run_id: str, case: dict[str, Any]) -> dict[str, Any]:
    """日志用真实 session_id。耗时只在有起止时间时给出，不用 0 充数。"""
    from datetime import datetime

    from mino_nexus.records.eventlog.store import get_meta, resolve_session_id

    cid = str(case.get("case_id") or "")
    sid = str(case.get("report_run_id") or "").strip() or run_store.report_run_id(
        run_id, cid, sn=str(case.get("sn") or ""), coverage=str(case.get("coverage") or "")
    )
    resolved = resolve_session_id(sid, case_id=cid) or resolve_session_id(run_id, case_id=cid)
    meta = get_meta(resolved) if resolved else None
    if not meta:
        return {}
    out: dict[str, Any] = {"session_id": str(meta.get("session_id") or resolved)}
    start = str(meta.get("started_at") or "")
    end = str(meta.get("finished_at") or "")
    if start and end:
        try:
            a = datetime.fromisoformat(start)
            b = datetime.fromisoformat(end)
            ms = int((b - a).total_seconds() * 1000)
            if ms >= 0:
                out["duration_ms"] = ms
        except ValueError:
            pass
    return out


def _record_case_preflight_fail(
    *,
    run_id: str,
    case: dict[str, Any],
    reason: str,
    app_id: str,
    sn: str,
    provider_id: str = "",
    reason_code: str = "",
    reason_detail: dict | None = None,
    status: str = "fail",
) -> None:
    """开跑前失败（租约/拒跑）也落 session，避免 Studio Session Log 404。"""
    from mino_nexus.records.eventlog.writer import open_session

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
        block = {"reason": summary, "reason_code": reason_code, "execution_started": "no"}
        if reason_detail:
            block["reason_detail"] = reason_detail
        writer.append("preflight/block", block)
        # close 必须带 reason_code：不带的话 session/end 里 reason_code 是空串，
        # 事后只能看到 status=fail + "unclassified"，查不出是准入失败
        # （cr-1dd866f93044 的 case-5c5bf758/ed07139f/bc49fd61）。
        writer.close(status=status, summary=summary, step_count=0, reason_code=reason_code)
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
    from mino_nexus.casework.run_write_safety import protected
    from mino_nexus.loop.device_run_continuity import (
        continuity_blockers,
        get_sn_state,
        update_after_case,
    )

    env_brief = str(doc.get("env_brief") or "")
    continuity_by_sn: dict = {}
    for case_seq, case in enumerate(cases):
        if case.get("status") not in {None, "pending", "running"}:
            continue
        if not _task_still_running(run_id):
            break
        if protected(run_id, sn=str(case.get("sn") or doc.get("sn") or "")):
            for untouched in cases[case_seq:]:
                if str(untouched.get("status") or "pending") not in ("pending", "running"):
                    continue
                run_store.patch_case(run_id, str(untouched.get("case_id") or ""),
                    target_sn=str(untouched.get("sn") or ""), status="blocked",
                    summary="前序动作停止未确认，本条未执行；设备与账号资源仍隔离",
                    reason_code="resource_isolated", execution_started="no", resource_isolated=True)
            break
        cid = str(case.get("case_id") or "")
        sn = str(case.get("sn") or doc.get("sn") or "")
        from mino_nexus.platform.llm import dispatch_log
        dispatch_log.bind(task_id=run_id, case_id=cid, case_step_index=0,
                          session_id=str(case.get("report_run_id") or run_store.report_run_id(run_id, cid, sn=sn, coverage=str(case.get("coverage") or ""))))
        run_store.patch_case(run_id, cid, target_sn=sn, status="running")
        emit_testing_task({
            "event": "case_running",
            "run_id": run_id,
            "task_id": run_id,
            "case_id": cid,
            "sn": sn,
            "app_id": str(doc.get("app_id") or ""),
        })
        from mino_nexus.runtime.session_gate import ensure_case_scene

        scene = ensure_case_scene(case)
        from mino_nexus.resources import project_store as ps
        from mino_nexus.resources.case_resource_claim import (
            ensure_resource_key_on_case,
            preflight_device_app_gap,
        )
        from mino_nexus.resources.resource_preflight import run_start_resource_blockers

        app_id = str(doc.get("app_id") or "")
        project_id = ""
        if app_id:
            try:
                project_id = str(ps.require_app(app_id).get("project_id") or "")
            except KeyError:
                project_id = ""
        env_doc = ps.project_env(project_id) if project_id else None
        try:
            ensure_resource_key_on_case(
                case,
                env_doc=env_doc,
                env=str(doc.get("env_profile") or "test"),
                package=str(package or ""),
            )
        except ValueError as exc:
            reason = str(exc)[:240]
            _record_case_preflight_fail(
                run_id=run_id, case=case, reason=reason, app_id=app_id, sn=sn,
                provider_id=str(doc.get("provider_id") or ""), reason_code=reason.split(":", 1)[0],
            )
            run_store.patch_case(run_id, cid, target_sn=sn, status="fail", summary=reason, error=reason, **_case_trace_fields(run_id, case))
            emit_testing_task({"event": "case_finished", "run_id": run_id, "task_id": run_id, "case_id": cid, "status": "fail", "app_id": app_id})
            continue
        rk = case.get("resource_key") if isinstance(case.get("resource_key"), dict) else None
        merged_scene = scene
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
            run_store.patch_case(run_id, cid, target_sn=sn, status="fail", summary=reason, error=reason, **_case_trace_fields(run_id, case))
            emit_testing_task({
                "event": "case_finished",
                "run_id": run_id,
                "task_id": run_id,
                "case_id": cid,
                "status": "fail",
                "app_id": app_id,
            })
            continue
        try:
            blockers = run_start_resource_blockers(
                rk,
                scene=merged_scene,
                precondition=str(case.get("precondition") or ""),
                sn=sn,
                package_id=str(package or ""),
            )
        except ValueError as exc:
            blockers = [str(exc)]
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
            run_store.patch_case(run_id, cid, target_sn=sn, status="fail", summary=reason, error=reason, **_case_trace_fields(run_id, case))
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

            SLog.i("CaseRunner", f"resource preflight case={cid} " + "; ".join(gaps[:3]))
        platform = str(
            doc.get("platform")
            or case.get("platform")
            or (rk.get("platform") if isinstance(rk, dict) else "")
            or "android"
        )
        from mino_nexus.resources.device_resource_lease import (
            release_device_lease,
        )
        from mino_nexus.casework.execution_resources import admission, claim_without_account, heartbeat, release as release_bundle
        device_held = False
        result = None
        repo: dict = {}
        try:
            from mino_nexus.casework.script_workspace import get_repo
            from mino_nexus.resources.accounts.lease import ensure_case_account_lease

            repo = get_repo(project_id) if project_id else {}
            with admission(doc, case, project_id=project_id, package=package, platform=platform) as resource_attempt:
                ok, lease_err = ensure_case_account_lease(
                    run_id, app_id=app_id, env_profile=str(doc.get("env_profile") or "test"),
                    platform=platform, target_package=package, case=case, scene=scene,
                    plugin_user_id=str(doc.get("plugin_user_id") or ""))
                if ok and not resource_attempt["claimed"]:
                    ok = claim_without_account()
                    lease_err = resource_attempt.get("reason") or "" if not ok else ""
            device_held = resource_attempt["claimed"]
            if not ok:
                code = str(resource_attempt.get("reason_code") or lease_err or "租号失败")
                detail = resource_attempt.get("reason_detail") if isinstance(resource_attempt.get("reason_detail"), dict) else {}
                coarse = str(resource_attempt.get("reason") or "")
                holder = str(detail.get("holder_run_id") or detail.get("resource_key") or "")
                reason = lease_err or "租号失败"
                if resource_attempt.get("reason_code"):
                    reason = f"资源组合准入：{code}"
                    if coarse and coarse != code:
                        reason += f"（{coarse}）"
                    if holder:
                        reason += f"；{holder}"
                _record_case_preflight_fail(
                    run_id=run_id,
                    case=case,
                    reason=reason,
                    app_id=app_id,
                    sn=sn,
                    provider_id=str(doc.get("provider_id") or ""),
                    reason_code=code,
                    reason_detail=detail or None,
                )
                run_store.patch_case(run_id, cid, target_sn=sn, status="fail", summary=reason, error=reason,
                    reason_code=code, reason_detail=detail or None, **_case_trace_fields(run_id, case))
                emit_testing_task({
                    "event": "case_finished",
                    "run_id": run_id,
                    "task_id": run_id,
                    "case_id": cid,
                    "status": "fail",
                    "app_id": str(doc.get("app_id") or ""),
                })
                continue
            from mino_nexus.resources.resource_task_card import build_resource_card

            lease_snap: dict = {}
            try:
                from mino_nexus.resources.accounts.lease import restore_lease_for_run

                class _LeaseCtx:
                    pass

                lc = _LeaseCtx()
                lc.app_id, lc.run_id, lc.case_id, lc.sn = app_id, run_id, cid, sn
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
            run_store.patch_case(run_id, cid, target_sn=sn, resource_card=resource_card)
            emit_testing_task({
                "event": "case_resource",
                "run_id": run_id,
                "task_id": run_id,
                "case_id": cid,
                "app_id": app_id,
                "resource_card": resource_card,
            })
            next_case = cases[case_seq + 1] if case_seq + 1 < len(cases) else None
            from mino_nexus.casework.execution_policy import start_attempt
            start_attempt(run_id, case)
            result = None
            with heartbeat(resource_attempt):
                from mino_nexus.loop.script_dispatch import dispatch_script_case

                result = dispatch_script_case(
                    run_id=run_id, case=case, sn=sn, app_id=app_id, package=package,
                    provider_id=provider_id, cancel_check=lambda: run_store.task_cancelled(run_id),
                    case_seq=case_seq, platform=platform, project_id=project_id, repo=repo,
                )
            doc = run_store.get(run_id) or doc
            env_brief = str(doc.get("env_brief") or env_brief)
            run_store.patch_case(
                run_id, cid, target_sn=sn,
                status=result.get("status") or "fail",
                summary=result.get("summary") or "",
                error=result.get("summary") or "",
                elapsed_ms=result.get("elapsed_ms") or 0,
                engine_steps=result.get("steps") or [],
                skill_id=result.get("skill_id") or "",
                view_id=result.get("view_id") or "",
                slots=result.get("slots") if isinstance(result.get("slots"), dict) else {},
                resource_isolated=bool(result.get("resource_isolated")),
                reason_code=str(result.get("reason_code") or ""),
                failure_stage=str(result.get("failure_stage") or ""),
                case_step=int(result.get("case_step") or 0),
                **_case_trace_fields(run_id, case),
            )
            emit_testing_task({
                "event": "case_finished",
                "run_id": run_id,
                "task_id": run_id,
                "case_id": cid,
                "status": result.get("status"),
                "reason_code": str(result.get("reason_code") or ""),
                "failure_stage": str(result.get("failure_stage") or ""),
                "app_id": str(doc.get("app_id") or ""),
            })
            if not _task_still_running(run_id):
                break
            update_after_case(
                sn_state,
                status=str(result.get("status") or "fail"),
                precondition=str(case.get("precondition") or ""),
                case=case if isinstance(case, dict) else {},
                summary=str(result.get("summary") or ""),
            )
        except Exception as exc:
            import traceback

            reason = f"{type(exc).__name__}: {exc}"[:240] or "执行异常"
            SLog.e(TAG, f"case {cid} run={run_id} crashed: {reason}\n{traceback.format_exc()}")
            run_store.patch_case(
                run_id, cid, target_sn=sn, status="fail", summary=reason, error=reason,
                **_case_trace_fields(run_id, case),
            )
            emit_testing_task({
                "event": "case_finished",
                "run_id": run_id,
                "task_id": run_id,
                "case_id": cid,
                "status": "fail",
                "app_id": str(doc.get("app_id") or ""),
            })
            update_after_case(
                sn_state,
                status="fail",
                precondition=str(case.get("precondition") or ""),
                case=case if isinstance(case, dict) else {},
                summary=reason,
            )
        finally:
            if device_held and not (result or {}).get("resources_owned_by_exit"):
                release_bundle(run_id, sn, attempt_id=resource_attempt["attempt_id"])
                release_device_lease(sn, str(package or ""), run_id, attempt_id=resource_attempt["attempt_id"])


@owned_job()
def _run_in_background(*, run_id: str, package: str, playbook: dict, provider_id: str) -> None:
    from mino_nexus.loop.case_runner import _worker_sns_for_cases

    doc = run_store.get(run_id)
    if not doc or not run_store.task_is_live(doc):
        return
    try:
        from mino_nexus.casework.resource_transition import mark_web_run_devices

        mark_web_run_devices(run_id, "guest", source="task_start", package=package, doc=doc)
        cases = [case for case in doc.get("cases") or [] if case.get("status") in {None, "pending"}]
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
                t = user_thread(
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
        from mino_nexus.resources.accounts.lease import release_run_lease
        from mino_nexus.resources.device_resource_lease import release_device_leases_for_run

        latest = run_store.get(run_id) or doc
        app_id = str((latest or doc or {}).get("app_id") or "")
        from mino_nexus.casework.resource_transition import mark_web_run_devices

        from mino_nexus.casework.run_write_safety import protected

        if not protected(run_id):
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
            allow_unbound_cleanup=False,
        )
