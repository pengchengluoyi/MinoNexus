"""外部执行的软租约：登记「谁、哪台、什么时间段」，冲突只提示不拒绝。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from mino_nexus.core.database import session_scope
from mino_nexus.models.external_lease import ExternalLease
from mino_nexus.platform.identity import require_user_id

DEFAULT_MINUTES = 30
MAX_MINUTES = 240


def acquire(run_id: str, sn: str, *, agent: str = "", minutes: int = DEFAULT_MINUTES) -> dict[str, Any]:
    sn = str(sn or "").strip()
    if not sn:
        return {"lease": None, "conflicts": []}
    now = time.time()
    span = max(1, min(int(minutes or DEFAULT_MINUTES), MAX_MINUTES)) * 60
    uid = require_user_id()
    with session_scope() as db:
        overlap = db.query(ExternalLease).filter(
            ExternalLease.sn == sn, ExternalLease.released_at.is_(None),
            ExternalLease.expires_at > now, ExternalLease.run_id != run_id).all()
        conflicts = [{"kind": "external_lease", "run_id": r.run_id, "agent": r.agent,
                      "mine": r.owner_user_id == uid, "expires_at": r.expires_at} for r in overlap]
        lease = ExternalLease(lease_id="xl-" + uuid.uuid4().hex[:12], run_id=run_id, owner_user_id=uid,
                              sn=sn, agent=str(agent or "")[:80], starts_at=now, expires_at=now + span)
        db.add(lease)
        lease_id, expires = lease.lease_id, lease.expires_at
    from mino_nexus.casework import run_store
    busy = run_store.busy_task_for_sn(sn)
    if busy and busy != run_id:
        conflicts.append({"kind": "nexus_run", "run_id": busy})
    return {"lease": {"lease_id": lease_id, "sn": sn, "expires_at": expires}, "conflicts": conflicts}


def release(run_id: str) -> None:
    with session_scope() as db:
        for row in db.query(ExternalLease).filter(ExternalLease.run_id == run_id,
                                                  ExternalLease.released_at.is_(None)).all():
            row.released_at = time.time()
