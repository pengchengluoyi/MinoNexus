"""号池账号 SQLite 真源；从 projects.env.test_accounts 一次性迁入。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from mino_nexus.core.database import session_scope
from mino_nexus.models.pool_account import PoolAccount, PoolAccountFacet


def _now() -> int:
    return int(time.time())


def _facets_for_account(db, project_id: str, account_id: str) -> dict[str, str]:
    rows = (
        db.query(PoolAccountFacet)
        .filter(
            PoolAccountFacet.project_id == project_id,
            PoolAccountFacet.account_id == account_id,
        )
        .all()
    )
    return {str(r.facet_key): str(r.facet_value or "unknown") for r in rows}


def _registered_at_iso(ts: int) -> str:
    if not ts or int(ts) <= 0:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OSError, ValueError, OverflowError):
        return ""


def _account_to_dict(row: PoolAccount, facets: dict[str, str]) -> dict[str, Any]:
    created = int(row.created_at or 0)
    return {
        "id": row.id,
        "account_id": row.id,
        "display_name": row.display_name or "",
        "name": row.display_name or "",
        "env": row.env or "test",
        "kind": row.kind or "mixed",
        "phone": row.phone or "",
        "email": row.email or "",
        "username": row.username or "",
        "password": row.password or "",
        "otp": row.otp or "",
        "profile_id": row.profile_id or "",
        "note": row.note or "",
        "locked": bool(row.locked),
        "lease": row.lease if isinstance(row.lease, dict) else {},
        "facets": dict(facets),
        "created_at": created,
        "registered_at": _registered_at_iso(created),
        "updated_at": int(row.updated_at or 0),
    }


def _write_facets(db, project_id: str, account_id: str, facets: dict[str, Any]) -> None:
    db.query(PoolAccountFacet).filter(
        PoolAccountFacet.project_id == project_id,
        PoolAccountFacet.account_id == account_id,
    ).delete(synchronize_session=False)
    for key, val in (facets or {}).items():
        k = str(key or "").strip()
        if not k:
            continue
        v = str(val or "").strip().lower() or "unknown"
        db.add(
            PoolAccountFacet(
                project_id=project_id,
                account_id=account_id,
                facet_key=k[:32],
                facet_value=v[:64],
            )
        )


def _upsert_row(db, project_id: str, row: dict[str, Any]) -> None:
    aid = str(row.get("account_id") or row.get("id") or "").strip()
    if not aid:
        return
    now = _now()
    existing = (
        db.query(PoolAccount)
        .filter(PoolAccount.project_id == project_id, PoolAccount.id == aid)
        .one_or_none()
    )
    if existing is None:
        existing = PoolAccount(id=aid, project_id=project_id, created_at=now)
        db.add(existing)
    existing.env = str(row.get("env") or "test")[:32]
    existing.display_name = str(row.get("display_name") or "")[:80]
    existing.phone = str(row.get("phone") or "")[:32]
    existing.email = str(row.get("email") or "")[:80]
    existing.username = str(row.get("username") or "")[:80]
    existing.password = str(row.get("password") or "")[:120]
    existing.otp = str(row.get("otp") or "")[:32]
    existing.kind = str(row.get("kind") or "mixed")[:24]
    existing.profile_id = str(row.get("profile_id") or "")[:40]
    existing.note = str(row.get("note") or "")[:200]
    existing.locked = 1 if row.get("locked") else 0
    lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
    existing.lease = lease
    existing.updated_at = now
    facets = row.get("facets") if isinstance(row.get("facets"), dict) else {}
    _write_facets(db, project_id, aid, facets)


def count_accounts(project_id: str) -> int:
    pid = str(project_id or "").strip()
    if not pid:
        return 0
    with session_scope() as db:
        return db.query(PoolAccount).filter(PoolAccount.project_id == pid).count()


def merge_env_test_accounts(project_id: str, env_doc: dict | None) -> int:
    """把 projects.env.test_accounts 合并进 DB（按 id upsert，不删库里已有行）。"""
    from mino_nexus.services.project_env import _norm_test_accounts

    pid = str(project_id or "").strip()
    if not pid:
        return 0
    doc = env_doc if isinstance(env_doc, dict) else {}
    raw = doc.get("test_accounts")
    if not isinstance(raw, list) or not raw:
        return 0
    rows = _norm_test_accounts(raw, env_doc=doc)
    if not rows:
        return 0
    with session_scope() as db:
        for row in rows:
            _upsert_row(db, pid, row)
    return len(rows)


def list_accounts(project_id: str, env_doc: dict | None = None) -> list[dict[str, Any]]:
    pid = str(project_id or "").strip()
    if not pid:
        return []
    merge_env_test_accounts(pid, env_doc)
    with session_scope() as db:
        accounts = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid)
            .order_by(PoolAccount.updated_at.desc())
            .all()
        )
        out: list[dict[str, Any]] = []
        for acc in accounts:
            facets = _facets_for_account(db, pid, acc.id)
            out.append(_account_to_dict(acc, facets))
        return out


def replace_all(project_id: str, rows: list[dict[str, Any]]) -> None:
    pid = str(project_id or "").strip()
    if not pid:
        return
    with session_scope() as db:
        db.query(PoolAccountFacet).filter(PoolAccountFacet.project_id == pid).delete(
            synchronize_session=False
        )
        db.query(PoolAccount).filter(PoolAccount.project_id == pid).delete(synchronize_session=False)
        for row in rows or []:
            _upsert_row(db, pid, row)


def upsert_one(project_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
    pid = str(project_id or "").strip()
    if not pid:
        return None
    with session_scope() as db:
        _upsert_row(db, pid, row)
        db.flush()
        aid = str(row.get("account_id") or row.get("id") or "").strip()
        acc = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .one_or_none()
        )
        if not acc:
            return None
        facets = _facets_for_account(db, pid, acc.id)
        return _account_to_dict(acc, facets)


def delete_one(project_id: str, account_id: str) -> bool:
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    if not pid or not aid:
        return False
    with session_scope() as db:
        db.query(PoolAccountFacet).filter(
            PoolAccountFacet.project_id == pid,
            PoolAccountFacet.account_id == aid,
        ).delete(synchronize_session=False)
        n = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .delete(synchronize_session=False)
        )
        return n > 0


def set_account_lease(project_id: str, account_id: str, lease: dict[str, Any] | None) -> bool:
    """只更新 lease 列，避免 replace_all 整表重写。"""
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    if not pid or not aid:
        return False
    payload = lease if isinstance(lease, dict) else {}
    with session_scope() as db:
        row = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .one_or_none()
        )
        if row is None:
            return False
        row.lease = payload
        row.updated_at = _now()
        return True


def try_claim_account_lease(
    project_id: str,
    account_id: str,
    lease: dict[str, Any],
) -> bool:
    """原子占号：仅当未被其它 run/设备占用（或同 run+同 sn 续租）时写入。"""
    from mino_nexus.services.resource_pool import lease_blocks_other_holder

    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    payload = dict(lease or {})
    if not pid or not aid or not str(payload.get("run_id") or "").strip():
        return False
    rid = str(payload.get("run_id") or "").strip()
    sn = str(payload.get("sn") or "").strip()
    with session_scope() as db:
        row = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .one_or_none()
        )
        if row is None:
            return False
        existing = row.lease if isinstance(row.lease, dict) else {}
        if lease_blocks_other_holder(existing, run_id=rid, sn=sn):
            return False
        row.lease = payload
        row.updated_at = _now()
        return True


def clear_leases_for_run(project_id: str, run_id: str) -> int:
    """清空指定 run 在号池上的租约，返回释放条数。"""
    pid = str(project_id or "").strip()
    rid = str(run_id or "").strip()
    if not pid or not rid:
        return 0
    n = 0
    with session_scope() as db:
        rows = db.query(PoolAccount).filter(PoolAccount.project_id == pid).all()
        for row in rows:
            lease = row.lease if isinstance(row.lease, dict) else {}
            if str(lease.get("run_id") or "").strip() != rid:
                continue
            try:
                from mino_nexus.services.resource_allocation_log import append_allocation_log
                from mino_nexus.services.project_env import account_ident as _ident_fn

                stub = {
                    "id": row.id,
                    "phone": row.phone,
                    "email": row.email,
                    "username": row.username,
                    "display_name": row.display_name,
                }
                ident = _ident_fn(stub)
                append_allocation_log(
                    project_id=pid,
                    action="lease_release",
                    message=f"跑批结束释放 {ident}",
                    run_id=rid,
                    case_id=str(lease.get("case_id") or "")[:80],
                    sn=str(lease.get("sn") or "")[:64],
                    account_id=str(row.id or ""),
                    account_ident=ident,
                    detail={"reason": "run_end"},
                )
            except Exception:
                pass
            row.lease = {}
            row.updated_at = _now()
            n += 1
    return n


def env_without_test_accounts_blob(doc: dict[str, Any]) -> dict[str, Any]:
    """号池真源在 pool_accounts 表，env JSON 不再重复整表。"""
    out = dict(doc or {})
    out.pop("test_accounts", None)
    return out


def env_has_test_accounts_blob(doc: dict[str, Any] | None) -> bool:
    """project.env 是否仍带 legacy test_accounts 键（含空列表）。"""
    return "test_accounts" in (doc or {})


def persist_env_strip_test_accounts_if_needed(project_id: str, env_doc: dict | None) -> bool:
    """若 env 仍含 legacy test_accounts，迁入 DB 后从 project.env 去掉；否则不写 env。"""
    doc = env_doc if isinstance(env_doc, dict) else {}
    if not env_has_test_accounts_blob(doc):
        return False
    merge_env_test_accounts(project_id, doc)
    from mino_nexus.services import project_store as ps

    ps.save_project_env(project_id, env_without_test_accounts_blob(doc))
    return True
