"""启动时 additive 迁移。新库靠 create_all；旧库缺列再 ALTER。"""
from __future__ import annotations

import json

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


def _clear_user_plugin_secrets() -> None:
    """插件密钥改存在 Scout。旧行里的 JSON 清掉，不读出来、不搬到节点。"""
    insp = inspect(engine)
    if "user_plugin_secrets" not in insp.get_table_names():
        return
    with engine.connect() as conn:
        conn.execute(text("UPDATE user_plugin_secrets SET config = '{}'"))
        conn.commit()
    SLog.i(TAG, "cleared user_plugin_secrets")


def _remove_nexus_wechat_account() -> None:
    from mino_nexus.core.paths import data_dir

    path = data_dir() / "wechat_ilink.json"
    if not path.is_file():
        return
    path.unlink()
    SLog.i(TAG, "removed nexus wechat account file")


def run_auto_migration() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        conn.commit()
    _ensure_column("app_regression_runs", "owner_user_id", "owner_user_id TEXT DEFAULT ''")
    _ensure_column("session_meta", "owner_user_id", "owner_user_id TEXT DEFAULT ''")
    _ensure_column("dispatch_calls", "owner_user_id", "owner_user_id TEXT DEFAULT ''")
    _ensure_column("doc_sources", "owner_user_id", "owner_user_id TEXT DEFAULT ''")
    _ensure_column("im_route_identities", "binding_key", "binding_key TEXT NOT NULL DEFAULT ''")
    with engine.begin() as conn:
        conn.execute(text("UPDATE im_route_identities SET binding_key=lower(hex(randomblob(16))) WHERE binding_key=''"))
    from mino_nexus.core.migration_backup import MARKER
    with engine.begin() as conn:
        if not conn.execute(text("SELECT 1 FROM settings WHERE id=:id"), {"id": MARKER}).first():
            # 只继承旧任务已固化且仍为有效用户的 plugin_user_id；不推测其他 owner。
            conn.execute(text("""UPDATE app_regression_runs SET owner_user_id = json_extract(payload, '$.plugin_user_id'),
                payload = json_set(payload, '$.owner_user_id', json_extract(payload, '$.plugin_user_id'))
                WHERE COALESCE(owner_user_id, '') = '' AND json_valid(payload)
                  AND json_extract(payload, '$.plugin_user_id') IN (SELECT user_id FROM users)"""))
            conn.execute(text("""UPDATE session_meta SET owner_user_id = COALESCE((SELECT r.owner_user_id FROM app_regression_runs r
                WHERE r.run_id = session_meta.run_id), '') WHERE COALESCE(owner_user_id, '') = ''"""))
            conn.execute(text("INSERT INTO settings(id,payload) VALUES (:id,:payload)"),
                         {"id": MARKER, "payload": json.dumps({"legacy_ai": "pending_claim", "legacy_im": "disabled", "version": 1})})
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_run_owner_app_started ON app_regression_runs(owner_user_id,app_id,started_at,run_id)"))
    _ensure_column("users", "email", "email TEXT")
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT user_id, email, extra FROM users")).all()
        for user_id, email, extra in rows:
            payload = json.loads(extra or "{}") if isinstance(extra, str) else (extra or {})
            normalized = str(payload.get("email") or email or "").strip().lower() or None
            if normalized != email:
                conn.execute(text("UPDATE users SET email = :email WHERE user_id = :uid"),
                             {"email": normalized, "uid": user_id})
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email ON users(email)"))
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
        _ensure_column("nav_fsm_states", "meta", "meta JSON")
        _ensure_column("nav_flow_block_catalog", "key_ref", "key_ref TEXT DEFAULT ''")
        _ensure_column("catalog_entries", "ui_coverable", "ui_coverable INTEGER DEFAULT 1")
        _ensure_column("doc_sources", "source_kind", "source_kind TEXT DEFAULT 'upload'")
        _ensure_column("doc_sources", "source_url", "source_url TEXT DEFAULT ''")
        _ensure_column("doc_sources", "feishu_bot_id", "feishu_bot_id TEXT DEFAULT ''")
        _ensure_column("doc_sources", "auto_sync", "auto_sync INTEGER DEFAULT 0")
        _ensure_column("doc_sources", "sync_interval_sec", "sync_interval_sec INTEGER DEFAULT 3600")
        _ensure_column("assistant_mcp_tokens", "kind", "kind TEXT DEFAULT 'mcp'")
        _ensure_column("install_tokens", "install_id", "install_id TEXT DEFAULT ''")
        _ensure_column("install_tokens", "requested_node_id", "requested_node_id TEXT DEFAULT ''")
        _ensure_column("install_tokens", "node_id", "node_id TEXT DEFAULT ''")
        _ensure_column("install_tokens", "registered_at", "registered_at INTEGER DEFAULT 0")
        _ensure_column("install_tokens", "credential_digest", "credential_digest TEXT DEFAULT ''")
        _ensure_column("node_credentials", "revoked_at", "revoked_at INTEGER DEFAULT 0")
        with engine.connect() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_install_tokens_install_id ON install_tokens (install_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_project_cases_page ON project_cases(project_id, sort_index, pk)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_app_regression_runs_app_started ON app_regression_runs(app_id, started_at, run_id)"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_app_regression_runs_started "
                    "ON app_regression_runs(started_at)"
                )
            )
            conn.commit()
        from mino_nexus.services.doc_store import ensure_fts

        ensure_fts()
        # 历史密钥与账号文件保留；执行链路不再读取它们，不能在启动时清除用户数据。
    except Exception as exc:
        SLog.w(TAG, f"migration skipped: {exc}")
    SLog.i(TAG, "schema ready")
