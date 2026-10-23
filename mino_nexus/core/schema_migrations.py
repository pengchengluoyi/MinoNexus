"""只增不改的 SQLite 迁移。

当前只有工作层（work_*）一组新表。迁移在严格 schema 校验之前执行；
迁移失败必须直接拒绝启动，不能带着半套表继续跑。
"""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

WORK_SCHEMA_VERSION = 1

WORK_TABLES = (
    "work_items",
    "work_steps",
    "work_events",
    "work_approvals",
    "work_artifacts",
    "work_links",
    "work_agent_sessions",
    "work_agent_memory",
)


def _user_version(conn) -> int:
    return int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)


def apply_additive_migrations(engine: Engine, metadata) -> None:
    """把旧库补到当前版本；只 create 缺失的新表，不 ALTER/DROP 已有表。"""
    with engine.begin() as conn:
        version = _user_version(conn)
        if version > WORK_SCHEMA_VERSION:
            raise RuntimeError(
                f"database_schema_newer; db_user_version={version}; "
                f"code_user_version={WORK_SCHEMA_VERSION}"
            )
        if version < WORK_SCHEMA_VERSION:
            for name in WORK_TABLES:
                table = metadata.tables.get(name)
                if table is None:
                    raise RuntimeError(f"work_model_not_registered:{name}")
                table.create(bind=conn, checkfirst=True)
            conn.exec_driver_sql(f"PRAGMA user_version = {WORK_SCHEMA_VERSION}")
            version = WORK_SCHEMA_VERSION
        missing = [name for name in WORK_TABLES if name not in inspect(conn).get_table_names()]
        if missing:
            raise RuntimeError("work_schema_incomplete:" + ",".join(missing))
