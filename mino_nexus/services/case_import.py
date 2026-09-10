"""用例导入：preview / commit，冲突由用户决定覆盖或跳过。"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Literal

from mino_nexus.services import cover_import as ci
from mino_nexus.services import project_store as ps
from mino_nexus.services.case_store import (
    apply_import_row,
    count_cases,
    find_similar_case,
    get_case,
    list_cases,
)

ConflictAction = Literal["overwrite", "skip"]

_PREVIEW_TTL = timedelta(hours=2)
_previews: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _purge_previews() -> None:
    cutoff = datetime.now(timezone.utc) - _PREVIEW_TTL
    dead = []
    for tok, doc in _previews.items():
        created = doc.get("created_at")
        try:
            ts = datetime.fromisoformat(str(created))
        except Exception:
            dead.append(tok)
            continue
        if ts < cutoff:
            dead.append(tok)
    for tok in dead:
        _previews.pop(tok, None)


def _require_project(project_id: str) -> dict[str, Any]:
    row = ps.find_project(str(project_id or "").strip())
    if not row:
        raise ValueError("项目不存在")
    return row


def list_project_requirements(project_id: str) -> list[dict[str, Any]]:
    """聚合项目下各 App 的 qa_process.requirements（按 id 去重）。"""
    from mino_nexus.services import app_automation as aas

    _require_project(project_id)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for app in ps.list_apps(project_id):
        app_row = ps.find_app(str(app.get("id") or ""))
        if not app_row:
            continue
        qp = aas.get_automation_config(app_row, hydrate_cases=False).get("qa_process") or {}
        for req in qp.get("requirements") or []:
            if not isinstance(req, dict):
                continue
            rid = str(req.get("id") or "").strip()
            if not rid or rid in seen:
                continue
            seen.add(rid)
            out.append({
                "id": rid,
                "title": str(req.get("title") or req.get("external_id") or rid),
                "external_id": str(req.get("external_id") or ""),
            })
    return out


def _requirement_title(project_id: str, requirement_id: str) -> str:
    rid = str(requirement_id or "").strip()
    for req in list_project_requirements(project_id):
        if str(req.get("id") or "") == rid:
            return str(req.get("title") or rid)
    return rid


def _norm_import_row(raw: dict[str, Any], index: int) -> dict[str, Any]:
    steps = raw.get("steps")
    expected = raw.get("expected")
    if isinstance(steps, str):
        steps_list = [s.strip() for s in steps.splitlines() if s.strip()]
        steps_raw = steps
    else:
        steps_list = [str(x) for x in (steps or []) if str(x).strip()]
        steps_raw = str(raw.get("steps_raw") or "") or "\n".join(f"{i}. {x}" for i, x in enumerate(steps_list, 1))
    if isinstance(expected, str):
        expected_list = [s.strip() for s in expected.splitlines() if s.strip()]
        expected_raw = expected
    else:
        expected_list = [str(x) for x in (expected or []) if str(x).strip()]
        expected_raw = str(raw.get("expected_raw") or "") or "\n".join(
            f"{i}. {x}" for i, x in enumerate(expected_list, 1)
        )
    cid = str(raw.get("case_id") or "").strip()
    return {
        **raw,
        "case_id": cid,
        "name": str(raw.get("name") or cid or f"导入用例 {index + 1}"),
        "module": str(raw.get("module") or ""),
        "precondition": str(raw.get("precondition") or ""),
        "steps": steps_list,
        "expected": expected_list,
        "steps_raw": steps_raw,
        "expected_raw": expected_raw,
        "platform": str(raw.get("platform") or "双端"),
        "aspect": str(raw.get("aspect") or "正向"),
        "point_ids": [str(x) for x in (raw.get("point_ids") or []) if str(x).strip()],
        "index": index,
        "source": "import",
    }


def _similar_case_info(
    project_id: str,
    requirement_id: str,
    row: dict[str, Any],
) -> dict[str, Any] | None:
    ex = find_similar_case(project_id, requirement_id, row)
    if not ex:
        return None
    existing_rid = str(ex.get("requirement_id") or "")
    target_rid = str(requirement_id or "")
    return {
        "exists": True,
        "existing_case_id": ex.get("case_id") or "",
        "existing_name": ex.get("name") or "",
        "existing_requirement_id": existing_rid,
        "existing_requirement_title": _requirement_title(project_id, existing_rid),
        "requirement_changed": bool(existing_rid and target_rid and existing_rid != target_rid),
        "content_match": True,
    }


def _conflict_info(
    project_id: str,
    case_id: str,
    target_requirement_id: str,
) -> dict[str, Any] | None:
    cid = str(case_id or "").strip()
    if not cid:
        return None
    existing = get_case(project_id, cid)
    if not existing:
        return None
    existing_rid = str(existing.get("requirement_id") or "")
    target_rid = str(target_requirement_id or "")
    return {
        "exists": True,
        "existing_case_id": cid,
        "existing_name": existing.get("name") or "",
        "existing_requirement_id": existing_rid,
        "existing_requirement_title": _requirement_title(project_id, existing_rid),
        "requirement_changed": bool(existing_rid and target_rid and existing_rid != target_rid),
    }


def preview_import(
    *,
    project_id: str,
    requirement_id: str,
    table: list[list[str]] | None = None,
    text: str = "",
    filename: str = "",
    header_row: int = 0,
    skip_rows: list[int] | None = None,
    column_map: dict[str, int] | None = None,
) -> dict[str, Any]:
    _purge_previews()
    pid = str(project_id or "").strip()
    rid = str(requirement_id or "").strip()
    if not pid:
        raise ValueError("缺少 project_id")
    if not rid:
        raise ValueError("请选择需求")
    _require_project(pid)
    reqs = list_project_requirements(pid)
    if not any(str(r.get("id") or "") == rid for r in reqs):
        raise ValueError(f"需求 {rid} 不属于该项目")
    grid = normalize_table(table) if table else ci.load_table_from_text(text, filename)
    labels = ci.header_labels(grid, header_row)
    cmap = dict(column_map or ci.suggest_column_map(labels))
    parsed, meta = ci.parse_table_rows(
        grid,
        header_row=header_row,
        skip_rows=skip_rows,
        column_map=cmap,
    )
    preview_rows: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for i, raw in enumerate(parsed):
        row = _norm_import_row(raw, i)
        name = str(row.get("name") or "").strip()
        cid = str(row.get("case_id") or "").strip()
        conflict = _conflict_info(pid, cid, rid) if cid else None
        if not conflict:
            conflict = _similar_case_info(pid, rid, row)
        if conflict:
            conflicts.append(str(conflict.get("existing_case_id") or cid or name))
        pre = row.get("precondition") or ""
        steps = row.get("steps") or []
        expected = row.get("expected") or []
        flags: list[str] = []
        header_names = {
            "用例名称", "名称", "用例编号", "编号", "模块", "端", "前置条件",
            "步骤", "测试步骤", "预期", "预期效果", "预期结果",
        }
        if name in header_names:
            flags.append("likely_header")
        if name and not steps and not cid:
            flags.append("empty_steps")
        if name and not expected and not cid:
            flags.append("empty_expected")
        if conflict and not cid:
            flags.append("likely_duplicate")
        preview_rows.append({
            "row_index": raw.get("row_index", i),
            "case_id": cid,
            "generated_id": not bool(cid),
            "name": name,
            "module": row.get("module") or "",
            "platform": row.get("platform") or "",
            "precondition_preview": str(pre)[:200],
            "steps_preview": "\n".join(steps)[:240],
            "expected_preview": "\n".join(expected)[:240],
            "flags": flags,
            "selected_by_default": "likely_header" not in flags,
            "conflict": conflict,
            "payload": row,
        })
    token = uuid.uuid4().hex
    _previews[token] = {
        "created_at": _now(),
        "project_id": pid,
        "requirement_id": rid,
        "rows": copy.deepcopy(preview_rows),
        "meta": meta,
    }
    return {
        "preview_token": token,
        "project_id": pid,
        "requirement_id": rid,
        "requirement_title": _requirement_title(pid, rid),
        "parsed": len(preview_rows),
        "conflicts": conflicts,
        "rows": preview_rows,
        **meta,
    }


def normalize_table(table: Any) -> list[list[str]]:
    return ci.normalize_table(table)


def commit_import(
    *,
    project_id: str,
    requirement_id: str,
    preview_token: str = "",
    rows: list[dict[str, Any]] | None = None,
    default_on_conflict: ConflictAction = "skip",
) -> dict[str, Any]:
    pid = str(project_id or "").strip()
    rid = str(requirement_id or "").strip()
    if not pid or not rid:
        raise ValueError("缺少 project_id 或 requirement_id")
    _require_project(pid)
    work_rows: list[dict[str, Any]] = []
    if preview_token:
        doc = _previews.get(str(preview_token))
        if not doc:
            raise ValueError("预览已过期，请重新解析")
        if str(doc.get("project_id") or "") != pid:
            raise ValueError("预览与项目不匹配")
        if str(doc.get("requirement_id") or "") != rid:
            raise ValueError("预览与需求不匹配")
        work_rows = copy.deepcopy(list(doc.get("rows") or []))
        overrides = {
            item.get("row_index"): item
            for item in (rows or [])
            if isinstance(item, dict) and item.get("row_index") is not None
        }
        for item in work_rows:
            ov = overrides.get(item.get("row_index"))
            if not ov:
                continue
            if "on_conflict" in ov:
                item["on_conflict"] = ov["on_conflict"]
            if "selected" in ov:
                item["selected"] = ov["selected"]
    elif rows:
        work_rows = rows
    else:
        raise ValueError("请提供 preview_token 或 rows")
    if not work_rows:
        raise ValueError("没有可导入的行")
    created = updated = skipped = 0
    results: list[dict[str, Any]] = []
    for item in work_rows:
        if not isinstance(item, dict):
            continue
        if item.get("selected") is False:
            skipped += 1
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        if not isinstance(payload, dict):
            skipped += 1
            continue
        row = _norm_import_row(payload, int(item.get("index") or 0))
        action_raw = str(item.get("on_conflict") or default_on_conflict or "skip").strip().lower()
        on_conflict: ConflictAction = "overwrite" if action_raw == "overwrite" else "skip"
        status, saved = apply_import_row(pid, rid, row, on_conflict=on_conflict)
        if status == "created":
            created += 1
        elif status == "updated":
            updated += 1
        else:
            skipped += 1
        results.append({
            "row_index": item.get("row_index"),
            "case_id": (saved or {}).get("case_id") or row.get("case_id") or "",
            "status": status,
        })
    if preview_token:
        _previews.pop(str(preview_token), None)
    total = count_cases(pid, rid)
    return {
        "project_id": pid,
        "requirement_id": rid,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total": total,
        "results": results,
        "action": "imported_cases",
    }


def cases_payload(project_id: str) -> dict[str, Any]:
    pid = str(project_id or "").strip()
    _require_project(pid)
    cases = list_cases(pid)
    req_titles = {str(r.get("id") or ""): str(r.get("title") or "") for r in list_project_requirements(pid)}
    rows: list[dict[str, Any]] = []
    for raw in cases:
        title = req_titles.get(str(raw.get("requirement_id") or ""), "需求")
        rows.append({**raw, "requirement_title": title})
    return {
        "cases": rows,
        "total": len(rows),
        "source": "project_cases",
        "project_id": pid,
    }
