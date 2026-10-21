"""Program-owned development capability; synchronized at every startup."""
def sync_development_capability():
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry
    payload = {"id": "node.ide_prepare", "version": 1, "kind": "generic", "caller": "program",
        "phases": ["prep"], "platforms": ["mac", "win"], "system_owned": True,
        "params": [{"name": "operation_id", "type": "string", "required": True},
                   {"name": "action", "type": "string", "enum": ["check", "execute", "stop", "restart", "build", "install", "logs", "probe", "save_connection", "list_connections", "receipt"]}],
        "implementations": [{"id": "ide.prepare.v1", "executor": "ide", "runs_on": "scout", "params_contract": {"version": 1}, "result_contract": {"version": 1}}]}
    with session_scope() as db:
        row = db.query(CatalogEntry).filter_by(kind="generic", id="node.ide_prepare").first()
        if row and (row.payload_json or {}).get("version", 0) > 1:
            return
        if not row:
            row = CatalogEntry(kind="generic", id="node.ide_prepare")
            db.add(row)
        row.payload_json, row.platforms_json = payload, payload["platforms"]
        row.display_name, row.enabled, row.owner = "PyCharm 开发准备", True, "system"
        row.visible_to_json = ["system"]
    from mino_nexus.catalog import registry
    registry.reload()
