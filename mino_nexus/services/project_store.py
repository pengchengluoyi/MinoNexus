"""项目 / 应用 JSON 库。契约对齐上游 `rProject` + `models/project`。"""
from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from mino_nexus.services.project_env import (
    account_ident as account_ident_of,
    default_project_env,
    env_from_project_row,
    list_test_accounts,
    normalize_project_env,
)



def _now() -> int:
    return int(time.time())


def _owner_from(actor: dict[str, Any] | None) -> dict[str, str]:
    row = actor if isinstance(actor, dict) else {}
    uid = str(row.get("user_id") or row.get("created_by") or row.get("id") or "").strip()
    name = str(row.get("name") or row.get("created_by_name") or row.get("username") or "").strip()
    if uid:
        return {"created_by": uid, "created_by_name": name}
    from mino_nexus.services.auth_store import seed_admin_user

    admin = seed_admin_user()
    return {
        "created_by": str(admin.get("user_id") or ""),
        "created_by_name": str(admin.get("name") or admin.get("username") or "管理员"),
    }


def _default_owner() -> dict[str, str]:
    """导入 / 旧数据缺创建人：回填 Nexus 种子账号 `admin`，避免 UI 看起来无主。"""
    return _owner_from(None)


def _ensure_ownership(root: dict[str, Any]) -> bool:
    dirty = False
    fallback: dict[str, str] | None = None
    now = _now()

    def owner() -> dict[str, str]:
        nonlocal fallback
        if fallback is None:
            fallback = _default_owner()
        return fallback

    for bucket in ("projects", "apps"):
        for row in root[bucket]:
            if not isinstance(row, dict):
                continue
            if not str(row.get("created_by") or "").strip():
                row.update(owner())
                dirty = True
            elif not str(row.get("created_by_name") or "").strip():
                from mino_nexus.services.auth_store import public_user_by_id

                pub = public_user_by_id(str(row.get("created_by") or ""))
                row["created_by_name"] = str((pub or {}).get("name") or owner()["created_by_name"])
                dirty = True
            if row.get("created_at") in (None, ""):
                row["created_at"] = now
                dirty = True
            if row.get("updated_at") in (None, ""):
                row["updated_at"] = row.get("created_at") or now
                dirty = True
    return dirty


