"""为已有逻辑块补全 key_ref（P8 一次性 backfill）。"""
from __future__ import annotations

from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID, NavFlowBlockCatalog
from mino_nexus.services.case_key_registry import default_block_key_ref


def backfill_flow_block_key_refs() -> int:
    from mino_nexus.core.database import SessionLocal

    db = SessionLocal()
    n = 0
    try:
        rows = (
            db.query(NavFlowBlockCatalog)
            .filter(NavFlowBlockCatalog.app_id == GLOBAL_APP_ID)
            .all()
        )
        for row in rows:
            if str(getattr(row, "key_ref", "") or "").strip():
                continue
            bid = str(row.block_id or "").strip()
            if not bid:
                continue
            row.key_ref = default_block_key_ref(bid)
            n += 1
        if n:
            db.commit()
    finally:
        db.close()
    return n
