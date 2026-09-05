"""应用自动化配置：存在 app.env.automation 里。"""
from __future__ import annotations

import copy
import uuid
from typing import Any

from mino_nexus.services.project_env import (
    profile_keys,
    profile_snapshot,
    resolve_profile_name,
    target_id_from_snapshot,
)

DEFAULT_AUTOMATION: dict[str, Any] = {
    "env_profile": "test",
    "execution_env": {"mode": "fixed", "profile": "test"},
    "skills": {
        "default": {"pre": [], "post": []},
        "devices": {},
    },
    "suites": [],
    "qa_process": {
        "requirements": [],
        "releases": [],
        "schedule": [],
        "workflow": None,
        "features": [],
        "app_atlas": {"modules": [], "updated_at": ""},
        "atlas_patches": [],
        "autonomy": {
            "enabled": True,
            "auto_analyze": True,
            "auto_mindmap": True,
            "auto_cases": True,
            "auto_atlas": True,
            "auto_dispatch": False,
        },
        "role_log": [],
        "updated_at": "",
    },
    "playbook": {},
}


def _normalize_suites(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        ids = [str(x).strip() for x in (item.get("case_ids") or []) if str(x).strip()]
        if not name or not ids:
            continue
        out.append({
            "id": str(item.get("id") or uuid.uuid4().hex[:10]),
            "name": name[:80],
            "case_ids": ids,
            "updated_at": str(item.get("updated_at") or ""),
        })
    return out


def _normalize_qa_process(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULT_AUTOMATION["qa_process"])

    def _rows(key: str) -> list[dict[str, Any]]:
        items = raw.get(key)
        if not isinstance(items, list):
            return []
        return [x for x in items if isinstance(x, dict) and str(x.get("id") or "").strip()]

    def _reqs() -> list[dict[str, Any]]:
        rows = []
        for req in _rows("requirements"):
            item = dict(req)
            item.pop("draft_cases", None)
            rows.append(item)
        return rows

    out: dict[str, Any] = {
        "requirements": _reqs(),
        "releases": _rows("releases"),
        "schedule": _rows("schedule"),
        "updated_at": str(raw.get("updated_at") or ""),
    }
    if isinstance(raw.get("cover_job"), dict):
        out["cover_job"] = raw["cover_job"]
    wf = raw.get("workflow")
    if isinstance(wf, dict):
        out["workflow"] = wf
    feats = raw.get("features")
    if isinstance(feats, list):
        out["features"] = [x for x in feats if isinstance(x, dict)]
    atlas = raw.get("app_atlas")
    out["app_atlas"] = atlas if isinstance(atlas, dict) else {"modules": [], "updated_at": ""}
    patches = raw.get("atlas_patches")
    out["atlas_patches"] = [x for x in patches if isinstance(x, dict)] if isinstance(patches, list) else []
    auto = raw.get("autonomy")
    if isinstance(auto, dict):
        out["autonomy"] = auto
    log = raw.get("role_log")
    if isinstance(log, list):
        out["role_log"] = [x for x in log if isinstance(x, dict)][-80:]
    return out


def _app_env(app: dict) -> dict[str, Any]:
    return dict(app.get("env") or {}) if isinstance(app.get("env"), dict) else {}


def _attach_draft_cases(app_id: str, qp: dict[str, Any]) -> dict[str, Any]:
    """读路径：把 app_cases 填回 requirements[].draft_cases，给 Studio 用例库用。"""
    from mino_nexus.services.case_store import list_cases

    qp = dict(qp or {})
    reqs = [dict(r) for r in (qp.get("requirements") or []) if isinstance(r, dict)]
    aid = str(app_id or "").strip()
    if not aid:
        qp["requirements"] = reqs
        return qp
    by_req: dict[str, list[dict[str, Any]]] = {}
    for row in list_cases(aid):
        by_req.setdefault(str(row.get("requirement_id") or ""), []).append(row)
    for item in reqs:
        item["draft_cases"] = by_req.get(str(item.get("id") or ""), [])
    qp["requirements"] = reqs
    return qp


def _sync_draft_cases_to_store(app_id: str, qp: dict[str, Any]) -> None:
    """写路径：请求里带了 draft_cases 才回写 sqlite；缺这个 key 表示没动用例，不覆盖。"""
    from mino_nexus.services.case_store import upsert_cases

    aid = str(app_id or "").strip()
    if not aid or not isinstance(qp, dict):
        return
    for req in qp.get("requirements") or []:
        if not isinstance(req, dict) or "draft_cases" not in req:
            continue
        rid = str(req.get("id") or "").strip()
        rows = [x for x in (req.get("draft_cases") or []) if isinstance(x, dict)]
        upsert_cases(aid, rid, rows, replace=True)


def get_automation_config(app: dict, *, hydrate_cases: bool = True) -> dict[str, Any]:
    env = _app_env(app)
    raw = env.get("automation") if isinstance(env.get("automation"), dict) else {}
    out = copy.deepcopy(DEFAULT_AUTOMATION)
    if raw.get("env_profile"):
        out["env_profile"] = raw["env_profile"]
    ex = raw.get("execution_env")
    if isinstance(ex, dict):
        out["execution_env"] = {
            "mode": ex.get("mode") or "fixed",
            "profile": ex.get("profile") or out["env_profile"],
        }
    skills = raw.get("skills") if isinstance(raw.get("skills"), dict) else {}
    if isinstance(skills.get("default"), dict):
        out["skills"]["default"] = {
            "pre": list(skills["default"].get("pre") or []),
            "post": list(skills["default"].get("post") or []),
        }
    devices = skills.get("devices") if isinstance(skills.get("devices"), dict) else {}
    out["skills"]["devices"] = {
        k: {
            "pre": list((v or {}).get("pre") or []),
            "post": list((v or {}).get("post") or []),
        }
        for k, v in devices.items()
        if isinstance(v, dict)
    }
    out["suites"] = _normalize_suites(raw.get("suites"))
    out["qa_process"] = _normalize_qa_process(raw.get("qa_process"))
    if hydrate_cases:
        out["qa_process"] = _attach_draft_cases(str(app.get("id") or ""), out["qa_process"])
    figma = raw.get("figma")
    if isinstance(figma, dict):
        out["figma"] = {
            "file_url": (figma.get("file_url") or "").strip(),
            "file_key": (figma.get("file_key") or "").strip(),
            "last_sync_at": figma.get("last_sync_at") or "",
            "pages_summary": figma.get("pages_summary") or [],
            "logic": figma.get("logic") if isinstance(figma.get("logic"), dict) else {},
            "logic_applied_at": figma.get("logic_applied_at") or "",
            "login_frame": figma.get("login_frame") if isinstance(figma.get("login_frame"), dict) else {},
            "login_reference": figma.get("login_reference") if isinstance(figma.get("login_reference"), dict) else {},
        }
    pb = raw.get("playbook")
    out["playbook"] = pb if isinstance(pb, dict) else {}
    return out


def save_automation_config(app: dict, config: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services import project_store as ps

    current = get_automation_config(app, hydrate_cases=False)
    if config.get("env_profile"):
        current["env_profile"] = str(config["env_profile"]).strip() or "test"
    if "execution_env" in config and isinstance(config["execution_env"], dict):
        current["execution_env"] = config["execution_env"]
        if config["execution_env"].get("profile"):
            current["env_profile"] = config["execution_env"]["profile"]
    if "skills" in config and isinstance(config["skills"], dict):
        current["skills"] = config["skills"]
    if "suites" in config:
        current["suites"] = _normalize_suites(config.get("suites"))
    if "qa_process" in config:
        incoming = config.get("qa_process") if isinstance(config.get("qa_process"), dict) else {}
        _sync_draft_cases_to_store(str(app.get("id") or ""), incoming)
        current["qa_process"] = _normalize_qa_process(incoming)
    if "figma" in config and isinstance(config["figma"], dict):
        prev = current.get("figma") or {}
        incoming = config["figma"]
        current["figma"] = {
            "file_url": (incoming.get("file_url") or "").strip(),
            "file_key": (incoming.get("file_key") or "").strip(),
            "last_sync_at": incoming.get("last_sync_at") or prev.get("last_sync_at") or "",
            "pages_summary": incoming.get("pages_summary") or prev.get("pages_summary") or [],
            "logic": incoming.get("logic") if isinstance(incoming.get("logic"), dict) else (prev.get("logic") or {}),
            "logic_applied_at": incoming.get("logic_applied_at") or prev.get("logic_applied_at") or "",
            "login_frame": incoming.get("login_frame") if isinstance(incoming.get("login_frame"), dict) else (prev.get("login_frame") or {}),
            "login_reference": incoming.get("login_reference") if isinstance(incoming.get("login_reference"), dict) else (prev.get("login_reference") or {}),
        }
    if "playbook" in config and isinstance(config.get("playbook"), dict):
        current["playbook"] = config["playbook"]
    env = _app_env(app)
    env["automation"] = current
    ps.update_app_record(str(app["id"]), env=env)
    out = copy.deepcopy(current)
    out["qa_process"] = _attach_draft_cases(str(app.get("id") or ""), out["qa_process"])
    return out


def count_qa_process_cases_from_env(env: dict | None, app_id: str = "") -> int:
    from mino_nexus.services.case_store import count_cases

    aid = str(app_id or "").strip()
    if aid:
        n = count_cases(aid)
        if n:
            return n
    env = env if isinstance(env, dict) else {}
    auto = env.get("automation") if isinstance(env.get("automation"), dict) else {}
    qp = auto.get("qa_process") if isinstance(auto.get("qa_process"), dict) else {}
    n = 0
    for req in qp.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        drafts = req.get("draft_cases") or []
        if not isinstance(drafts, list):
            continue
        n += sum(1 for x in drafts if isinstance(x, dict) and str(x.get("case_id") or "").strip())
    return n


def _raw_env_has_draft_cases(app: dict) -> bool:
    env = app.get("env") if isinstance(app.get("env"), dict) else {}
    auto = env.get("automation") if isinstance(env.get("automation"), dict) else {}
    qp = auto.get("qa_process") if isinstance(auto.get("qa_process"), dict) else {}
    if isinstance(qp.get("draft_cases"), list) and qp.get("draft_cases"):
        return True
    for req in qp.get("requirements") or []:
        if isinstance(req, dict) and isinstance(req.get("draft_cases"), list) and req.get("draft_cases"):
            return True
    return False


def list_app_cases(app: dict) -> list[dict[str, Any]]:
    from mino_nexus.services.case_store import list_cases, promote_from_env

    aid = str(app.get("id") or "").strip()
    moved = promote_from_env(app)
    if moved or _raw_env_has_draft_cases(app):
        qp = get_automation_config(app, hydrate_cases=False).get("qa_process") or {}
        save_automation_config(app, {"qa_process": qp})
    qp = get_automation_config(app, hydrate_cases=False).get("qa_process") or {}
    titles = {}
    for req in qp.get("requirements") or []:
        if isinstance(req, dict) and req.get("id"):
            titles[str(req["id"])] = str(req.get("title") or req.get("external_id") or "需求").strip() or "需求"
    rows: list[dict[str, Any]] = []
    for raw in list_cases(aid):
        title = titles.get(str(raw.get("requirement_id") or ""), "需求")
        module = str(raw.get("module") or "").strip()
        if not module.startswith("本需求生成"):
            module = f"本需求生成 / {title}" + (f" / {module}" if module else "")
        rows.append({**raw, "module": module})
    return rows


def cases_payload(app: dict) -> dict[str, Any]:
    cases = list_app_cases(app)
    return {
        "cases": cases,
        "total": len(cases),
        "source": "app_cases",
        "from_cache": True,
        "synced_at": "",
        "resolve_note": "",
    }


def package_for_app(app: dict, env_profile: str | None = None, platform: str = "android") -> str:
    from mino_nexus.services import project_store as ps

    plat = str(platform or "android").lower()
    pid = str(app.get("project_id") or "")
    try:
        env_doc = ps.project_env(pid) if pid else {"default_profile": "test", "profiles": {}}
    except KeyError:
        env_doc = {"default_profile": "test", "profiles": {}}
    cfg = get_automation_config(app)
    profile = env_profile or cfg.get("env_profile")
    name = resolve_profile_name(env_doc, profile)
    snap = profile_snapshot(env_doc, name)
    want = "web" if plat in ("web", "browser", "playwright") else ("ios" if plat in ("ios", "iphone", "ipad") else "android")
    return target_id_from_snapshot(snap, want)


def get_playbook(app: dict) -> dict[str, Any]:
    pb = get_automation_config(app).get("playbook")
    return pb if isinstance(pb, dict) else {}


def save_playbook(app: dict, playbook: dict[str, Any]) -> dict[str, Any]:
    cfg = save_automation_config(app, {"playbook": playbook if isinstance(playbook, dict) else {}})
    return cfg.get("playbook") or {}


def qa_process_summary() -> list[dict[str, Any]]:
    from mino_nexus.services import project_store as ps

    items = []
    for p in ps.list_projects():
        for a in p.get("apps") or []:
            app = ps.find_app(str(a.get("id") or ""))
            if not app:
                continue
            proc = get_automation_config(app, hydrate_cases=False).get("qa_process") or {}
            items.append({
                "app_id": app.get("id"),
                "app_name": app.get("name") or "",
                "project_id": app.get("project_id") or "",
                "project_name": p.get("name") or "",
                "requirements": proc.get("requirements") or [],
                "releases": proc.get("releases") or [],
                "schedule": proc.get("schedule") or [],
                "workflow": proc.get("workflow") or None,
            })
    return items


def config_payload(app: dict) -> dict[str, Any]:
    from mino_nexus.services import project_store as ps

    cfg = get_automation_config(app)
    pid = str(app.get("project_id") or "")
    try:
        env_doc = ps.project_env(pid) if pid else None
    except KeyError:
        env_doc = None
    project = ps.find_project(pid) if pid else None
    cases = list_app_cases(app)
    return {
        "app_id": app.get("id"),
        "app_name": app.get("name"),
        "project_name": (project or {}).get("name") or "",
        "env_profile": cfg.get("env_profile"),
        "env_profiles": profile_keys(env_doc) if env_doc is not None else ["test", "pre", "prod"],
        "package": package_for_app(app),
        "automation": cfg,
        "stats": {
            "case_count": len(cases),
            "feishu_cases": len(cases),
        },
    }
