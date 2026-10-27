"""页面入口档案的持久化与排序。

没有审核/发布状态机：抽取出来的入口直接 `active`。失败会降置信度、连续失败
标 `stale`，只是在排序里往后放，不会把运行时卡在“等审批”。
"""
from __future__ import annotations

import hashlib
import json
import time
from difflib import SequenceMatcher
from typing import Any

from mino_nexus.core.database import session_scope
from mino_nexus.core.log import SLog
from mino_nexus.models.nav_page_entry import NavPageEntry, NavPageEntryEvidence, NavPageGap
from mino_nexus.navigation.entries.text import labels_for_page, norm_key, norm_text

TAG = "NavPageEntry"
_MAX_LIST = 500
_MAX_EVIDENCE = 20


def _now() -> int:
    return int(time.time())


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def entry_hash(
    *,
    app_id: str,
    from_state_id: str,
    target_state_id: str,
    target_label: str,
    trigger: dict[str, Any],
) -> str:
    trigger = _as_dict(trigger)
    selector = _as_dict(trigger.get("selector"))
    selector_key = "|".join(
        str(selector.get(key) or "")
        for key in ("resource_id", "content_desc", "text", "class_name", "path")
    )
    if not selector_key and trigger.get("text"):
        selector_key = str(trigger.get("text") or "")
    raw = "|".join([
        norm_key(app_id),
        norm_key(from_state_id),
        norm_key(target_state_id),
        norm_key(target_label),
        norm_key(trigger.get("kind") or ""),
        norm_key(selector_key),
    ])
    return _sha1(raw)[:24]


def _entry_id(hash_value: str) -> str:
    return "entry." + str(hash_value or "")[:20]


_SOURCE_RANK = {"capture": 4, "runtime": 3, "manual": 3, "case": 2, "prior": 1}


def _confidence(*, source_kind: str, incoming: float, existing: float = 0.0, existing_kind: str = "") -> float:
    base = {
        "capture": 0.9,
        "runtime": 0.86,
        "case": 0.74,
        "prior": 0.58,
        "manual": 0.8,
    }.get(str(source_kind or ""), 0.6)
    value = max(float(incoming or 0.0), base)
    if existing:
        value = max(value, float(existing))
        if str(source_kind or "") != str(existing_kind or ""):
            value += 0.02
    return round(min(0.98, max(0.1, value)), 4)


def _public(row: NavPageEntry, *, evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "entry_id": row.entry_id,
        "app_id": row.app_id,
        "project_id": row.project_id,
        "from_state_id": row.from_state_id,
        "target_state_id": row.target_state_id,
        "target_label": row.target_label,
        "platform": row.platform,
        "build_scope": row.build_scope,
        "source_kind": row.source_kind,
        "status": row.status,
        "confidence": round(float(row.confidence or 0.0), 4),
        "trigger": _as_dict(row.trigger),
        "preconditions": _as_dict(row.preconditions),
        "expect": _as_dict(row.expect),
        "fingerprint_hash": str(_as_dict(row.fingerprint).get("hash") or ""),
        "fingerprint": _as_dict(row.fingerprint),
        "success_count": int(row.success_count or 0),
        "fail_count": int(row.fail_count or 0),
        "fail_streak": int(row.fail_streak or 0),
        "last_success_at": int(row.last_success_at or 0),
        "last_fail_at": int(row.last_fail_at or 0),
        "conflict": bool(row.conflict),
        "created_at": int(row.created_at or 0),
        "updated_at": int(row.updated_at or 0),
        "evidence": evidence or [],
    }


def _evidence_public(row: NavPageEntryEvidence) -> dict[str, Any]:
    return {
        "source_kind": row.source_kind,
        "source_ref": row.source_ref,
        "relation": row.relation,
        "quote": row.quote,
        "weight": round(float(row.weight or 0.0), 4),
        "data": _as_dict(row.data),
        "created_at": int(row.created_at or 0),
    }


def get_entry(app_id: str, entry_id: str) -> dict[str, Any] | None:
    with session_scope() as db:
        row = (
            db.query(NavPageEntry)
            .filter(NavPageEntry.app_id == str(app_id or ""), NavPageEntry.entry_id == str(entry_id or ""))
            .one_or_none()
        )
        if row is None:
            return None
        evidence = (
            db.query(NavPageEntryEvidence)
            .filter(NavPageEntryEvidence.entry_id == row.entry_id)
            .order_by(NavPageEntryEvidence.created_at.desc())
            .limit(_MAX_EVIDENCE)
            .all()
        )
        return _public(row, evidence=[_evidence_public(item) for item in evidence])


