"""PRD/用例驱动的待验证逻辑块与 version_facets 候选（仅写 candidates，不进 runtime）。"""
from __future__ import annotations

import hashlib
from typing import Any


def _cid(prefix: str, *parts: str) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return f"{prefix}.{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def propose_expectation_candidates(
    app_id: str,
    *,
    atlas_doc: dict[str, Any] | None,
    case_docs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """从 Atlas 与用例步骤生成 pending 候选（v2 最小实现）。"""
    out: list[dict[str, Any]] = []
    doc = atlas_doc if isinstance(atlas_doc, dict) else {}
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    blocks = meta.get("flow_blocks") if isinstance(meta.get("flow_blocks"), list) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        bid = str(block.get("flow_block_id") or "")
        if not bid:
            continue
        out.append(
            {
                "candidate_id": _cid("cand.flow_block", app_id, bid),
                "kind": "flow_block_proposal",
                "path": f"meta.flow_blocks[id={bid}]",
                "proposed_value": dict(block),
                "confidence": 0.55,
                "status": "pending",
                "verification_status": "pending",
                "evidence": [{"source": "atlas_synthesis"}],
            }
        )
    for case in case_docs or []:
        cid = str(case.get("case_id") or "")
        steps = case.get("steps") or []
        if not cid or not steps:
            continue
        text = " ".join(str(s) for s in steps[:6])[:240]
        out.append(
            {
                "candidate_id": _cid("cand.case_flow_hint", app_id, cid),
                "kind": "case_flow_hint",
                "path": f"cases[{cid}].flow_hint",
                "proposed_value": {"case_id": cid, "hint": text},
                "confidence": 0.35,
                "status": "pending",
                "verification_status": "pending",
                "evidence": [{"case_id": cid}],
            }
        )
    return out


def merge_candidates_into_batch(batch: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    b = dict(batch or {})
    prev = list(b.get("candidates") or [])
    seen = {str(c.get("candidate_id") or "") for c in prev}
    for row in rows:
        cid = str(row.get("candidate_id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        prev.append(row)
    b["candidates"] = prev
    b["pending_count"] = sum(1 for c in prev if str(c.get("status") or "") == "pending")
    return b
