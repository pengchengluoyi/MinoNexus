"""执行账号：HTTP 绑定与持久任务归属。

上下文原语已下沉 `contract/ownership.py`（records 需要）；本模块保留
只保留 `user_thread`（固化账号后起线程）。任务归属在 `casework/job_owner.py`，
ASGI 中间件在 `api/middleware.py`。
"""
from __future__ import annotations

import threading
from contextvars import copy_context

from mino_nexus.contract.ownership import (  # noqa: F401 — re-export
    current_user_id,
    execution_user,
    require_user_id,
)


def user_thread(*, target, args=(), kwargs=None, **options) -> threading.Thread:
    """在创建时固化已验证账号，而不是在线程启动时读取共享状态。"""
    require_user_id()
    context = copy_context()
    return threading.Thread(target=context.run, args=(target, *args), kwargs=kwargs or {}, **options)
