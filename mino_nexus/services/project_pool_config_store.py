"""项目号池配置 SQLite 真源；从 projects.env 懒迁移。"""
from __future__ import annotations

import copy
from typing import Any

from mino_nexus.core.database import session_scope
from mino_nexus.models.project import Project
from mino_nexus.models.project_pool_config import ProjectPoolConfig

POOL_ENV_KEYS = (
    "account_facet_extensions",
    "account_template_ids",
    "account_pool_local",
)


def _empty_pool_local() -> dict[str, Any]:
    return {"templates": [], "extension_addons": {}}


def _norm_pool_local(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_pool_local()
    templates = [x for x in (raw.get("templates") or []) if isinstance(x, dict)]
    addons_raw = raw.get("extension_addons")
    addons: dict[str, list] = {}
    if isinstance(addons_raw, dict):
        for tid, rows in addons_raw.items():
            key = str(tid or "").strip()
            if key and isinstance(rows, list):
                addons[key] = [x for x in rows if isinstance(x, dict)]
    return {"templates": templates, "extension_addons": addons}


def pool_config_dict(row: ProjectPoolConfig | None) -> dict[str, Any]:
    if row is None:
        return {
            "account_facet_extensions": [],
            "account_template_ids": [],
            "account_pool_local": _empty_pool_local(),
        }
    ext = row.facet_extensions if isinstance(row.facet_extensions, list) else []
    tids = row.template_ids if isinstance(row.template_ids, list) else []
    return {
        "account_facet_extensions": [x for x in ext if isinstance(x, dict)],
        "account_template_ids": [str(x) for x in tids if str(x).strip()],
        "account_pool_local": _norm_pool_local(row.pool_local),
    }


def load_pool_config(project_id: str) -> dict[str, Any]:
    pid = str(project_id or "").strip()
    if not pid:
        return pool_config_dict(None)
    with session_scope() as db:
        row = db.query(ProjectPoolConfig).filter(ProjectPoolConfig.project_id == pid).one_or_none()
        return pool_config_dict(row)


def save_pool_config(project_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services.account_facet_schema import normalize_extensions
    from mino_nexus.services.project_env import _norm_template_ids

    pid = str(project_id or "").strip()
    if not pid:
        return pool_config_dict(None)
    with session_scope() as db:
        row = db.query(ProjectPoolConfig).filter(ProjectPoolConfig.project_id == pid).one_or_none()
        if row is None:
            row = ProjectPoolConfig(project_id=pid)
            db.add(row)
        if "account_facet_extensions" in patch:
            row.facet_extensions = normalize_extensions(patch.get("account_facet_extensions"))
        if "account_template_ids" in patch:
            row.template_ids = _norm_template_ids(patch.get("account_template_ids"))
        if "account_pool_local" in patch:
            row.pool_local = _norm_pool_local(patch.get("account_pool_local"))
        db.flush()
        return pool_config_dict(row)


def extract_pool_config_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    src = doc if isinstance(doc, dict) else {}
    out: dict[str, Any] = {}
    if "account_facet_extensions" in src:
        out["account_facet_extensions"] = src.get("account_facet_extensions")
    if "account_template_ids" in src:
        out["account_template_ids"] = src.get("account_template_ids")
    if "account_pool_local" in src:
        out["account_pool_local"] = src.get("account_pool_local")
    return out


def strip_pool_keys_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc or {})
    for key in POOL_ENV_KEYS:
        out.pop(key, None)
    out.pop("test_accounts", None)
    return out


def merge_pool_config_into_doc(doc: dict[str, Any], pool: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(doc or {})
    cfg = pool if isinstance(pool, dict) else {}
    out["account_facet_extensions"] = copy.deepcopy(cfg.get("account_facet_extensions") or [])
    out["account_template_ids"] = list(cfg.get("account_template_ids") or [])
    local = cfg.get("account_pool_local")
    out["account_pool_local"] = _norm_pool_local(local)
    out.pop("test_accounts", None)
    return out


def delete_pool_config(project_id: str) -> None:
    pid = str(project_id or "").strip()
    if not pid:
        return
    with session_scope() as db:
        db.query(ProjectPoolConfig).filter(ProjectPoolConfig.project_id == pid).delete(
            synchronize_session=False
        )


def ensure_migrated_from_project_env(project_id: str) -> bool:
    """若 projects.env 仍含号池配置，迁入 project_pool_config 并瘦身 env 列。"""
    pid = str(project_id or "").strip()
    if not pid:
        return False
    from mino_nexus.services.pool_account_store import merge_env_test_accounts

    with session_scope() as db:
        project = db.query(Project).filter(Project.id == pid).one_or_none()
        if project is None:
            return False
        env = copy.deepcopy(project.env) if isinstance(project.env, dict) else {}
        if not isinstance(env, dict):
            return False
        has_pool = any(k in env for k in POOL_ENV_KEYS)
        has_accounts_blob = "test_accounts" in env
        if not has_pool and not has_accounts_blob:
            return False

    if has_accounts_blob:
        merge_env_test_accounts(pid, env)

    with session_scope() as db:
        project = db.query(Project).filter(Project.id == pid).one_or_none()
        if project is None:
            return False
        env = copy.deepcopy(project.env) if isinstance(project.env, dict) else {}
        patch = extract_pool_config_from_doc(env)
        if patch:
            row = db.query(ProjectPoolConfig).filter(ProjectPoolConfig.project_id == pid).one_or_none()
            if row is None:
                row = ProjectPoolConfig(project_id=pid)
                db.add(row)
            from mino_nexus.services.account_facet_schema import normalize_extensions
            from mino_nexus.services.project_env import _norm_template_ids

            if "account_facet_extensions" in patch:
                row.facet_extensions = normalize_extensions(patch.get("account_facet_extensions"))
            if "account_template_ids" in patch:
                row.template_ids = _norm_template_ids(patch.get("account_template_ids"))
            if "account_pool_local" in patch:
                row.pool_local = _norm_pool_local(patch.get("account_pool_local"))

        slim = strip_pool_keys_from_doc(env)
        project.env = slim
        return True


def project_env_summary(env: dict[str, Any] | None) -> dict[str, Any]:
    doc = env if isinstance(env, dict) else {}
    environments = doc.get("environments") or []
    keys = [str(e.get("key") or "") for e in environments if isinstance(e, dict) and e.get("key")]
    return {
        "default_profile": str(doc.get("default_profile") or "test"),
        "environment_keys": keys,
        "pipeline": list(doc.get("pipeline") or []),
    }
