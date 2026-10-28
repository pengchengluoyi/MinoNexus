"""SQLite 迁移。新表只补不改；已下线的 trace / baseline / 用例评审表会删除。

页面入口层（nav_page_*）和用例页面引用（nav_case_*）各一组新表。
迁移在严格 schema 校验之前执行；迁移失败必须直接拒绝启动，
不能带着半套表继续跑。
"""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

SCHEMA_VERSION = 6

NAV_ENTRY_TABLES = (
    "nav_page_entries",
    "nav_page_entry_evidence",
    "nav_page_gaps",
)

NAV_CASE_TABLES = (
    "nav_case_page_refs",
)

CASE_SCRIPT_TABLES = (
    "case_scripts",
    "case_platform_scripts",
    "project_script_repos",
)

ADDITIVE_TABLES = NAV_ENTRY_TABLES + NAV_CASE_TABLES + CASE_SCRIPT_TABLES

RETIRED_TABLES = (
    "m_case_run_trace",
    "m_case_baseline",
    "case_review_records",
)


def _user_version(conn) -> int:
    return int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)


def apply_additive_migrations(engine: Engine, metadata) -> None:
    """把旧库补到当前版本。新表只创建；已下线表直接删除。"""
    with engine.begin() as conn:
        version = _user_version(conn)
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database_schema_newer; db_user_version={version}; "
                f"code_user_version={SCHEMA_VERSION}"
            )
        present = set(inspect(conn).get_table_names())
        for name in RETIRED_TABLES:
            if name in present:
                conn.exec_driver_sql(f'DROP TABLE "{name}"')
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
