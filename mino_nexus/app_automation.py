"""应用自动化配置：存在 app.env.automation 里。"""
from __future__ import annotations

import copy
import uuid
from typing import Any

from mino_nexus.json_store import load_json, save_json
from mino_nexus.project_env import (
    profile_keys,
    profile_snapshot,
    resolve_profile_name,
    target_id_from_snapshot,
)

_ICONS = "icon_targets.json"

DEFAULT_AUTOMATION: dict[str, Any] = {
    "env_profile": "test",
    "execution_env": {"mode": "fixed", "profile": "test"},
    "skills": {
        "default": {"pre": [], "post": []},
        "devices": {},
    },
    "icon_targets": [],
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

    out: dict[str, Any] = {
        "requirements": _rows("requirements"),
        "releases": _rows("releases"),
        "schedule": _rows("schedule"),
        "updated_at": str(raw.get("updated_at") or ""),
    }
    if isinstance(raw.get("cover_job"), dict):
        out["cover_job"] = raw["cover_job"]
    if isinstance(raw.get("draft_cases"), list):
        out["draft_cases"] = raw["draft_cases"]
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


def get_automation_config(app: dict) -> dict[str, Any]:
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
    icons = raw.get("icon_targets")
    if isinstance(icons, list):
        out["icon_targets"] = [x for x in icons if isinstance(x, dict)]
    out["suites"] = _normalize_suites(raw.get("suites"))
    out["qa_process"] = _normalize_qa_process(raw.get("qa_process"))
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
    from mino_nexus import project_store as ps

    current = get_automation_config(app)
    if config.get("env_profile"):
        current["env_profile"] = str(config["env_profile"]).strip() or "test"
    if "execution_env" in config and isinstance(config["execution_env"], dict):
        current["execution_env"] = config["execution_env"]
        if config["execution_env"].get("profile"):
            current["env_profile"] = config["execution_env"]["profile"]
    if "skills" in config and isinstance(config["skills"], dict):
        current["skills"] = config["skills"]
    if "icon_targets" in config and isinstance(config["icon_targets"], list):
        current["icon_targets"] = [x for x in config["icon_targets"] if isinstance(x, dict)]
    if "suites" in config:
        current["suites"] = _normalize_suites(config.get("suites"))
    if "qa_process" in config:
        current["qa_process"] = _normalize_qa_process(config.get("qa_process"))
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
    return current


def count_qa_process_cases_from_env(env: dict | None) -> int:
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


def list_app_cases(app: dict) -> list[dict[str, Any]]:
    qp = get_automation_config(app).get("qa_process") or {}
    rows: list[dict[str, Any]] = []
    for req in qp.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        title = str(req.get("title") or req.get("external_id") or "需求").strip() or "需求"
        for raw in req.get("draft_cases") or []:
            if not isinstance(raw, dict):
                continue
            cid = str(raw.get("case_id") or "").strip()
            if not cid:
                continue
            steps = raw.get("steps")
            expected = raw.get("expected")
            if isinstance(steps, list):
                steps_list = [str(x) for x in steps if str(x).strip()]
                steps_raw = str(raw.get("steps_raw") or "").strip() or "\n".join(
                    f"{i}. {x}" for i, x in enumerate(steps_list, 1)
                )
            else:
                steps_raw = str(raw.get("steps_raw") or steps or "")
                steps_list = [s.strip() for s in steps_raw.splitlines() if s.strip()]
            if isinstance(expected, list):
                expected_list = [str(x) for x in expected if str(x).strip()]
                expected_raw = str(raw.get("expected_raw") or "").strip() or "\n".join(
                    f"{i}. {x}" for i, x in enumerate(expected_list, 1)
                )
            else:
                expected_raw = str(raw.get("expected_raw") or expected or "")
                expected_list = [s.strip() for s in expected_raw.splitlines() if s.strip()]
            module = str(raw.get("module") or "").strip()
            rows.append({
                **raw,
                "case_id": cid,
                "name": str(raw.get("name") or raw.get("title") or cid),
                "module": f"本需求生成 / {title}" + (f" / {module}" if module else ""),
                "source": "generated",
                "requirement_id": str(req.get("id") or ""),
                "steps": steps_list,
                "expected": expected_list,
                "steps_raw": steps_raw,
                "expected_raw": expected_raw,
                "precondition": str(raw.get("precondition") or raw.get("pre") or ""),
                "platform": str(raw.get("platform") or ""),
            })
    return rows


def cases_payload(app: dict) -> dict[str, Any]:
    cases = list_app_cases(app)
    return {
        "cases": cases,
        "total": len(cases),
        "source": "qa_process",
        "from_cache": True,
        "synced_at": "",
        "resolve_note": "",
    }


def package_for_app(app: dict, env_profile: str | None = None, platform: str = "android") -> str:
    from mino_nexus import project_store as ps

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


def _icon_root() -> dict[str, Any]:
    raw = load_json(_ICONS, {})
    return raw if isinstance(raw, dict) else {}


def list_icon_targets(app_id: str, page: int = 1, page_size: int = 50, keyword: str = "") -> dict[str, Any]:
    items = [x for x in (_icon_root().get(app_id) or []) if isinstance(x, dict)]
    kw = str(keyword or "").strip().lower()
    if kw:
        items = [x for x in items if kw in str(x.get("name") or "").lower() or kw in " ".join(x.get("aliases") or []).lower()]
    total = len(items)
    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 50)))
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": total, "page": page, "page_size": page_size}


