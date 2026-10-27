"""只增不改的 SQLite 迁移。

工作层（work_*）、页面入口层（nav_page_*）和用例页面引用（nav_case_*）
各一组新表。迁移在严格 schema 校验之前执行；迁移失败必须直接拒绝启动，
不能带着半套表继续跑。
"""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

SCHEMA_VERSION = 3

# 兼容旧名字：现有部署/文档里有引用。
WORK_SCHEMA_VERSION = SCHEMA_VERSION

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

NAV_ENTRY_TABLES = (
    "nav_page_entries",
    "nav_page_entry_evidence",
    "nav_page_gaps",
)

NAV_CASE_TABLES = (
    "nav_case_page_refs",
)

ADDITIVE_TABLES = WORK_TABLES + NAV_ENTRY_TABLES + NAV_CASE_TABLES


def _user_version(conn) -> int:
    return int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)


def apply_additive_migrations(engine: Engine, metadata) -> None:
    """把旧库补到当前版本；只 create 缺失的新表，不 ALTER/DROP 已有表。"""
    with engine.begin() as conn:
        version = _user_version(conn)
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database_schema_newer; db_user_version={version}; "
                f"code_user_version={SCHEMA_VERSION}"
            )
        if version < SCHEMA_VERSION:
            for name in ADDITIVE_TABLES:
                table = metadata.tables.get(name)
                if table is None:
                    raise RuntimeError(f"schema_model_not_registered:{name}")
                table.create(bind=conn, checkfirst=True)
            conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
            version = SCHEMA_VERSION
        missing = [name for name in ADDITIVE_TABLES if name not in inspect(conn).get_table_names()]
        if missing:
            raise RuntimeError("schema_incomplete:" + ",".join(missing))
