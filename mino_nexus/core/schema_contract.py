"""Initialize an empty database; reject older schemas without modifying them."""
from sqlalchemy import inspect


def initialize_current_schema(engine, metadata):
    from mino_nexus.core.schema_migrations import apply_additive_migrations

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if not tables:
        metadata.create_all(bind=engine)
        apply_additive_migrations(engine, metadata)
        return
    # 先补新表，再做严格校验：旧库不应因为新增 work_* 被误判为不支持。
    apply_additive_migrations(engine, metadata)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    expected = set(metadata.tables)
    additive = {"node_tool_tasks"} & expected
    issues = ["missing_table:" + name for name in sorted(expected - tables - additive)]
    for name in sorted(expected & tables):
        actual = {column["name"] for column in inspector.get_columns(name)}
        required = set(metadata.tables[name].columns.keys())
        if actual != required:
            issues.append("columns_mismatch:" + name)
    if issues:
        raise RuntimeError("database_schema_unsupported; use a separately configured current-version database; " + ";".join(issues))
    for name in sorted(additive - tables):
        metadata.tables[name].create(bind=engine, checkfirst=True)
