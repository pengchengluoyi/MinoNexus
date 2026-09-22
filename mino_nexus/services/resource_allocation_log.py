"""测试资源租号/释放审计与选号降权。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from mino_nexus.core.database import session_scope
from mino_nexus.core.log import SLog
from mino_nexus.models.resource_ops import ResourceAllocationLog

TAG = "ResourceAllocLog"

RECENT_PICK_WINDOW_SEC = 4 * 3600
MAX_RECENT_PICK_PENALTY = 36

ACTION_FACET_UPDATE = "facet_update"
ACTION_FACET_RESTORE = "facet_restore"

_SESSION_MIRROR_ACTIONS = frozenset({
    "lease_claim",
    "lease_release",
    "lease_fail",
    ACTION_FACET_UPDATE,
    ACTION_FACET_RESTORE,
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_session_id(run_id: str, *, case_id: str = "") -> str:
    rid = str(run_id or "").strip()
    if not rid:
        return ""
    try:
        from mino_nexus.services.session_store import resolve_session_id

        return str(resolve_session_id(rid, case_id=case_id) or "").strip()
    except Exception:
        return rid


def _mirror_allocation_session_log(
    *,
    session_id: str,
    action: str,
    message: str,
    run_id: str,
    case_id: str,
    account_ident: str,
    allocation_log_id: int,
    detail: dict[str, Any],
) -> None:
    sid = str(session_id or "").strip()
    if not sid or str(action or "") not in _SESSION_MIRROR_ACTIONS:
        return
    try:
        from mino_nexus.services import session_store

        session_store.append_event(
            session_id=sid,
            type="resource/account",
            payload={
                "action": str(action or "")[:48],
                "message": str(message or "")[:500],
                "run_id": str(run_id or "")[:80],
                "case_id": str(case_id or "")[:80],
                "account_ident": str(account_ident or "")[:80],
                "allocation_log_id": int(allocation_log_id or 0),
                "detail_keys": sorted(str(k) for k in (detail or {}).keys())[:24],
            },
            turn=0,
            phase="resource",
        )
    except Exception as exc:
        SLog.w(TAG, f"session mirror skipped session={sid[:24]}: {exc!r}")


def append_allocation_log(
    *,
    project_id: str,
    action: str,
    message: str = "",
    env: str = "",
    run_id: str = "",
    case_id: str = "",
    sn: str = "",
    package_id: str = "",
    account_id: str = "",
    account_ident: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    pid = str(project_id or "").strip()
    act = str(action or "").strip()[:48]
    if not pid or not act:
        return
    detail_out = dict(detail or {})
    sid = _resolve_session_id(run_id, case_id=case_id)
    if sid:
        detail_out["session_id"] = sid
    row = ResourceAllocationLog(
        created_at=_now_iso(),
        project_id=pid[:64],
        env=str(env or "")[:32],
        run_id=str(run_id or "")[:80],
        case_id=str(case_id or "")[:80],
        sn=str(sn or "")[:64],
        package_id=str(package_id or "")[:120],
        account_id=str(account_id or "")[:64],
        account_ident=str(account_ident or "")[:80],
        action=act,
        message=str(message or "")[:2000],
        detail_json=detail_out,
    )
    log_id = 0
    try:
        with session_scope() as db:
            db.add(row)
            db.flush()
            log_id = int(row.id or 0)
    except Exception:
        return
    if log_id and sid:
        _mirror_allocation_session_log(
            session_id=sid,
            action=act,
            message=str(message or ""),
            run_id=str(run_id or ""),
            case_id=str(case_id or ""),
            account_ident=str(account_ident or ""),
            allocation_log_id=log_id,
            detail=detail_out,
        )


def list_allocation_logs(
    project_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
    run_id: str = "",
    case_id: str = "",
    action: str = "",
    account_ident: str = "",
    sn: str = "",
    env: str = "",
) -> dict[str, Any]:
    pid = str(project_id or "").strip()
    pg = max(1, int(page or 1))
    ps = max(1, min(int(page_size or 20), 100))
    with session_scope() as db:
        q = db.query(ResourceAllocationLog).filter(ResourceAllocationLog.project_id == pid)
        rid = str(run_id or "").strip()
        if rid:
            q = q.filter(ResourceAllocationLog.run_id.contains(rid))
        cid = str(case_id or "").strip()
        if cid:
            q = q.filter(ResourceAllocationLog.case_id.contains(cid))
        act = str(action or "").strip()
        if act:
            q = q.filter(ResourceAllocationLog.action == act)
        ident_q = str(account_ident or "").strip()
        if ident_q:
            q = q.filter(ResourceAllocationLog.account_ident.contains(ident_q))
        sn_q = str(sn or "").strip()
        if sn_q:
            q = q.filter(ResourceAllocationLog.sn.contains(sn_q))
        env_q = str(env or "").strip()
        if env_q:
            q = q.filter(ResourceAllocationLog.env == env_q)
        total = q.count()
        rows = (
            q.order_by(ResourceAllocationLog.id.desc())
            .offset((pg - 1) * ps)
            .limit(ps)
            .all()
        )
        items = [
            {
                "id": r.id,
                "created_at": r.created_at or "",
                "project_id": r.project_id or "",
                "env": r.env or "",
                "run_id": r.run_id or "",
                "case_id": r.case_id or "",
                "sn": r.sn or "",
                "package_id": r.package_id or "",
                "account_id": r.account_id or "",
                "account_ident": r.account_ident or "",
                "action": r.action or "",
                "message": r.message or "",
                "detail": r.detail_json if isinstance(r.detail_json, dict) else {},
                "session_id": (
                    (r.detail_json or {}).get("session_id")
                    if isinstance(r.detail_json, dict)
                    else ""
                )
                or _resolve_session_id(str(r.run_id or ""), case_id=str(r.case_id or "")),
            }
            for r in rows
        ]
    return {"items": items, "total": total, "page": pg, "page_size": ps}


def account_recent_pick_penalty(
    project_id: str,
    account_id: str,
    *,
    run_id: str = "",
    holder_sn: str = "",
) -> tuple[int, str]:
    """跨任务/跨设备近期用过则降权，避免每轮都命中同一账号。"""
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    rid = str(run_id or "").strip()
    sn = str(holder_sn or "").strip()
    if not pid or not aid:
        return 0, ""
    cutoff = time.time() - RECENT_PICK_WINDOW_SEC
    with session_scope() as db:
        rows = (
            db.query(ResourceAllocationLog)
            .filter(
                ResourceAllocationLog.project_id == pid,
                ResourceAllocationLog.account_id == aid,
                ResourceAllocationLog.action == "lease_claim",
            )
            .order_by(ResourceAllocationLog.id.desc())
            .limit(12)
            .all()
        )
        snapshots = [
            {
                "run_id": str(r.run_id or "").strip(),
                "sn": str(r.sn or "").strip(),
                "created_at": str(r.created_at or ""),
            }
            for r in rows
        ]
    best_pen = 0
    reason = ""
    for row in snapshots:
        other_run = row["run_id"]
        other_sn = row["sn"]
        if other_run == rid and (not sn or not other_sn or other_sn == sn):
            continue
        ts = row["created_at"]
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = time.time() - dt.timestamp()
        except ValueError:
            age = 0
        if age > RECENT_PICK_WINDOW_SEC:
            continue
        frac = max(0.0, 1.0 - age / RECENT_PICK_WINDOW_SEC)
        pen = int(MAX_RECENT_PICK_PENALTY * frac)
        if pen > best_pen:
            best_pen = pen
            reason = f"近期占用({other_run[:10] or '?'})"
    return best_pen, reason
