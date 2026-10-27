"""页面/控件语义清理：把旧 case_flow 长句降级为页面状态或缺口。

只处理 `meta.page_source == "case_flow"` 的记录；采集页、人工页不动。
默认 dry-run，只有显式 apply 才写库。删除不是策略：错误页标
`semantic_status=rejected`，旧控件保留但 Workbench 不再把它当元素。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.knowledge.page_semantics import analyze_clause, page_key


def _states(db, app_id: str):
    from mino_nexus.models.nav_fsm import NavFsm, NavFsmState

    fsm = db.query(NavFsm).filter_by(app_id=app_id, version="v1").one_or_none()
    if fsm is None:
        return None, []
    return fsm, db.query(NavFsmState).filter_by(fsm_id=fsm.id).all()


def _plan(db, app_id: str) -> dict[str, Any]:
    fsm, states = _states(db, app_id)
    if fsm is None:
        return {"app_id": app_id, "version": "", "items": [], "summary": {"total": 0, "keep": 0, "rename": 0, "reject": 0, "merge": 0}}
    captured: dict[str, NavFsmState] = {}
    case_states: list[NavFsmState] = []
    for row in states:
        meta = row.meta or {}
        name = str(meta.get("display_name") or "").strip()
        if not name or str(meta.get("semantic_status") or "") == "rejected":
            continue
        if str(meta.get("page_source") or "") == "case_flow":
            case_states.append(row)
            continue
        key = str(meta.get("page_key") or "") or page_key(name)
        captured.setdefault(key, row)
    kept: dict[tuple[str, str], NavFsmState] = {}
    items: list[dict[str, Any]] = []
    for row in case_states:
        meta = row.meta or {}
        name = str(meta.get("display_name") or "").strip()
        sem = analyze_clause(name)
        # 已修复过的状态可能 display_name 已经是短名，业务状态保存在
        # meta.variant；不能用重新分析短名得到的空 variant 覆盖它。
        variant = str(meta.get("variant") or sem.variant or "")
        item: dict[str, Any] = {
            "state_id": row.state_id,
            "display_name": name,
            "page_label": sem.page_label,
            "variant": variant,
            "role": sem.role,
            "confidence": sem.confidence,
        }
        if not sem.page_label or sem.role in ("assertion", "flow") or sem.confidence < 0.6:
            item.update(action="reject", reason="不是稳定页面（动作/断言/流程）")
            items.append(item)
            continue
        key = page_key(sem.page_label)
        target = captured.get(key)
        duplicate = kept.get((key, variant))
        if duplicate is not None and duplicate.state_id != row.state_id:
            item.update(action="merge", target_state_id=duplicate.state_id, page_key=key, reason="同一页面同一状态重复")
        elif target is not None:
            item.update(action="attach", target_state_id=target.state_id, page_key=key, reason="已有采集页，挂为同一页面的状态")
        elif name != sem.page_label:
            item.update(action="rename", page_key=key, reason="去掉动作/状态修饰，保留页面短名")
        else:
            item.update(action="keep", page_key=key, reason="已是规范页面名")
        kept.setdefault((key, variant), row)
        items.append(item)
    summary = {"total": len(items), "keep": 0, "rename": 0, "attach": 0, "merge": 0, "reject": 0}
    for item in items:
        summary[str(item.get("action") or "keep")] = summary.get(str(item.get("action") or "keep"), 0) + 1
    return {"app_id": app_id, "version": fsm.version, "items": items, "summary": summary}


def audit(project_id: str, app_id: str) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope

    with session_scope() as db:
        return _plan(db, app_id)


def _remap_controls(db, old_state: str, target_state: str) -> None:
    from mino_nexus.models.ui_control import UiControl

    rows = db.query(UiControl).all()
    for row in rows:
        locator = dict(row.locator_value or {})
        if str(locator.get("nav_state_id") or "") != old_state:
            continue
        if target_state:
            locator["nav_state_id"] = target_state
            locator["nav_state_source"] = "semantic_merge"
        else:
            locator.pop("nav_state_id", None)
            locator.pop("nav_state_source", None)
            locator["semantic_unbound"] = True
        row.locator_value = locator
        row.revision = int(row.revision or 1) + 1


def _remap_edges(db, fsm_id: int, old_state: str, target_state: str) -> None:
    if not target_state:
        return
    from mino_nexus.models.nav_fsm import NavFsmEdge

    rows = db.query(NavFsmEdge).filter_by(fsm_id=fsm_id).all()
    for row in rows:
        changed = False
        if row.from_state == old_state:
            row.from_state = target_state
            changed = True
        if row.to_state == old_state:
            row.to_state = target_state
            changed = True
        if changed and row.from_state == row.to_state:
            row.kind = "recover"


def _remap_refs(db, app_id: str, old_state: str, target_state: str) -> None:
    from mino_nexus.models.nav_case_page_ref import NavCasePageRef
    from mino_nexus.models.nav_page_entry import NavPageEntry

    for row in db.query(NavCasePageRef).filter_by(app_id=app_id, page_state_id=old_state).all():
        if target_state:
            row.page_state_id = target_state
        else:
            db.delete(row)
    for row in db.query(NavPageEntry).filter_by(app_id=app_id, target_state_id=old_state).all():
        if target_state:
            row.target_state_id = target_state
        else:
            row.target_state_id = ""
            row.status = "stale"


def repair(project_id: str, app_id: str, *, apply: bool = False) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.nav_fsm import NavFsmState

    with session_scope() as db:
        fsm, _ = _states(db, app_id)
        plan = _plan(db, app_id)
        if not apply or fsm is None:
            return {**plan, "applied": False, "project_id": project_id}
        for item in plan["items"]:
            row = db.query(NavFsmState).filter_by(fsm_id=fsm.id, state_id=item["state_id"]).one_or_none()
            if row is None:
                continue
            action = str(item.get("action") or "")
            meta = dict(row.meta or {})
            if action == "reject":
                meta["semantic_status"] = "rejected"
                meta["semantic_reason"] = item.get("reason") or "不是页面"
                meta["legacy_display_name"] = item.get("display_name") or ""
                row.meta = meta
                _remap_controls(db, row.state_id, "")
                _remap_edges(db, fsm.id, row.state_id, "")
                _remap_refs(db, app_id, row.state_id, "")
                continue
            if action in ("merge", "attach") and item.get("target_state_id"):
                meta["semantic_status"] = "rejected"
                meta["semantic_reason"] = item.get("reason") or "同一页面的重复状态"
                meta["merged_into"] = item["target_state_id"]
                meta["page_key"] = item.get("page_key") or page_key(item.get("page_label") or "")
                meta["variant"] = item.get("variant") or ""
                row.meta = meta
                _remap_controls(db, row.state_id, item["target_state_id"])
                _remap_edges(db, fsm.id, row.state_id, item["target_state_id"])
                _remap_refs(db, app_id, row.state_id, item["target_state_id"])
                continue
            label = item.get("page_label") or row.state_id
            meta["display_name"] = label
            meta["page_key"] = item.get("page_key") or page_key(label)
            meta["variant"] = item.get("variant") or ""
            meta["semantic_status"] = "attached" if action == "attach" else "provisional"
            meta["semantic_rule"] = "case_flow.canonical_page.v1"
            if item.get("display_name") and item["display_name"] != label:
                meta["legacy_display_name"] = item["display_name"]
            aliases = [str(x) for x in (meta.get("aliases") or []) if str(x)]
            if label not in aliases:
                aliases.append(label)
            meta["aliases"] = aliases[:20]
            row.meta = meta
        return {**plan, "applied": True, "project_id": project_id}
