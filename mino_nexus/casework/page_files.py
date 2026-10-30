"""应用逻辑的页面真源：项目仓库 pages/*.yaml。没有这个目录时为空，不读库。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from mino_nexus.casework.script_workspace import workspace_root


def load_pages(project_id: str) -> dict[str, Any]:
    checkout = workspace_root(project_id) / "repo"
    folder = checkout / "pages"
    if not folder.is_dir():
        return {"pages": [], "controls": [], "entries": []}
    docs: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(folder.glob("*.yaml")):
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, dict):
            docs.append((path.stem, raw))
    mentions = _mentions(checkout / "tests", [str(doc.get("page_key") or stem) for stem, doc in docs])
    pages: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for stem, doc in docs:
        page_key = str(doc.get("page_key") or stem).strip() or stem
        if "/" in page_key or "\\" in page_key or page_key in {".", ".."}:
            continue
        label = str(doc.get("label") or page_key)
        anchors: list[str] = []
        for state in doc.get("states") or []:
            if not isinstance(state, dict):
                continue
            for anchor in state.get("anchors") or []:
                text = str(anchor or "").strip()
                if text and text not in anchors:
                    anchors.append(text)
        case_ids = mentions.get(page_key, [])
        pages.append({
            "state_id": page_key,
            "label": label,
            "page_key": page_key,
            "variant": "",
            "semantic_status": "repo",
            "revision": 0,
            "case_ref_count": len(case_ids),
            "case_ids": case_ids,
            "visit_count": 0,
            "anchors": anchors[:8],
            "reviews": [],
        })
        for control in doc.get("controls") or []:
            if not isinstance(control, dict):
                continue
            cid = str(control.get("id") or "").strip()
            if not cid:
                continue
            controls.append({
                "control_id": cid,
                "target_desc": str(control.get("label") or cid),
                "element_type": "button",
                "scope": "page",
                "page_state_id": page_key,
                "semantic_role": str(control.get("role") or "element"),
                "revision": 1,
                "publish_state": "repo",
                "sensitive": False,
                "locator_value": {},
                "locator_spec": control.get("locator_spec") if isinstance(control.get("locator_spec"), dict) else {},
                "updated_at": "",
                "updated_by": "",
            })
        for index, entry in enumerate(doc.get("entries") or []):
            if not isinstance(entry, dict):
                continue
            platforms = [str(item) for item in (entry.get("platforms") or []) if str(item or "").strip()]
            trigger = entry.get("trigger")
            trigger_text = trigger if isinstance(trigger, str) else ""
            entries.append({
                "entry_id": f"{page_key}.{index}",
                "from_state_id": str(entry.get("from") or ""),
                "target_state_id": page_key,
                "target_label": label,
                "platform": platforms[0] if len(platforms) == 1 else "",
                "platforms": platforms,
                "source_kind": "repo",
                "status": "active",
                "confidence": 1.0,
                "trigger": {"text": trigger_text} if trigger_text else {},
                "expect": {},
                "fingerprint_hash": "",
                "success_count": 0,
                "fail_count": 0,
                "conflict": False,
                "updated_at": 0,
            })
    return {"pages": pages, "controls": controls, "entries": entries}


def export_project(project_id: str) -> dict[str, Any]:
    """把库里的页面、控件定位和入口写成 pages/*.yaml。已有文件不覆盖。不写回数据库。"""
    checkout = workspace_root(project_id) / "repo"
    if not (checkout / ".git").is_dir():
        return {"project_id": project_id, "written": [], "reason": "没有检出"}
    grouped = _from_tables(project_id)
    folder = checkout / "pages"
    folder.mkdir(exist_ok=True)
    written: list[str] = []
    skipped: list[str] = []
    for page_key, doc in grouped.items():
        name = f"{page_key}.yaml"
        path = folder / name
        if path.exists():
            skipped.append(name)
            continue
        path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        written.append(name)
    return {"project_id": project_id, "written": written, "skipped": skipped}


def _mentions(tests: Path, page_keys: list[str]) -> dict[str, list[str]]:
    found = {key: [] for key in page_keys if key}
    if not tests.is_dir() or not found:
        return found
    for path in tests.rglob("*.py"):
        if not path.is_file() or path.name.endswith(tuple(f".{plat}.py" for plat in ("android", "ios", "web", "mac", "win"))):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        case_id = path.stem
        for key in found:
            if key and key in text and case_id not in found[key]:
                found[key].append(case_id)
    return found


def _from_tables(project_id: str) -> dict[str, dict[str, Any]]:
    from mino_nexus.casework.application_understanding import _page_anchors
    from mino_nexus.core.database import session_scope
    from mino_nexus.knowledge.control_registry import _public
    from mino_nexus.knowledge.page_semantics import analyze_clause, page_key
    from mino_nexus.models.nav_fsm import NavFsm, NavFsmState
    from mino_nexus.models.nav_page_entry import NavPageEntry
    from mino_nexus.models.ui_control import UiControl

    grouped: dict[str, dict[str, Any]] = {}
    state_to_key: dict[str, str] = {}
    with session_scope() as db:
        for fsm in db.query(NavFsm).filter_by(project_id=project_id).all():
            for row in db.query(NavFsmState).filter_by(fsm_id=fsm.id).all():
                meta = row.meta or {}
                if str(meta.get("semantic_status") or "") == "rejected":
                    continue
                sem = analyze_clause(str(meta.get("display_name") or ""))
                page_source = str(meta.get("page_source") or "")
                if page_source == "case_flow" and (not sem.page_label or sem.role in ("assertion", "flow")):
                    continue
                label = sem.page_label if page_source == "case_flow" and sem.page_label else (meta.get("display_name") or row.state_id)
                key = _file_key(str(meta.get("page_key") or "") or page_key(str(label or "")))
                if not key:
                    continue
                bucket = grouped.setdefault(key, {
                    "page_key": key,
                    "label": str(label or key),
                    "states": [],
                    "controls": [],
                    "entries": [],
                })
                anchors = _page_anchors(meta if isinstance(meta, dict) else {})
                state_id = str(row.state_id or "default")
                state_to_key[state_id] = key
                if not any(item.get("id") == state_id for item in bucket["states"]):
                    bucket["states"].append({"id": state_id, "anchors": anchors})
        controls = [_public(row) for row in db.query(UiControl).filter_by(project_id=project_id).all()]
        entry_rows = db.query(NavPageEntry).filter_by(project_id=project_id).all()
        entry_dump = [{
            "from_state_id": str(row.from_state_id or ""),
            "target_state_id": str(row.target_state_id or ""),
            "target_label": str(row.target_label or ""),
            "platform": str(row.platform or ""),
            "trigger": row.trigger if isinstance(row.trigger, dict) else {},
        } for row in entry_rows]
    seen_controls: set[tuple[str, str]] = set()
    for row in controls:
        key = state_to_key.get(str(row.get("page_state_id") or ""))
        if not key:
            continue
        cid = str(row.get("control_id") or "").strip()
        role = str(row.get("semantic_role") or "")
        if not cid or role != "element" or (key, cid) in seen_controls:
            continue
        seen_controls.add((key, cid))
        item = {
            "id": cid,
            "label": str(row.get("target_desc") or cid),
            "role": "element",
        }
        spec = row.get("locator_spec") if isinstance(row.get("locator_spec"), dict) else {}
        if spec:
            item["locator_spec"] = spec
        grouped[key]["controls"].append(item)
    for row in entry_dump:
        key = state_to_key.get(row["target_state_id"])
        if not key:
            for candidate, doc in grouped.items():
                if row["target_label"] and row["target_label"] == doc["label"]:
                    key = candidate
                    break
        if not key:
            continue
        trigger = row["trigger"]
        text = str(trigger.get("text") or trigger.get("kind") or "").strip()
        if not text:
            continue
        item = {
            "from": state_to_key.get(row["from_state_id"], row["from_state_id"]),
            "trigger": text,
        }
        if row["platform"]:
            item["platforms"] = [row["platform"]]
        existing = grouped[key]["entries"]
        if item not in existing:
            existing.append(item)
    return grouped


def _file_key(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or "/" in text or text in {".", ".."} or text.endswith(".yaml"):
        return ""
    return text