def _project_dict(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "uid": row.uid,
        "name": row.name,
        "description": row.description,
        "env": row.env if isinstance(row.env, dict) else {},
        "created_by": row.created_by,
        "created_by_name": row.created_by_name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _app_dict(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "uid": row.uid,
        "name": row.name,
        "description": row.description,
        "platforms": row.platforms,
        "env": row.env if isinstance(row.env, dict) else {},
        "project_id": row.project_id,
        "created_by": row.created_by,
        "created_by_name": row.created_by_name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _root() -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.project import App, Project

    ensure_db()
    db = SessionLocal()
    try:
        raw = {
            "projects": [_project_dict(p) for p in db.query(Project).all()],
            "apps": [_app_dict(a) for a in db.query(App).all()],
        }
    finally:
        db.close()
    if _ensure_ownership(raw):
        _save(raw)
    return raw


def _project_row(row: dict) -> Any:
    from mino_nexus.models.project import Project

    return Project(
        id=str(row["id"]),
        uid=str(row.get("uid") or ""),
        name=str(row.get("name") or row["id"]),
        description=str(row.get("description") or ""),
        env=row.get("env") if isinstance(row.get("env"), dict) else {},
        created_by=str(row.get("created_by") or ""),
        created_by_name=str(row.get("created_by_name") or ""),
        created_at=int(row.get("created_at") or 0),
        updated_at=int(row.get("updated_at") or 0),
    )


def _app_row(row: dict) -> Any:
    from mino_nexus.models.project import App

    return App(
        id=str(row["id"]),
        uid=str(row.get("uid") or ""),
        name=str(row.get("name") or row["id"]),
        description=str(row.get("description") or ""),
        platforms=str(row.get("platforms") or ""),
        env=row.get("env") if isinstance(row.get("env"), dict) else {},
        project_id=str(row.get("project_id") or ""),
        created_by=str(row.get("created_by") or ""),
        created_by_name=str(row.get("created_by_name") or ""),
        created_at=int(row.get("created_at") or 0),
        updated_at=int(row.get("updated_at") or 0),
    )


def _save(root: dict[str, Any]) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.project import App, Project

    with session_scope() as db:
        keep_p = set()
        for row in root.get("projects") or []:
            if isinstance(row, dict) and row.get("id"):
                db.merge(_project_row(row))
                keep_p.add(str(row["id"]))
        for row in db.query(Project).all():
            if row.id not in keep_p:
                db.delete(row)
        keep_a = set()
        for row in root.get("apps") or []:
            if isinstance(row, dict) and row.get("id"):
                db.merge(_app_row(row))
                keep_a.add(str(row["id"]))
        for row in db.query(App).all():
            if row.id not in keep_a:
                db.delete(row)


def _apps_of(root: dict[str, Any], project_id: str) -> list[dict[str, Any]]:
    return [a for a in root["apps"] if isinstance(a, dict) and a.get("project_id") == project_id]


def _owner_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_by": str(row.get("created_by") or ""),
        "created_by_name": str(row.get("created_by_name") or ""),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _knowledge_count(app_id: str) -> int:
    from mino_nexus.services.knowledge_store import count_for_app

    return count_for_app(app_id)


def _qa_summary(env: dict[str, Any]) -> dict[str, Any]:
    auto = env.get("automation") if isinstance(env.get("automation"), dict) else {}
    qp = auto.get("qa_process") if isinstance(auto.get("qa_process"), dict) else {}
    reqs = qp.get("requirements") if isinstance(qp.get("requirements"), list) else []
    rels = qp.get("releases") if isinstance(qp.get("releases"), list) else []
    return {
        "requirement_count": sum(1 for x in reqs if isinstance(x, dict)),
        "release_count": sum(1 for x in rels if isinstance(x, dict)),
        "updated_at": str(qp.get("updated_at") or auto.get("updated_at") or ""),
        "env_profile": str(auto.get("env_profile") or ""),
    }


def _env_targets(app: dict[str, Any]) -> dict[str, str]:
    from mino_nexus.services.app_automation import package_for_app

    out = {"package": "", "bundle": "", "web_url": ""}
    try:
        out["package"] = str(package_for_app(app, platform="android") or "")
    except Exception:
        pass
    try:
        out["bundle"] = str(package_for_app(app, platform="ios") or "")
    except Exception:
        pass
    try:
        out["web_url"] = str(package_for_app(app, platform="web") or "")
    except Exception:
        pass
    return out


def _public_app(a: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    from mino_nexus.services.app_automation import count_qa_process_cases_from_env

    env = a.get("env") if isinstance(a.get("env"), dict) else {}
    aid = str(a.get("id") or "")
    stats = {
        "case_count": count_qa_process_cases_from_env(env, app_id=aid),
        "feishu_cases": count_qa_process_cases_from_env(env, app_id=aid),
        "knowledge_count": _knowledge_count(aid),
    }
    row = {
        "id": a.get("id"),
        "uid": a.get("uid"),
        "name": a.get("name"),
        "description": a.get("description"),
        "platforms": a.get("platforms"),
        "project_id": a.get("project_id"),
        "automation_stats": stats,
        **_owner_public(a),
    }
    if detail:
        row["env"] = env
        row["qa_process"] = _qa_summary(env)
        row.update(_env_targets(a))
    return row


def find_project(project_id: str) -> dict[str, Any] | None:
    root = _root()
    for p in root["projects"]:
        if isinstance(p, dict) and p.get("id") == project_id:
            return p
    return None


def find_app(app_id: str) -> dict[str, Any] | None:
    root = _root()
    for a in root["apps"]:
        if isinstance(a, dict) and a.get("id") == app_id:
            return a
    return None


def require_project(project_id: str) -> dict[str, Any]:
    row = find_project(project_id)
    if not row:
        raise KeyError("Project not found")
    return row


def require_app(app_id: str) -> dict[str, Any]:
    row = find_app(app_id)
    if not row:
        raise KeyError("App not found")
    return row


def project_env(project_id: str) -> dict[str, Any]:
    root = _root()
    project = require_project(project_id)
    return env_from_project_row(project, _apps_of(root, project_id))


def list_projects() -> list[dict[str, Any]]:
    root = _root()
    out = []
    for p in root["projects"]:
        if not isinstance(p, dict):
            continue
        apps_out = [_public_app(a) for a in _apps_of(root, str(p.get("id") or ""))]
        case_count = sum(int((a.get("automation_stats") or {}).get("case_count") or 0) for a in apps_out)
        knowledge_count = sum(int((a.get("automation_stats") or {}).get("knowledge_count") or 0) for a in apps_out)
        out.append(
            {
                "id": p.get("id"),
                "uid": p.get("uid"),
                "name": p.get("name"),
                "description": p.get("description"),
                "env": p.get("env") if isinstance(p.get("env"), dict) else {},
                "apps": apps_out,
                "app_count": len(apps_out),
                "case_count": case_count,
                "knowledge_count": knowledge_count,
                **_owner_public(p),
            }
        )
    return out


def create_project(name: str, description: str = "", owner: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _root()
    now = _now()
    row = {
        "id": str(uuid.uuid4()),
        "uid": str(uuid.uuid4()),
        "name": str(name or "").strip() or "未命名项目",
        "description": str(description or ""),
        "env": default_project_env(),
        **_owner_from(owner),
        "created_at": now,
        "updated_at": now,
    }
    root["projects"].append(row)
    _save(root)
    return copy.deepcopy(row)


def create_app(
    project_id: str,
    *,
    name: str,
    description: str = "",
    platforms: str = "",
    env: dict | None = None,
    owner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_project(project_id)
    root = _root()
    now = _now()
    row = {
        "id": str(uuid.uuid4()),
        "uid": str(uuid.uuid4()),
        "name": str(name or "").strip() or "未命名应用",
        "description": str(description or ""),
        "platforms": str(platforms or ""),
        "env": env if isinstance(env, dict) else {},
        "project_id": project_id,
        **_owner_from(owner),
        "created_at": now,
        "updated_at": now,
    }
    root["apps"].append(row)
    _save(root)
    return copy.deepcopy(row)


def get_app_detail(app_id: str) -> dict[str, Any]:
    app = require_app(app_id)
    project = find_project(str(app.get("project_id") or ""))
    row = _public_app(app, detail=True)
    row["project_name"] = (project or {}).get("name")
    row["project_uid"] = (project or {}).get("uid")
    if project:
        row["project_created_by"] = project.get("created_by")
        row["project_created_by_name"] = project.get("created_by_name")
    return row


def update_app_env(app_id: str, env: dict) -> dict[str, Any]:
    """旧接口：把内容写进所属项目的 test 环境。"""
    app = require_app(app_id)
    pid = str(app.get("project_id") or "")
    project = require_project(pid)
    root = _root()
    doc = normalize_project_env(project.get("env") or default_project_env())
    doc["profiles"]["test"] = normalize_project_env(env or {})["profiles"]["test"]
    for p in root["projects"]:
        if p.get("id") == pid:
            p["env"] = doc
            p["updated_at"] = _now()
            break
    _save(root)
    return doc


def save_project_env(project_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    require_project(project_id)
    root = _root()
    for p in root["projects"]:
        if p.get("id") == project_id:
            p["env"] = doc
            p["updated_at"] = _now()
            break
    _save(root)
    return doc


def update_app_record(app_id: str, **fields: Any) -> dict[str, Any]:
    require_app(app_id)
    root = _root()
    for a in root["apps"]:
        if a.get("id") == app_id:
            a.update({k: v for k, v in fields.items() if v is not None})
            a["updated_at"] = _now()
            _save(root)
            return copy.deepcopy(a)
    raise KeyError("App not found")


def delete_app(app_id: str) -> dict[str, Any]:
    app = require_app(app_id)
    name = app.get("name")
    project_id = app.get("project_id")
    root = _root()
    root["apps"] = [a for a in root["apps"] if a.get("id") != app_id]
    _save(root)
    return {"id": app_id, "name": name, "project_id": project_id}


def delete_project(project_id: str) -> dict[str, Any]:
    project = require_project(project_id)
    name = project.get("name")
    root = _root()
    apps = _apps_of(root, project_id)
    app_ids = [str(a.get("id")) for a in apps]
    app_names = [a.get("name") for a in apps]
    root["apps"] = [a for a in root["apps"] if a.get("project_id") != project_id]
    root["projects"] = [p for p in root["projects"] if p.get("id") != project_id]
    _save(root)
    return {"id": project_id, "name": name, "app_ids": app_ids, "app_names": app_names}


def find_test_account(
    *,
    account_id: str = "",
    account_ident: str = "",
    project_id: str = "",
    app_id: str = "",
) -> dict[str, Any] | None:
    """在项目号池里找被测产品的登录账号，不是 Mino 管理员。"""
    want_id = str(account_id or "").strip()
    want_ident = str(account_ident or "").strip()
    if not want_id and not want_ident:
        return None
    pids: list[str] = []
    if str(project_id or "").strip():
        pids.append(str(project_id).strip())
    elif str(app_id or "").strip():
        app = find_app(str(app_id).strip())
        if app and app.get("project_id"):
            pids.append(str(app.get("project_id")))
    else:
        root = _root()
        pids = [str(p.get("id") or "") for p in root["projects"] if isinstance(p, dict) and p.get("id")]
    seen: set[str] = set()
    for pid in pids:
        if not pid or pid in seen:
            continue
        seen.add(pid)
        try:
            doc = project_env(pid)
        except KeyError:
            continue
        for row in list_test_accounts(doc):
            ident = account_ident_of(row)
            if want_id and str(row.get("id") or "") == want_id:
                return {**row, "project_id": pid, "account_ident": ident}
            if want_ident and ident == want_ident:
                return {**row, "project_id": pid, "account_ident": ident}
    return None
