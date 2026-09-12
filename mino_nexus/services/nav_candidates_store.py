"""v2.5 NavFSM 字段候选：审核前不写 `nav_fsm*`。"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from mino_nexus.core.paths import nav_candidates_dir

LATEST_NAME = "latest.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _latest_path(app_id: str) -> Path:
    return nav_candidates_dir(app_id) / LATEST_NAME


def load_batch(app_id: str) -> dict[str, Any]:
    return _read_json(_latest_path(app_id), {"app_id": app_id, "batch_id": "", "candidates": []})


def save_batch(app_id: str, batch: dict[str, Any]) -> dict[str, Any]:
    body = dict(batch or {})
    if not body.get("batch_id"):
        body["batch_id"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    body["app_id"] = str(app_id or "")
    body["updated_at"] = int(time.time())
    _write_json(_latest_path(app_id), body)
    bid = str(body.get("batch_id") or "")
    if bid:
        _write_json(nav_candidates_dir(app_id, bid), body)
    return body


def append_candidates(app_id: str, rows: list[dict[str, Any]], *, source: str = "") -> dict[str, Any]:
    batch = load_batch(app_id)
    if not batch.get("batch_id"):
        batch["batch_id"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        batch["generated_at"] = int(time.time())
        batch["source"] = source
    existing = {str(c.get("candidate_id") or ""): c for c in (batch.get("candidates") or [])}
    for row in rows:
        cid = str(row.get("candidate_id") or "").strip() or f"cand.{uuid.uuid4().hex[:10]}"
        prev = existing.get(cid) or {}
        merged = {**prev, **row, "candidate_id": cid, "status": row.get("status") or prev.get("status") or "pending"}
        existing[cid] = merged
    batch["candidates"] = list(existing.values())
    batch["pending_count"] = sum(1 for c in batch["candidates"] if c.get("status") == "pending")
    return save_batch(app_id, batch)


def review_candidate(app_id: str, candidate_id: str, *, status: str, note: str = "") -> dict[str, Any]:
    batch = load_batch(app_id)
    found = False
    for row in batch.get("candidates") or []:
        if str(row.get("candidate_id") or "") == str(candidate_id):
            row["status"] = str(status or "pending")
            row["review_note"] = str(note or "")
            row["reviewed_at"] = int(time.time())
            found = True
            break
    if not found:
        raise KeyError(f"候选不存在：{candidate_id}")
    batch["pending_count"] = sum(1 for c in batch.get("candidates") or [] if c.get("status") == "pending")
    return save_batch(app_id, batch)


def accepted_candidates(app_id: str) -> list[dict[str, Any]]:
    batch = load_batch(app_id)
    return [c for c in (batch.get("candidates") or []) if c.get("status") == "accepted"]
