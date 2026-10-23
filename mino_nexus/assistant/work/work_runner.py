"""单 Worker 工作层执行循环。"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid

from mino_nexus.assistant.work import work_agent
from mino_nexus.assistant.work.work_handlers import execute_step
from mino_nexus.assistant.work.work_store import (
    claim_step, cleanup_terminal_work, detail, finish_step, heartbeat,
    purge_expired_memories, recover_stale_work, reserve_budget,
)
from mino_nexus.core.log import SLog

_STOP = threading.Event()


def start() -> None:
    _STOP.clear()


def stop() -> None:
    _STOP.set()


def _block(work_id: str, reason: str) -> None:
    from mino_nexus.assistant.work.work_store import block_work

    try:
        block_work(work_id, reason)
    except Exception:
        pass


def run_once(worker_id: str) -> bool:
    if _STOP.is_set():
        return False
    step = claim_step(worker_id)
    if not step:
        return False
    work = detail(step["work_id"])
    if not work:
        finish_step(step["step_id"], worker_id=worker_id, status="cancelled", error="work_missing")
        return True
    if not reserve_budget(step, work):
        return True

    stop_beat = threading.Event()

    def beat() -> None:
        while not stop_beat.wait(15):
            try:
                if not heartbeat(step["step_id"], worker_id):
                    return
            except Exception:
                return

    beat_thread = threading.Thread(target=beat, daemon=True, name="work-hb-" + step["step_id"][:8])
    beat_thread.start()
    try:
        from mino_nexus.platform.identity import execution_user

        with execution_user(work["owner_user_id"]):
            result = execute_step(step, work)
        if not isinstance(result, dict):
            SLog.w("Work", f"handler returned non-dict: {step['step_id']}")
    except work_agent.WorkAgentError as exc:
        if exc.permanent:
            finish_step(step["step_id"], worker_id=worker_id, status="failed", error=str(exc))
            _block(step["work_id"], "handler_failed:" + str(exc)[:200])
        elif step.get("kind") == "tool.call":
            # 工具是否已发出写请求无法确认，绝不重放。
            finish_step(step["step_id"], worker_id=worker_id, status="unknown",
                        error=str(exc))
            _block(step["work_id"], "write_result_unknown")
        else:
            attempt = int(step.get("attempt") or 1)
            if attempt < int(step.get("max_attempts") or 3):
                finish_step(step["step_id"], worker_id=worker_id, status="ready",
                            next_run_at=time.time() + min(30, 2 ** attempt),
                            error=str(exc))
            else:
                finish_step(step["step_id"], worker_id=worker_id, status="failed", error=str(exc))
                _block(step["work_id"], "handler_failed:" + str(exc)[:200])
    except Exception as exc:
        if step.get("kind") == "tool.call":
            finish_step(step["step_id"], worker_id=worker_id, status="unknown",
                        error=f"{type(exc).__name__}: {exc}"[:2000])
            _block(step["work_id"], "write_result_unknown")
        else:
            attempt = int(step.get("attempt") or 1)
            if attempt < int(step.get("max_attempts") or 3):
                finish_step(step["step_id"], worker_id=worker_id, status="ready",
                            next_run_at=time.time() + min(30, 2 ** attempt),
                            error=f"{type(exc).__name__}: {exc}"[:2000])
            else:
                finish_step(step["step_id"], worker_id=worker_id, status="failed",
                            error=f"{type(exc).__name__}: {exc}"[:2000])
                _block(step["work_id"], "handler_failed:" + type(exc).__name__)
    finally:
        stop_beat.set()
        beat_thread.join(timeout=2)
    return True


def run_maintenance() -> None:
    """每日清理：过期记忆 + 超保留期的终态工作项。"""
    try:
        purge_expired_memories()
    except Exception as exc:
        SLog.w("Work", f"memory cleanup: {type(exc).__name__}")
    try:
        cleanup_terminal_work(90)
    except Exception as exc:
        SLog.w("Work", f"work cleanup: {type(exc).__name__}")


async def maintenance_worker() -> None:
    while not _STOP.is_set():
        await asyncio.to_thread(run_maintenance)
        await asyncio.sleep(6 * 3600)


async def worker() -> None:
    worker_id = "work-" + uuid.uuid4().hex[:8]
    while not _STOP.is_set():
        try:
            worked = await asyncio.to_thread(run_once, worker_id)
        except Exception as exc:
            SLog.w("Work", f"tick {type(exc).__name__}: {exc}")
            worked = False
        await asyncio.sleep(0.2 if worked else 1.0)


__all__ = ["worker", "maintenance_worker", "run_once", "recover_stale_work", "start", "stop"]
