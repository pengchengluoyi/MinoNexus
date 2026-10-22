"""节点断开/设备丢失：在途 run 立刻失败，不重派。"""
from __future__ import annotations

from mino_nexus.casework.run_store import finish, get, request_cancel, task_is_live


def interrupt_runs(run_ids: list[str], *, reason: str) -> list[str]:
    """节点断开 / 设备丢失：在途 run 立刻失败，不重派。"""
    from mino_nexus.loop.observe.agent_stream import emit_testing_task
    from mino_nexus.loop.web.web_env import release_devices_for_run

    done: list[str] = []
    seen: set[str] = set()
    for raw in run_ids or []:
        rid = str(raw or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        request_cancel(rid)
        doc = get(rid)
        if not doc or not task_is_live(doc):
            continue
        finished = finish(rid, status="failed", error=reason)
        sns = [str(x) for x in (finished.get("sns") or []) if str(x).strip()]
        head = str(finished.get("sn") or "").strip()
        if head and head not in sns:
            sns = [head, *sns]
        release_devices_for_run(
            rid,
            sns=sns,
            platforms_by_sn=finished.get("platforms_by_sn")
            if isinstance(finished.get("platforms_by_sn"), dict)
            else {},
        )
        from mino_nexus.casework.run_resource_release import release_run_resource_holdings

        release_run_resource_holdings(finished, run_id=rid)
        emit_testing_task({
            "event": "task_finished",
            "run_id": rid,
            "task_id": rid,
            "status": finished.get("status") or "failed",
            "app_id": str(finished.get("app_id") or ""),
        })
        done.append(rid)
    return done
