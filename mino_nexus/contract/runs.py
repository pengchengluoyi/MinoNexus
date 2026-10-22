"""跑批台账的只读查询端口。

`resources` 需要知道"这台设备上还有没有在途任务"，但不该 import `casework`——
用例台账在 `casework`，资源域只在下面。台账在 import 时把查询函数注册进来；
未注册时按"没有在途任务"回答（此时也确实没有跑批）。
"""
from __future__ import annotations

from typing import Any, Callable

_LIVE_STATUS = frozenset({"running", "queued"})
_impl: dict[str, Callable[..., Any]] = {}


def register_runs(**fns: Callable[..., Any]) -> None:
    _impl.update(fns)


def get(run_id: str) -> dict[str, Any] | None:
    fn = _impl.get("get")
    return fn(run_id) if fn else None


def task_is_live(doc: dict[str, Any] | None) -> bool:
    fn = _impl.get("task_is_live")
    if fn is not None:
        return bool(fn(doc))
    return str((doc or {}).get("status") or "") in _LIVE_STATUS


def running_run_ids_for_sn(sn: str, *, limit: int = 400) -> list[str]:
    fn = _impl.get("running_run_ids_for_sn")
    if fn is None:
        return []
    return fn(sn, limit=limit)


def busy_task_for_sn(sn: str) -> str:
    ids = running_run_ids_for_sn(sn, limit=80)
    return ids[0] if ids else ""
