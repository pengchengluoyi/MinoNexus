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


def _drop_table_if_exists(table: str) -> None:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return
    with engine.connect() as conn:
        conn.execute(text(f"DROP TABLE {table}"))
        conn.commit()
    SLog.i(TAG, f"dropped table {table}")


def run_auto_migration() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        conn.commit()
    try:
        _ensure_column("llm_jobs", "overrides_json", "overrides_json JSON")
        _ensure_column("llm_jobs", "prompt_version", "prompt_version INTEGER DEFAULT 1")
        _drop_table_if_exists("app_cases")
        # NavFSM：新库靠 create_all；若有人按 docs/NAVIGATION_ATLAS.md §0.2.1 的 DDL 手建过表，
        # 这里把本仓多出来的列补上（设计稿 DDL 没有 scroll_into_view，见 §10.4）。
        _ensure_column("nav_fsm_edges", "scroll_into_view", "scroll_into_view JSON")
        _ensure_column("nav_fsm", "test_data", "test_data JSON")
        _ensure_column("nav_fsm", "updated_by", "updated_by TEXT")
        _ensure_column("nav_fsm", "updated_at", "updated_at INTEGER DEFAULT 0")
        _ensure_column("nav_fsm_states", "entry", "entry INTEGER DEFAULT 0")
        _ensure_column("nav_fsm_states", "role", "role TEXT DEFAULT ''")
    except Exception as exc:
        SLog.w(TAG, f"migration skipped: {exc}")
    SLog.i(TAG, "schema ready")
