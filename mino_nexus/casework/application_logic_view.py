"""应用逻辑安全读模型：页面、控件、入口/缺口、总览。

数据真源仍是 nav_fsm / ui_controls / nav_page_entries / nav_page_gaps。
这里只做白名单映射、聚合和分页；写入仍走既有 configure / entries.extract 路由。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.casework.application_understanding import case_refs, pages
from mino_nexus.casework.v5_controls import audit, list_controls
from mino_nexus.core.redact import redact, scrub_text, text
from mino_nexus.navigation.entries.store import list_entries, list_gaps, summary
from mino_nexus.navigation.entries.text import norm_key
from mino_nexus.resources import project_store as ps

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def _app(project_id: str, app_id: str) -> dict[str, Any]:
    app = ps.require_app(str(app_id or ""))
    pid = str(app.get("project_id") or "")
    if pid != str(project_id or ""):
        raise ValueError("app_project_mismatch")
    return app


def _entry_extract_summary(result: dict[str, Any]) -> dict[str, Any]:
    """MCP 写入口只回各来源计数；明细走 logic.entry.list，避免把自由文本带出。"""
    def counts(part: Any) -> dict[str, int]:
        row = part if isinstance(part, dict) else {}
        return {
            "entry_count": int(row.get("entry_count") or 0),
            "gap_count": int(row.get("gap_count") or 0),
        }

    return {
        "app_id": text(result.get("app_id"), 80),
        "project_id": text(result.get("project_id"), 80),
        "platform": text(result.get("platform"), 40),
        "entry_count": int(result.get("entry_count") or 0),
        "gap_count": int(result.get("gap_count") or 0),
        "case": counts(result.get("case")),
        "capture": counts(result.get("capture")),
    }


def extract_entries(
    project_id: str,
    app_id: str,
    *,
    platform: str = "",
    case_ids: list[str] | None = None,
    include_cases: bool = True,
    include_capture: bool = True,
    actor: str = "",
) -> dict[str, Any]:
    """MCP 的安全入口重抽路由：先校验应用归属，再只返回计数摘要。"""
    _app(project_id, app_id)
    from mino_nexus.navigation.entries import extract as entry_extract

    result = entry_extract.extract_for_app(
        app_id=app_id,
        project_id=project_id,
        platform=str(platform or ""),
        case_ids=list(case_ids or []) or None,
        include_cases=bool(include_cases),
        include_capture=bool(include_capture),
        actor=str(actor or ""),
    )
    return _entry_extract_summary(result)


def _page_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "state_id": text(row.get("state_id"), 80),
        "label": scrub_text(row.get("label"), 120),
        "page_key": text(row.get("page_key"), 120),
        "variant": text(row.get("variant"), 40),
        "semantic_status": text(row.get("semantic_status"), 24),
        "knowledge_revision": int(row.get("revision") or 0),
        "case_ref_count": int(row.get("case_ref_count") or 0),
        "visit_count": int(row.get("visit_count") or 0),
        "anchors": [scrub_text(item, 80) for item in (row.get("anchors") or [])[:8]],
    }


def _control_row(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    locator = row.get("locator_value") if isinstance(row.get("locator_value"), dict) else {}
    source = locator.get("source") if isinstance(locator.get("source"), dict) else {}
    has_locator = bool(str(source.get("playwright") or source.get("uiautomator") or "").strip())
    out = {
        "control_id": text(row.get("control_id"), 80),
        "target_desc": scrub_text(row.get("target_desc"), 160),
        "element_type": text(row.get("element_type"), 32),
        "scope": text(row.get("scope"), 24),
        "page_state_id": text(row.get("page_state_id"), 80),
        "semantic_role": text(row.get("semantic_role"), 24),
        "revision": int(row.get("revision") or 1),
        "publish_state": text(row.get("publish_state"), 24),
        "sensitive": bool(row.get("sensitive")),
        "has_locator": has_locator,
        "locator_source": sorted(str(key) for key, value in source.items() if str(value or "").strip()),
        "updated_at": text(row.get("updated_at"), 40),
        "updated_by": scrub_text(row.get("updated_by"), 80),
    }
    if detail:
        out["locator_value"] = redact(locator)
        out["locator_spec"] = redact(row.get("locator_spec") if isinstance(row.get("locator_spec"), dict) else {})
        out["pending_merge"] = bool(row.get("pending_merge"))
        out["published_at"] = text(row.get("published_at"), 40)
    return out


def overview(project_id: str, app_id: str) -> dict[str, Any]:
    _app(project_id, app_id)
    page_rows = list(pages(project_id, app_id).get("pages") or [])
    state_ids = {str(row.get("state_id") or "") for row in page_rows}
    controls = list_controls(project_id)
    app_controls = [row for row in controls if str(row.get("page_state_id") or "") in state_ids]
    elements = [row for row in app_controls if str(row.get("semantic_role") or "") == "element"]
    identities = audit(project_id)
    entry_summary = summary(app_id)
    gaps = list_gaps(app_id, status="open", limit=200)
    pages_with_refs = sum(1 for row in page_rows if int(row.get("case_ref_count") or 0) > 0)
    bound_controls = sum(1 for row in app_controls if str(row.get("page_state_id") or "").strip())
    total_pages = len(page_rows)
    coverage = {
        "pages_total": total_pages,
        "pages_with_case_refs": pages_with_refs,
        "pages_with_case_refs_pct": round(pages_with_refs * 100.0 / total_pages, 1) if total_pages else 0.0,
        "controls_total": len(app_controls),
        "controls_element": len(elements),
        "controls_bound_to_page": bound_controls,
    }
    warnings = []
    if total_pages == 0:
        warnings.append("应用还没有页面状态，先采集或运行一次")
    if not app_controls:
        warnings.append("应用还没有控件记录")
    if identities:
        warnings.append(f"{len(identities)} 个 control_id 存在重复记录")
    if entry_summary.get("open_gaps"):
        warnings.append(f"{entry_summary['open_gaps']} 个入口缺口未关闭")
    recent_pages = sorted(
        page_rows,
        key=lambda row: (int(row.get("visit_count") or 0), int(row.get("case_ref_count") or 0)),
        reverse=True,
    )[:5]
    return {
        "project_id": text(project_id, 80),
        "app_id": text(app_id, 80),
        "coverage": coverage,
        "entries": entry_summary,
        "gaps": {
            "open": len(gaps),
            "items": [redact(item, scrub=True) for item in gaps[:5]],
        },
        "controls": {
            "identity_conflicts": identities,
            "duplicate_total": sum(int(item.get("count") or 0) for item in identities),
        },
        "recent_pages": [_page_row(row) for row in recent_pages],
        "warnings": warnings,
    }


def list_pages(
    project_id: str,
    app_id: str,
    *,
    status: str = "",
    q: str = "",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    _app(project_id, app_id)
    rows = list(pages(project_id, app_id).get("pages") or [])
    status_q = str(status or "").strip()
    needle = str(q or "").strip().lower()
    filtered = []
    for row in rows:
        if status_q and str(row.get("semantic_status") or "") != status_q:
            continue
        if needle:
            hay = " ".join(str(row.get(key) or "") for key in ("label", "page_key", "state_id")).lower()
            if needle not in hay:
                continue
        filtered.append(row)
    total = len(filtered)
    lim = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    off = max(0, int(offset or 0))
    page = filtered[off: off + lim]
    return {
        "items": [_page_row(row) for row in page],
        "total": total,
        "limit": lim,
        "next_offset": off + lim if off + lim < total else None,
        "truncated": off + lim < total,
    }


def _review_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_id": text(record.get("review_id"), 80),
        "status": text(record.get("status"), 24),
        "revision": int(record.get("revision") or 0),
        "created_at": text(record.get("created_at"), 40),
        "stale": bool(record.get("stale")),
        "positive_terms": [scrub_text(x, 80) for x in (record.get("positive_terms") or [])[:10]],
        "negative_terms": [scrub_text(x, 80) for x in (record.get("negative_terms") or [])[:10]],
        "examples_count": len(record.get("examples") or []),
    }


def propose_review(
    project_id: str,
    app_id: str,
    state_id: str,
    body: Any,
) -> dict[str, Any]:
    """MCP 只创建候选；批准/驳回仍由 Workbench 的人工 decide 完成。"""
    _app(project_id, app_id)
    from mino_nexus.casework import application_understanding as svc

    record = svc.propose(project_id, app_id, state_id, body)
    out = _review_row(record)
    out.update({
        "request_id": text(record.get("request_id"), 80),
        "version": text(getattr(body, "version", ""), 40),
        "platform": text(record.get("platform"), 24),
        "build": text(record.get("build"), 80),
        "reason": scrub_text(record.get("reason"), 300),
    })
    return out


def page_detail(
    project_id: str,
    app_id: str,
    state_id: str,
    *,
    include: list[str] | None = None,
    controls_limit: int = 30,
    refs_limit: int = 20,
) -> dict[str, Any] | None:
    _app(project_id, app_id)
    rows = list(pages(project_id, app_id).get("pages") or [])
    row = next((item for item in rows if str(item.get("state_id") or "") == str(state_id or "")), None)
    if row is None:
        return None
    wanted = {str(item).strip().lower() for item in (include or []) if str(item).strip()}
    if not wanted:
        wanted = {"controls", "case_refs", "entry", "gaps"}
    out: dict[str, Any] = {
        "page": _page_row(row),
        "state_id": text(state_id, 80),
        "label": scrub_text(row.get("label"), 120),
    }
    if "controls" in wanted:
        controls = [
            item for item in list_controls(project_id)
            if str(item.get("page_state_id") or "") == str(state_id or "")
            and str(item.get("semantic_role") or "") == "element"
        ]
        lim = max(1, min(int(controls_limit or 30), 100))
        out["controls"] = [_control_row(item) for item in controls[:lim]]
        out["controls_total"] = len(controls)
        out["controls_truncated"] = len(controls) > lim
    if "case_refs" in wanted:
        refs = case_refs(project_id, app_id, state_id)
        grouped: dict[str, dict[str, Any]] = {}
        relation_counts: dict[str, int] = {}
        for item in refs.get("items") or []:
            cid = str(item.get("case_id") or "")
            if not cid:
                continue
            bucket = grouped.setdefault(cid, {
                "case_id": text(cid, 80),
                "case_name": text(item.get("case_name"), 120),
                "count": 0,
                "relations": {},
            })
            bucket["count"] += 1
            rel = str(item.get("relation") or "unknown")
            bucket["relations"][rel] = int(bucket["relations"].get(rel) or 0) + 1
            relation_counts[rel] = relation_counts.get(rel, 0) + 1
        lim = max(1, min(int(refs_limit or 20), 50))
        cases = sorted(grouped.values(), key=lambda item: item["count"], reverse=True)
        out["case_refs"] = {
            "total_cases": len(cases),
            "total_refs": len(refs.get("items") or []),
            "by_relation": relation_counts,
            "items": cases[:lim],
            "truncated": len(cases) > lim,
        }
    label = str(row.get("label") or "")
    if "entry" in wanted:
        entries = list_entries(app_id, target=label, limit=20) if label else []
        out["entry"] = {
            "items": [_entry_row(item) for item in entries[:10]],
            "total_candidates": len(entries),
            "truncated": len(entries) > 10,
        }
    if "reviews" in wanted:
        records = [item for item in (row.get("reviews") or []) if isinstance(item, dict)]
        out["reviews"] = {
            "items": [_review_row(item) for item in records[:20]],
            "total": len(records),
            "truncated": len(records) > 20,
        }
    if "gaps" in wanted:
        gaps = [
            item for item in list_gaps(app_id, status="open", limit=200)
            if str(item.get("target_state_id") or "") == str(state_id or "")
            or (label and norm_key(item.get("target_label")) == norm_key(label))
        ]
        out["gaps"] = {
            "items": [redact(item, scrub=True) for item in gaps[:10]],
            "total": len(gaps),
            "truncated": len(gaps) > 10,
        }
    return out


def _entry_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": text(row.get("entry_id"), 80),
        "from_state_id": text(row.get("from_state_id"), 80),
        "target_state_id": text(row.get("target_state_id"), 80),
        "target_label": scrub_text(row.get("target_label"), 120),
        "platform": text(row.get("platform"), 24),
        "source_kind": text(row.get("source_kind"), 24),
        "status": text(row.get("status"), 24),
        "confidence": float(row.get("confidence") or 0.0),
        "trigger": redact(row.get("trigger") if isinstance(row.get("trigger"), dict) else {}, scrub=True),
        "expect": redact(row.get("expect") if isinstance(row.get("expect"), dict) else {}, scrub=True),
        "fingerprint_hash": text(row.get("fingerprint_hash"), 80),
        "success_count": int(row.get("success_count") or 0),
        "fail_count": int(row.get("fail_count") or 0),
        "conflict": bool(row.get("conflict")),
        "updated_at": int(row.get("updated_at") or 0),
    }


def list_controls_view(
    project_id: str,
    *,
    app_id: str = "",
    page_state_id: str = "",
    semantic_role: str = "",
    sensitive: str = "",
    q: str = "",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    state_ids: set[str] | None = None
    if app_id:
        _app(project_id, app_id)
        state_ids = {
            str(row.get("state_id") or "")
            for row in (pages(project_id, app_id).get("pages") or [])
        }
    rows = list_controls(project_id)
    page_q = str(page_state_id or "").strip()
    role_q = str(semantic_role or "").strip()
    sensitive_q = str(sensitive or "").strip().lower()
    needle = str(q or "").strip().lower()
    filtered = []
    for row in rows:
        if state_ids is not None and str(row.get("page_state_id") or "") not in state_ids:
            continue
        if page_q and str(row.get("page_state_id") or "") != page_q:
            continue
        if role_q and str(row.get("semantic_role") or "") != role_q:
            continue
        if sensitive_q in {"true", "1", "yes"} and not bool(row.get("sensitive")):
            continue
        if sensitive_q in {"false", "0", "no"} and bool(row.get("sensitive")):
            continue
        if needle and needle not in " ".join(
            str(row.get(key) or "") for key in ("control_id", "target_desc", "page_state_id")
        ).lower():
            continue
        filtered.append(row)
    filtered.sort(key=lambda row: (str(row.get("page_state_id") or ""), str(row.get("control_id") or "")))
    total = len(filtered)
    lim = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    off = max(0, int(offset or 0))
    page = filtered[off: off + lim]
    return {
        "items": [_control_row(row) for row in page],
        "total": total,
        "limit": lim,
        "next_offset": off + lim if off + lim < total else None,
        "truncated": off + lim < total,
        "identity_conflicts": audit(project_id),
    }


def control_detail(project_id: str, control_id: str) -> dict[str, Any] | None:
    rows = [row for row in list_controls(project_id) if str(row.get("control_id") or "") == str(control_id or "")]
    if not rows:
        return None
    rows.sort(key=lambda row: int(row.get("revision") or 0), reverse=True)
    out = _control_row(rows[0], detail=True)
    out["versions"] = [
        {
            "revision": int(row.get("revision") or 0),
            "publish_state": text(row.get("publish_state"), 24),
            "updated_at": text(row.get("updated_at"), 40),
            "updated_by": scrub_text(row.get("updated_by"), 80),
        }
        for row in rows[:20]
    ]
    out["versions_truncated"] = len(rows) > 20
    out["identity_conflict"] = len(rows) > 1
    return out


def list_entries_view(
    project_id: str,
    app_id: str,
    *,
    kind: str = "entry",
    target: str = "",
    from_state_id: str = "",
    status: str = "",
    platform: str = "",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    _app(project_id, app_id)
    kind_q = str(kind or "entry").strip().lower()
    lim = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    off = max(0, int(offset or 0))
    if kind_q in {"summary", "stats"}:
        return {"kind": "summary", "summary": summary(app_id)}
    if kind_q in {"gap", "gaps"}:
        rows = list_gaps(app_id, status=str(status or "open"), limit=MAX_LIMIT)
        total = len(rows)
        return {
            "kind": "gap",
            "items": [redact(row, scrub=True) for row in rows[off: off + lim]],
            "total": total,
            "limit": lim,
            "next_offset": off + lim if off + lim < total else None,
            "truncated": off + lim < total,
        }
    rows = list_entries(
        app_id,
        target=str(target or ""),
        from_state_id=str(from_state_id or ""),
        status=str(status or ""),
        platform=str(platform or ""),
        limit=500,
    )
    total = len(rows)
    page = rows[off: off + lim]
    return {
        "kind": "entry",
        "items": [_entry_row(row) for row in page],
        "total": total,
        "limit": lim,
        "next_offset": off + lim if off + lim < total else None,
        "truncated": off + lim < total,
    }


def _flow_block_row(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    steps = row.get("steps_json") if isinstance(row.get("steps_json"), list) else []
    out = {
        "block_id": text(row.get("block_id"), 120),
        "name": scrub_text(row.get("display_name") or row.get("block_id"), 160),
        "description": scrub_text(row.get("description"), 500),
        "enabled": bool(row.get("enabled")),
        "version": text(row.get("version"), 40),
        "key_ref": text(row.get("key_ref"), 120),
        "step_count": len(steps),
        "case_ids": [text(item, 80) for item in (row.get("case_ids") or [])][:20],
    }
    if detail:
        out["steps"] = [redact(step, scrub=True) for step in steps[:100] if isinstance(step, dict)]
        out["steps_truncated"] = len(steps) > 100
    return out


def list_flow_blocks(project_id: str, *, channel: str = "") -> dict[str, Any]:
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import list_catalog

    ps.require_project(project_id)
    rows = list_catalog(channel=str(channel or "").strip())
    return {
        "items": [_flow_block_row(row) for row in rows],
        "total": len(rows),
        "project_id": text(project_id, 80),
    }


def flow_block_detail(project_id: str, block_id: str) -> dict[str, Any] | None:
    from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import load_block_row

    ps.require_project(project_id)
    row = load_block_row(app_id=GLOBAL_APP_ID, block_id=str(block_id or ""))
    if row is None:
        return None
    out = _flow_block_row(row, detail=True)
    out["project_id"] = text(project_id, 80)
    return out
