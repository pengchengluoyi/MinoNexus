"""Only a fresh, independent expected-result proof may supersede an overlay report."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


def review_candidate(cursor: Any, ctx: Any, shot: Any, writer: Any, kind: str) -> dict | None:
    from mino_nexus.loop.llm_step_context import step_scope_key
    from mino_nexus.loop.milestones import read_state
    from mino_nexus.loop.visual_review import _prior_still_current
    from mino_nexus.action_space.scheme import execution_scheme

    prior = getattr(cursor, "visual_prior", None)
    image = str(getattr(shot, "image_base64", "") or "")
    image_ref = str(getattr(ctx, "decision_screenshot_hash", "") or "")
    if (kind != "overlay" or execution_scheme(ctx) != "visual"
            or getattr(cursor, "phase", "") != "do" or writer is None
            or not image or not image_ref or not isinstance(prior, dict)):
        return None
    mid = str(prior.get("milestone_id") or "")
    attempt = str(prior.get("attempt_ref") or "")
    expected = str(prior.get("expected") or "").strip()
    dispatched = int(prior.get("dispatch_turn") or 0)
    if (not mid or not attempt or not expected or dispatched <= 0
            or int(getattr(writer, "turn", 0) or 0) <= dispatched
            or prior.get("source") in {"interrupt", "screen_recovery"}
            or not _prior_still_current(read_state(cursor), mid)):
        return None
    state = read_state(cursor)
    if (state.get("case_step"), state.get("phase")) != step_scope_key(cursor):
        return None
    key = (step_scope_key(cursor), attempt)
    if getattr(cursor, "obstacle_expected_review_attempt", None) == key:
        return None
    return {"key": key, "prior": deepcopy(prior), "scope": step_scope_key(cursor),
            "milestone_id": mid, "attempt_ref": attempt, "expected": expected,
            "image_ref": image_ref, "raw_image_hash": hashlib.sha256(image.encode()).hexdigest()[:20]}


def proof_accepted(result: Any) -> bool:
    rows = getattr(result, "checkpoint_results", None)
    return (getattr(result, "passed", None) is True
            and not getattr(result, "error_kind", "") and not getattr(result, "parse_warnings", [])
            and isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
            and rows[0].get("point_id") == "expected_action_result"
            and rows[0].get("passed") is True and bool(str(rows[0].get("evidence") or "").strip()))


def review_expected_overlay(cursor: Any, ctx: Any, shot: Any, *, writer: Any, kind: str) -> bool:
    candidate = review_candidate(cursor, ctx, shot, writer, kind)
    if candidate is None:
        return False
    cursor.obstacle_expected_review_attempt = candidate["key"]  # one read-only model call per attempt
    from mino_nexus.loop.vision_assert import vision_assert_checkpoints
    result = vision_assert_checkpoints(
        expectation=candidate["expected"],
        image_base64=str(shot.image_base64), image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
        checkpoints=[{"point_id": "expected_action_result", "description": candidate["expected"]}],
        context_block=json.dumps({"purpose": "dispatched_action_result", "attempt_ref": candidate["attempt_ref"]}),
        run_context=ctx,
    )
    from mino_nexus.loop.llm_step_context import step_scope_key
    from mino_nexus.loop.milestones import read_state
    from mino_nexus.loop.visual_review import _prior_still_current, _confirm_visual_prior
    current = (step_scope_key(cursor) == candidate["scope"]
               and getattr(cursor, "visual_prior", None) == candidate["prior"]
               and str(getattr(ctx, "decision_screenshot_hash", "") or "") == candidate["image_ref"]
               and _prior_still_current(read_state(cursor), candidate["milestone_id"]))
    accepted = current and proof_accepted(result)
    writer.append("review/expected_overlay", {
        "attempt_ref": candidate["attempt_ref"], "milestone_id": candidate["milestone_id"],
        "observation_ref": candidate["image_ref"], "raw_image_hash": candidate["raw_image_hash"],
        "observation_turn": int(getattr(writer, "turn", 0) or 0),
        "passed": accepted, "proof_current": current, "error_kind": result.error_kind,
        "parse_warnings": list(result.parse_warnings), "checkpoint_results": result.checkpoint_results,
        "execution_started": False, "business_verdict_unchanged": True,
    })
    if not accepted:
        return False
    _confirm_visual_prior(cursor, ctx, candidate["milestone_id"],
                          capability_id=str(candidate["prior"].get("capability_id") or ""), writer=writer)
    cursor.visual_prior = None
    cursor.pending_visual_review = None
    cursor.semantic_stop = ""
    gate = getattr(cursor, "progress_gate", None)
    if gate is not None and hasattr(gate, "note_submilestone_pass"):
        gate.note_submilestone_pass()
    return True
