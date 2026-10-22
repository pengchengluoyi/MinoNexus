"""持久任务归属：从 run 记录恢复执行账号，未归属的旧任务拒绝执行。"""
from __future__ import annotations

from mino_nexus.platform.identity import execution_user


def owned_job(owner_field: str = "owner_user_id"):
    """从参数里的持久任务归属恢复上下文，未归属的旧任务拒绝执行。"""
    from functools import wraps
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            from mino_nexus.casework import run_store
            doc = run_store.get(str(kwargs.get("run_id") or "")) or kwargs.get("doc") or {}
            owner = str(doc.get(owner_field) or "")
            if not owner:
                if doc.get("run_id"):
                    run_store.finish(doc["run_id"], status="failed", error="历史任务缺少归属，已拒绝执行")
                raise PermissionError("缺少执行账号，已拒绝执行")
            with execution_user(owner):
                from mino_nexus.platform.llm import dispatch_log
                token = dispatch_log.bind(task_id=str(doc.get("run_id") or kwargs.get("run_id") or ""),
                                          trigger="case_run", source="case_run", job="case-initialization")
                try:
                    return fn(*args, **kwargs)
                finally:
                    dispatch_log.reset(token)
        return wrapped
    return decorate
