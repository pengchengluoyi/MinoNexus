"""统一用例草稿应用与去重（§3.5）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.casework.case_store import find_similar_case, get_case, list_cases, upsert_cases


def _norm_title(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").strip().lower())


def _validate_row(row: dict[str, Any]) -> str | None:
    title = str(row.get("name") or row.get("title") or "").strip()
    if not title:
        return "missing_title"
    steps = row.get("steps") or row.get("steps_raw") or ""
    if isinstance(steps, list):
        steps = "\n".join(str(x) for x in steps)
    if not str(steps).strip():
        return "empty_steps"
    platform = str(row.get("platform") or "android").strip().lower()
    if platform not in ("android", "ios", "web", "pc", "harmony"):
        return "invalid_platform"
    return None


def _payload_cases(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(artifact, dict):
        return []
    inner = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else artifact
    if not isinstance(inner, dict):
        return []
    raw = inner.get("cases") or inner.get("draft_cases") or []
    return [dict(x) for x in raw if isinstance(x, dict)]


def apply_case_drafts(
    project_id: str,
    requirement_id: str,
    artifact: dict[str, Any],
    *,
    input_version: str = "",
    replace: bool = False,
) -> dict[str, Any]:
    incoming = _payload_cases(artifact)
    added: list[str] = []
    modified: list[str] = []
    skipped_duplicate: list[str] = []
    needs_merge: list[dict[str, Any]] = []
    to_write: list[dict[str, Any]] = []
    rid = str(requirement_id or "").strip()

    for row in incoming:
        err = _validate_row(row)
        if err:
            skipped_duplicate.append(str(row.get("case_id") or row.get("name") or err))
            continue
        title = str(row.get("name") or row.get("title") or "").strip()
        norm = _norm_title(title)
        case_id = str(row.get("case_id") or "").strip()
        existing = get_case(project_id, case_id) if case_id else None
        if existing is None:
            existing = find_similar_case(project_id, rid, row)
        if existing:
            cid = str(existing.get("case_id") or "")
            expected = row.get("expected_updated_at") or row.get("updated_at")
            if expected and str(existing.get("updated_at") or "") != str(expected):
                needs_merge.append({"case_id": cid, "title": title, "reason": "updated_since_snapshot"})
                continue
            if _norm_title(existing.get("name") or existing.get("title") or "") == norm:
                patch = dict(row)
                patch["case_id"] = cid
                to_write.append(patch)
                modified.append(cid)
                continue
            skipped_duplicate.append(title)
            continue
        patch = dict(row)
        to_write.append(patch)
        added.append(str(patch.get("case_id") or title))

    if to_write:
        upsert_cases(project_id, rid, to_write, replace=replace, collab_emit=False)

    evidence = {
        "added": added,
        "modified": modified,
        "skipped_duplicate": skipped_duplicate,
        "needs_merge": needs_merge,
        "input_version": input_version,
        "total_existing": len(list_cases(project_id, rid)),
    }
    return evidence
