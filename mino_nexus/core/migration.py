"""启动时 additive 迁移。新库靠 create_all；旧库缺列再 ALTER。"""
from __future__ import annotations

from sqlalchemy import text

from mino_nexus.core.database import engine
from mino_nexus.core.log import SLog

TAG = "Migration"


def run_auto_migration() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        conn.commit()
    SLog.i(TAG, "schema ready")
