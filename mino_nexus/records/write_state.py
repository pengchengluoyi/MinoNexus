"""写动作安全状态（只读）：判断某次运行/某台设备是否还有未结算的写。

原住在 `services/run_write_safety.py`，但 `protected()` 本身只读库、不发放资源，
却被 `session_store`（要进 `records/`）依赖。把它下沉到 `records/`，事件日志就能
在不反向依赖 `services` 的前提下引用它；`run_write_safety` 继续 re-export，调用点不变。
"""
from __future__ import annotations

from typing import Any


def unresolved(doc: dict | None, sn: str = "") -> list[dict]:
    values = (doc or {}).get("write_safety") or {}
    return [v for v in values.values() if isinstance(v, dict)
            and v.get("state") in {"pending", "isolated"}
            and (not sn or not v.get("sn") or v.get("sn") == sn)]


def protected(run_id: str, *, db=None, sn: str = "") -> bool:
    """True 表示"写结果未知"，调用方不得据此释放资源或宣称成功。"""
    if not run_id:
        return False
    from mino_nexus.models.run import AppRegressionRun
    try:
        if db is not None:
            row = db.get(AppRegressionRun, run_id)
            if row is None:
                # Strict account leases are owned by the persisted attempt session.
                # Resolve its task explicitly; never infer the parent from ID syntax.
                from mino_nexus.models.session_event import SessionMeta
                attempt = db.get(SessionMeta, run_id)
                if attempt is not None:
                    row = db.get(AppRegressionRun, attempt.run_id)
                    if (row is None or not attempt.owner_user_id
                            or attempt.owner_user_id != row.owner_user_id):
                        return True  # An orphaned/foreign binding is not release evidence.
                    run_id = row.run_id
            if row and unresolved(row.payload, sn):
                return True
            from mino_nexus.models.execution_resource import ExecutionResource
            import time
            resources = db.query(ExecutionResource).filter_by(run_id=run_id)
            if sn:
                resources = resources.filter((ExecutionResource.sn == sn) | (ExecutionResource.sn == ""))
            return any(resource.state == "isolated" or (resource.state == "held" and resource.expires_at <= time.time())
                       for resource in resources.all())
        from mino_nexus.core.database import session_scope
        with session_scope() as session:
            return protected(run_id, db=session, sn=sn)
    except Exception:
        return True  # 状态不可读不能成为释放设备或账号的依据。