def list_entries(
    app_id: str,
    *,
    target: str = "",
    from_state_id: str = "",
    status: str = "",
    platform: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    with session_scope() as db:
        query = db.query(NavPageEntry).filter(NavPageEntry.app_id == str(app_id or ""))
        if status:
            query = query.filter(NavPageEntry.status == str(status))
        if platform:
            query = query.filter((NavPageEntry.platform == "") | (NavPageEntry.platform == str(platform)))
        if from_state_id:
            query = query.filter((NavPageEntry.from_state_id == "") | (NavPageEntry.from_state_id == str(from_state_id)))
        query = query.order_by(NavPageEntry.confidence.desc(), NavPageEntry.updated_at.desc())
        rows = query.limit(max(1, min(int(limit or 200), _MAX_LIST))).all()
        if target:
            rows = [row for row in rows if _target_score(str(target), row) > 0.35]
            rows.sort(key=lambda row: _target_score(str(target), row), reverse=True)
        return [_public(row) for row in rows]


def _target_score(target: str, row: NavPageEntry) -> float:
    want = norm_key(target)
    if not want:
        return 0.0
    labels = [row.target_label, row.target_state_id]
    labels.extend(labels_for_page(row.target_label))
    best = 0.0
    for label in labels:
        got = norm_key(label)
        if not got:
            continue
        if got == want:
            best = max(best, 1.0)
        elif want in got or got in want:
            best = max(best, 0.85)
        else:
            best = max(best, SequenceMatcher(None, want, got).ratio())
    return best


def upsert_entry(doc: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    """插入或合并一条入口。`doc` 的字段与模型同名，缺省安全。"""
    app_id = str(doc.get("app_id") or "")
    if not app_id:
        raise ValueError("app_id_required")
    from_state_id = str(doc.get("from_state_id") or "")
    target_state_id = str(doc.get("target_state_id") or "")
    target_label = str(doc.get("target_label") or "")
    trigger = _as_dict(doc.get("trigger"))
    hash_value = str(doc.get("entry_hash") or "") or entry_hash(
        app_id=app_id,
        from_state_id=from_state_id,
        target_state_id=target_state_id,
        target_label=target_label,
        trigger=trigger,
    )
    entry_id = str(doc.get("entry_id") or "") or _entry_id(hash_value)
    now = _now()
    source_kind = str(doc.get("source_kind") or "case")
    with session_scope() as db:
        row = (
            db.query(NavPageEntry)
            .filter(NavPageEntry.app_id == app_id, NavPageEntry.entry_hash == hash_value)
            .one_or_none()
        )
        created = row is None
        if row is None:
            row = NavPageEntry(
                entry_id=entry_id,
                app_id=app_id,
                project_id=str(doc.get("project_id") or ""),
                entry_hash=hash_value,
                created_at=now,
                status="active",
            )
            db.add(row)
        row.project_id = row.project_id or str(doc.get("project_id") or "")
        row.from_state_id = row.from_state_id or from_state_id
        row.target_state_id = row.target_state_id or target_state_id
        row.target_label = row.target_label or target_label
        row.platform = row.platform or str(doc.get("platform") or "")
        row.build_scope = row.build_scope or str(doc.get("build_scope") or "")
        incoming_source = str(doc.get("source_kind") or row.source_kind or "case")
        previous_source = str(row.source_kind or "")
        if _SOURCE_RANK.get(incoming_source, 0) >= _SOURCE_RANK.get(previous_source, 0):
            row.source_kind = incoming_source
        row.confidence = _confidence(
            source_kind=incoming_source,
            incoming=float(doc.get("confidence") or 0.0),
            existing=0.0 if created else float(row.confidence or 0.0),
            existing_kind=previous_source,
        )
        if trigger:
            row.trigger = trigger
        if doc.get("preconditions"):
            row.preconditions = _as_dict(doc.get("preconditions"))
        if doc.get("expect"):
            row.expect = _as_dict(doc.get("expect"))
        fingerprint = _as_dict(doc.get("fingerprint"))
        if fingerprint and (
            not _as_dict(row.fingerprint)
            or len(_as_list(fingerprint.get("anchors"))) >= len(_as_list(_as_dict(row.fingerprint).get("anchors")))
        ):
            row.fingerprint = fingerprint
        if str(doc.get("status") or "") in ("active", "stale", "rejected"):
            row.status = str(doc["status"])
        elif created:
            row.status = "active"
        if doc.get("conflict") is not None:
            row.conflict = bool(doc.get("conflict"))
        row.updated_at = now
        row.updated_by = str(actor or row.updated_by or "")

        source_ref = str(doc.get("source_ref") or "")
        relation = str(doc.get("relation") or ("observed" if source_kind == "capture" else "leads_to"))
        if source_ref:
            exists = (
                db.query(NavPageEntryEvidence.id)
                .filter(
                    NavPageEntryEvidence.entry_id == entry_id,
                    NavPageEntryEvidence.source_kind == source_kind,
                    NavPageEntryEvidence.source_ref == source_ref,
                    NavPageEntryEvidence.relation == relation,
                )
                .first()
            )
            if exists is None:
                db.add(NavPageEntryEvidence(
                    entry_id=entry_id,
                    app_id=app_id,
                    source_kind=source_kind,
                    source_ref=source_ref,
                    relation=relation,
                    quote=str(doc.get("quote") or "")[:2000],
                    weight=float(doc.get("weight") or 0.5),
                    data=_as_dict(doc.get("evidence_data")),
                    created_at=now,
                ))
        db.flush()
        return _public(row)


def record_result(entry_id: str, *, success: bool, detail: str = "") -> dict[str, Any] | None:
    now = _now()
    with session_scope() as db:
        row = db.query(NavPageEntry).filter(NavPageEntry.entry_id == str(entry_id or "")).one_or_none()
        if row is None:
            return None
        if success:
            row.success_count = int(row.success_count or 0) + 1
            row.fail_streak = 0
            row.last_success_at = now
            row.status = "active"
            row.confidence = round(min(0.98, float(row.confidence or 0.5) + 0.02), 4)
        else:
            row.fail_count = int(row.fail_count or 0) + 1
            row.fail_streak = int(row.fail_streak or 0) + 1
            row.last_fail_at = now
            row.confidence = round(max(0.1, float(row.confidence or 0.5) - 0.06), 4)
            if row.fail_streak >= 3:
                row.status = "stale"
        row.updated_at = now
        db.flush()
        return {
            "entry_id": row.entry_id,
            "status": row.status,
            "confidence": round(float(row.confidence or 0.0), 4),
            "success_count": int(row.success_count or 0),
            "fail_count": int(row.fail_count or 0),
            "fail_streak": int(row.fail_streak or 0),
            "detail": str(detail or ""),
        }


def set_fingerprint(entry_id: str, fingerprint: dict[str, Any]) -> bool:
    if not isinstance(fingerprint, dict) or not fingerprint.get("hash"):
        return False
    with session_scope() as db:
        row = db.query(NavPageEntry).filter(NavPageEntry.entry_id == str(entry_id or "")).one_or_none()
        if row is None:
            return False
        row.fingerprint = dict(fingerprint)
        row.updated_at = _now()
        return True


def record_gap(
    *,
    app_id: str,
    project_id: str = "",
    target_label: str = "",
    target_state_id: str = "",
    from_state_id: str = "",
    platform: str = "",
    kind: str = "no_entry",
    detail: str = "",
    sample_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """记录“知道目标但不知道怎么去”的缺口。重复出现只累加次数。"""
    raw = "|".join([
        norm_key(kind),
        norm_key(target_label),
        norm_key(target_state_id),
        norm_key(from_state_id),
        norm_key(platform),
    ])
    gap_id = "gap." + _sha1(raw)[:20]
    now = _now()
    with session_scope() as db:
        row = (
            db.query(NavPageGap)
            .filter(NavPageGap.app_id == str(app_id or ""), NavPageGap.gap_id == gap_id)
            .one_or_none()
        )
        if row is None:
            row = NavPageGap(
                gap_id=gap_id,
                app_id=str(app_id or ""),
                project_id=str(project_id or ""),
                target_label=str(target_label or ""),
                target_state_id=str(target_state_id or ""),
                from_state_id=str(from_state_id or ""),
                platform=str(platform or ""),
                kind=str(kind or "no_entry"),
                detail=str(detail or "")[:2000],
                occurrence_count=1,
                first_seen_at=now,
                last_seen_at=now,
                sample_refs=[sample_ref] if sample_ref else [],
            )
            db.add(row)
        else:
            row.occurrence_count = int(row.occurrence_count or 0) + 1
            row.last_seen_at = now
            # 同一个缺口再次出现说明旧结论已失效，重新打开，不能因为曾 resolved 就隐藏。
            row.status = "open"
            if detail:
                row.detail = str(detail)[:2000]
            refs = _as_list(row.sample_refs)
            if sample_ref:
                refs.append(sample_ref)
            row.sample_refs = refs[-5:]
        db.flush()
        return _gap_public(row)


def _gap_public(row: NavPageGap) -> dict[str, Any]:
    return {
        "gap_id": row.gap_id,
        "app_id": row.app_id,
        "project_id": row.project_id,
        "target_label": row.target_label,
        "target_state_id": row.target_state_id,
        "from_state_id": row.from_state_id,
        "platform": row.platform,
        "kind": row.kind,
        "status": row.status,
        "detail": row.detail,
        "occurrence_count": int(row.occurrence_count or 0),
        "first_seen_at": int(row.first_seen_at or 0),
        "last_seen_at": int(row.last_seen_at or 0),
        "sample_refs": _as_list(row.sample_refs),
    }


def list_gaps(app_id: str, *, status: str = "open", limit: int = 200) -> list[dict[str, Any]]:
    with session_scope() as db:
        query = db.query(NavPageGap).filter(NavPageGap.app_id == str(app_id or ""))
        if status:
            query = query.filter(NavPageGap.status == str(status))
        rows = (
            query.order_by(NavPageGap.last_seen_at.desc(), NavPageGap.occurrence_count.desc())
            .limit(max(1, min(int(limit or 200), _MAX_LIST)))
            .all()
        )
        return [_gap_public(row) for row in rows]


def resolve_gaps(app_id: str, *, target_label: str = "", target_state_id: str = "") -> int:
    if not target_label and not target_state_id:
        return 0
    now = _now()
    with session_scope() as db:
        query = db.query(NavPageGap).filter(NavPageGap.app_id == str(app_id or ""), NavPageGap.status == "open")
        rows = query.all()
        count = 0
        for row in rows:
            if target_state_id and row.target_state_id == target_state_id:
                row.status = "resolved"
            elif target_label and norm_key(row.target_label) == norm_key(target_label):
                row.status = "resolved"
            else:
                continue
            row.last_seen_at = now
            count += 1
        return count


def summary(app_id: str) -> dict[str, Any]:
    with session_scope() as db:
        rows = db.query(NavPageEntry).filter(NavPageEntry.app_id == str(app_id or "")).all()
        gaps = (
            db.query(NavPageGap)
            .filter(NavPageGap.app_id == str(app_id or ""), NavPageGap.status == "open")
            .count()
        )
        by_status: dict[str, int] = {}
        by_source: dict[str, int] = {}
        with_fingerprint = 0
        clean = 0
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
            by_source[row.source_kind] = by_source.get(row.source_kind, 0) + 1
            if _as_dict(row.fingerprint).get("hash"):
                with_fingerprint += 1
            if not bool(row.conflict):
                clean += 1
        total = len(rows)
    return {
        "app_id": str(app_id or ""),
        "total": total,
        "by_status": by_status,
        "by_source": by_source,
        "open_gaps": gaps,
        "with_fingerprint": with_fingerprint,
        "clean": clean,
    }


def candidates(
    app_id: str,
    *,
    target: str,
    from_state_id: str = "",
    platform: str = "",
    fingerprint: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按目标名/当前页/指纹给入口打分。只返回 active；stale 仅作最后兜底。"""
    rows = list_entries(app_id, target=target, platform=platform, limit=_MAX_LIST)
    current_fp = _as_dict(fingerprint)
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["status"] == "rejected":
            continue
        score = _target_score(target, _row_stub(row))
        score += 0.24 * float(row.get("confidence") or 0.0)
        score += 0.18 if row.get("from_state_id") and row.get("from_state_id") == from_state_id else 0.0
        score += 0.06 if not row.get("from_state_id") else 0.0
        if from_state_id and row.get("from_state_id") and row["from_state_id"] != from_state_id:
            score -= 0.45
        if row["status"] == "stale":
            score -= 0.35
        if row.get("conflict"):
            score -= 0.2
        if current_fp and isinstance(row.get("fingerprint"), dict) and row["fingerprint"].get("hash"):
            from mino_nexus.navigation.entries.fingerprint import compare_fingerprints

            cmp = compare_fingerprints(row["fingerprint"], current_fp)
            score += 0.2 * float(cmp.get("score") or 0.0)
            row["fingerprint_match"] = cmp
        score += min(0.08, 0.01 * int(row.get("success_count") or 0))
        score -= min(0.18, 0.03 * int(row.get("fail_streak") or 0))
        row["score"] = round(score, 4)
        out.append(row)
    out.sort(key=lambda item: item["score"], reverse=True)
    return out


def _row_stub(row: dict[str, Any]) -> NavPageEntry:
    obj = NavPageEntry()
    obj.target_label = str(row.get("target_label") or "")
    obj.target_state_id = str(row.get("target_state_id") or "")
    return obj


def dump_for_log(row: dict[str, Any] | None) -> str:
    try:
        return json.dumps(row or {}, ensure_ascii=False)[:1600]
    except Exception:  # noqa: BLE001
        return str(row)[:1600]
