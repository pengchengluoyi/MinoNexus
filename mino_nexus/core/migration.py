"""启动时 additive 迁移。新库靠 create_all；旧库缺列再 ALTER。"""
from __future__ import annotations

from sqlalchemy import inspect, text

from mino_nexus.core.database import engine
from mino_nexus.core.log import SLog

TAG = "Migration"


def _ensure_column(table: str, column: str, ddl: str) -> None:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns(table)}
    if column in cols:
        return
    with engine.connect() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        conn.commit()
    SLog.i(TAG, f"added column {table}.{column}")


def run_auto_migration() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        conn.commit()
    try:
        _ensure_column("llm_jobs", "overrides_json", "overrides_json JSON")
    except Exception as exc:
        SLog.w(TAG, f"migration skipped: {exc}")
    SLog.i(TAG, "schema ready")