def icon_target_count(app_id: str) -> int:
    items = _icon_root().get(app_id) or []
    return len(items) if isinstance(items, list) else 0


def upsert_icon_target(app_id: str, body: dict[str, Any]) -> dict[str, Any]:
    root = _icon_root()
    rows = [x for x in (root.get(app_id) or []) if isinstance(x, dict)]
    tid = str(body.get("id") or "").strip() or uuid.uuid4().hex[:12]
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    row = {
        "id": tid,
        "name": name,
        "x": int(body.get("x") or 0),
        "y": int(body.get("y") or 0),
        "w": int(body.get("w") or 0),
        "h": int(body.get("h") or 0),
        "image_url": str(body.get("image_url") or ""),
        "aliases": [str(a) for a in (body.get("aliases") or []) if str(a).strip()][:24],
        "note": str(body.get("note") or "").strip()[:200],
        "component_uid": str(body.get("component_uid") or ""),
    }
    found = False
    for i, old in enumerate(rows):
        if str(old.get("id")) == tid:
            rows[i] = row
            found = True
            break
    if not found:
        rows.append(row)
    root[app_id] = rows
    save_json(_ICONS, root)
    return row


def delete_icon_target(app_id: str, target_id: str) -> bool:
    root = _icon_root()
    rows = [x for x in (root.get(app_id) or []) if isinstance(x, dict)]
    nxt = [x for x in rows if str(x.get("id")) != str(target_id)]
    if len(nxt) == len(rows):
        return False
    root[app_id] = nxt
    save_json(_ICONS, root)
    return True


def purge_app_extras(app_id: str) -> None:
    root = _icon_root()
    if app_id in root:
        del root[app_id]
        save_json(_ICONS, root)


def qa_process_summary() -> list[dict[str, Any]]:
    from mino_nexus import project_store as ps

    items = []
    for p in ps.list_projects():
        for a in p.get("apps") or []:
            app = ps.find_app(str(a.get("id") or ""))
            if not app:
                continue
            proc = get_automation_config(app).get("qa_process") or {}
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
    from mino_nexus import project_store as ps

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
            "icon_targets": icon_target_count(str(app.get("id") or "")),
            "case_count": len(cases),
            "feishu_cases": len(cases),
        },
    }
