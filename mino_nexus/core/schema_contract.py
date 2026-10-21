"""Initialize an empty database; reject older schemas without modifying them."""
from sqlalchemy import inspect


def initialize_current_schema(engine, metadata):
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if not tables:
        metadata.create_all(bind=engine)
        return
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
