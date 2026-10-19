"""读写项目 collab 自动化策略。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from mino_nexus.models.collab_policy import CollabPolicy

_DEFAULTS: dict[str, Any] = {
    "auto_apply_drafts": True,
    "auto_compile": True,
    "auto_execute": True,
    "execute_env_profiles": ["test"],
    "execute_surfaces": [],
    "auto_publish": False,
    "auto_retry_toolchain": 1,
    "max_parallel_runs": 1,
    "daily_llm_budget_tokens": 0,
    "notify": ["workbench"],
    "watch_manual_runs": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool_int(value: Any, default: bool) -> int:
    if value is None:
        return 1 if default else 0
    if isinstance(value, bool):
        return 1 if value else 0
    return 1 if int(value) else 0


def _policy_row_public(row: CollabPolicy) -> dict[str, Any]:
    profiles = row.execute_env_profiles if isinstance(row.execute_env_profiles, list) else _DEFAULTS["execute_env_profiles"]
    surfaces = row.execute_surfaces if isinstance(row.execute_surfaces, list) else []
    notify = row.notify if isinstance(row.notify, list) and row.notify else _DEFAULTS["notify"]
    return {
        "policy_id": row.policy_id,
        "project_id": row.project_id,
        "app_id": str(row.app_id or ""),
        "auto_apply_drafts": bool(row.auto_apply_drafts),
        "auto_compile": bool(row.auto_compile),
        "auto_execute": bool(row.auto_execute),
        "execute_env_profiles": list(profiles),
        "execute_surfaces": list(surfaces),
        "auto_publish": bool(row.auto_publish),
        "auto_retry_toolchain": int(row.auto_retry_toolchain or 0),
        "max_parallel_runs": max(1, int(row.max_parallel_runs or 1)),
        "daily_llm_budget_tokens": int(row.daily_llm_budget_tokens or 0),
        "notify": list(notify),
        "watch_manual_runs": bool(row.watch_manual_runs),
        "updated_at": row.updated_at or "",
    }


def _merge_override(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(base)
    if not isinstance(override, dict):
        return out
    for key in _DEFAULTS:
        if key in override and override[key] is not None:
            out[key] = override[key]
    return out


def get_policy(project_id: str, *, app_id: str = "", bot_override: dict[str, Any] | None = None) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope

    pid = str(project_id or "").strip()
    aid = str(app_id or "").strip()
    with session_scope() as db:
        row = (
            db.query(CollabPolicy)
            .filter_by(project_id=pid, app_id=aid)
            .order_by(CollabPolicy.updated_at.desc())
            .first()
        )
        if row is None and aid:
            row = db.query(CollabPolicy).filter_by(project_id=pid, app_id="").first()
        if row is None:
            base = {**_DEFAULTS, "policy_id": "", "project_id": pid, "app_id": aid, "updated_at": ""}
            return _merge_override(base, bot_override)
        return _merge_override(_policy_row_public(row), bot_override)


def policy_for_topic(project_id: str, app_id: str, bot_policy_override: dict[str, Any] | None) -> dict[str, Any]:
    return get_policy(project_id, app_id=app_id, bot_override=bot_policy_override)


def put_policy(project_id: str, body: dict[str, Any], *, app_id: str = "") -> dict[str, Any]:
    from mino_nexus.core.database import session_scope

    pid = str(project_id or "").strip()
    aid = str(app_id or body.get("app_id") or "").strip()
    now = _now()
    with session_scope() as db:
        row = db.query(CollabPolicy).filter_by(project_id=pid, app_id=aid).first()
        if row is None:
            row = CollabPolicy(
                policy_id=str(uuid.uuid4()),
                project_id=pid,
                app_id=aid,
                execute_env_profiles=_DEFAULTS["execute_env_profiles"],
                notify=_DEFAULTS["notify"],
                updated_at=now,
            )
            db.add(row)
        if "auto_apply_drafts" in body:
            row.auto_apply_drafts = _bool_int(body["auto_apply_drafts"], True)
        if "auto_compile" in body:
            row.auto_compile = _bool_int(body["auto_compile"], True)
        if "auto_execute" in body:
            row.auto_execute = _bool_int(body["auto_execute"], True)
        if "execute_env_profiles" in body and isinstance(body["execute_env_profiles"], list):
            row.execute_env_profiles = list(body["execute_env_profiles"])
        if "execute_surfaces" in body and isinstance(body["execute_surfaces"], list):
            row.execute_surfaces = list(body["execute_surfaces"])
        if "auto_publish" in body:
            row.auto_publish = _bool_int(body["auto_publish"], False)
        if "auto_retry_toolchain" in body:
            row.auto_retry_toolchain = int(body["auto_retry_toolchain"] or 0)
        if "max_parallel_runs" in body:
            row.max_parallel_runs = max(1, int(body["max_parallel_runs"] or 1))
        if "daily_llm_budget_tokens" in body:
            row.daily_llm_budget_tokens = int(body["daily_llm_budget_tokens"] or 0)
        if "notify" in body and isinstance(body["notify"], list):
            row.notify = list(body["notify"])
        if "watch_manual_runs" in body:
            row.watch_manual_runs = _bool_int(body["watch_manual_runs"], False)
        row.updated_at = now
        db.flush()
        return _policy_row_public(row)
