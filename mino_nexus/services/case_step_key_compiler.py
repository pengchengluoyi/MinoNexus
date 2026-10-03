"""用例步骤：操作 / 预期编号行 → step_program_keys + do/check_program_plan（保存时编译）。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from mino_nexus.ai.case_text import parse_numbered_items_rules
from mino_nexus.loop.step_pointer import build_seq_nodes

_BLOCK_RE = re.compile(r"【块[:：]\s*([^】]+)】")
_NAV_RE = re.compile(r"【导航[:：]\s*([^】]+)】")
_CAP_RE = re.compile(r"【cap[:：]\s*([^】]+)】", re.I)
_KEY_REF_RE = re.compile(r"\b((?:operation|expected)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*)\b", re.I)
_COMPOUND_SPLIT = re.compile(r"[；;。，,]|(?:\s+然后\s+)|(?:\s+并\s+)")
_SLUG_BAD = re.compile(r"[^\w]+", re.UNICODE)
_SPATIAL_HINTS: tuple[tuple[str, str], ...] = (
    ("右上", "top_right"),
    ("右下", "bottom_right"),
    ("左上", "top_left"),
    ("左下", "bottom_left"),
    ("底部", "bottom"),
    ("底栏", "bottom"),
    ("顶部", "top"),
    ("中间", "center"),
)


def _norm(s: str) -> str:
    t = unicodedata.normalize("NFKC", str(s or "").strip().lower())
    return re.sub(r"\s+", "", t)


def _slug(text: str, *, prefix: str = "operation") -> str:
    raw = _SLUG_BAD.sub("_", _norm(text))[:48].strip("_")
    return f"{prefix}.generic.{raw or 'line'}"


def _catalog_by_layer(layer: str) -> list[dict[str, Any]]:
    from mino_nexus.services.case_resource_key_catalog import catalog_payload

    return [
        e
        for e in (catalog_payload().get("entries") or [])
        if isinstance(e, dict) and str(e.get("key_layer") or "") == layer
    ]


def _key_ref_of(entry: dict[str, Any]) -> str:
    ref = str(entry.get("key_ref") or "").strip()
    if ref:
        return ref
    sec = str(entry.get("section") or "key").strip().lower()
    cat = str(entry.get("write_category") or "item").strip()
    slug = _SLUG_BAD.sub("_", _norm(cat))[:40].strip("_")
    return f"{sec}.{slug}"


def _match_catalog_clause(clause: str, layer: str) -> tuple[dict[str, Any] | None, str]:
    text = str(clause or "").strip()
    if not text:
        return None, ""
    for m in _KEY_REF_RE.finditer(text):
        ref = m.group(1).lower()
        for ent in _catalog_by_layer(layer):
            if _key_ref_of(ent).lower() == ref:
                return ent, ref
    block = _BLOCK_RE.search(text)
    if block and layer == "operation":
        bid = block.group(1).strip()
        from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID
        from mino_nexus.services.case_key_registry import default_block_key_ref
        from mino_nexus.services.nav_flow_block_catalog import load_block_row, normalize_block_id

        canon = normalize_block_id(bid)
        row = load_block_row(app_id=GLOBAL_APP_ID, block_id=canon)
        ref = str((row or {}).get("key_ref") or "").strip() or default_block_key_ref(canon)
        return {
            "key_ref": ref,
            "kind": "flow_block",
            "block_id": canon,
        }, ref
    nav = _NAV_RE.search(text)
    if nav and layer == "operation":
        sid = nav.group(1).strip()
        from mino_nexus.services.case_key_registry import default_screen_key_ref

        ref = default_screen_key_ref(sid)
        return {
            "key_ref": ref,
            "kind": "nav",
            "nav_target": sid,
        }, ref
    cap = _CAP_RE.search(text)
    if cap and layer == "operation":
        cid = cap.group(1).strip()
        return {
            "key_ref": f"operation.cap.{_slug(cid, prefix='')}",
            "kind": "hook",
            "hook_cap": cid,
        }, f"operation.cap.{cid}"
    nt = _norm(text)
    best: dict[str, Any] | None = None
    best_len = 0
    best_ref = ""
    for ent in _catalog_by_layer(layer):
        for ex in ent.get("write_examples") or []:
            exs = str(ex or "").strip()
            if not exs:
                continue
            nex = _norm(exs)
            if nex in nt or nt in nex:
                if len(nex) > best_len:
                    best_len = len(nex)
                    best = ent
                    best_ref = _key_ref_of(ent)
    if best is not None:
        return best, best_ref
    return None, ""


def split_clauses(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    items = parse_numbered_items_rules(raw)
    bodies: list[str] = []
    if len(items) <= 1:
        bodies.append(_strip_op_prefix(raw))
    else:
        for it in items:
            b = str(it.get("text") or "").strip()
            if b:
                bodies.append(_strip_op_prefix(b))
    out: list[str] = []
    for body in bodies:
        for part in _COMPOUND_SPLIT.split(body):
            p = part.strip()
            if len(p) >= 2:
                out.append(p)
    return out or ([_strip_op_prefix(raw)] if raw else [])


def _strip_op_prefix(text: str) -> str:
    t = str(text or "").strip()
    for prefix in ("操作：", "操作:", "预期：", "预期:"):
        if t.startswith(prefix):
            return t[len(prefix) :].strip()
    return t


def _spatial_hint(clause: str) -> str:
    raw = str(clause or "")
    for token, hint in _SPATIAL_HINTS:
        if token in raw:
            return hint
    return ""


def _cap_block_reason(cap_id: str) -> str:
    cid = str(cap_id or "").strip()
    if not cid:
        return ""
    from mino_nexus.catalog.skill_channel import capability_enabled, capability_ui_coverable

    if not capability_enabled(cid):
        return "unavailable_cap"
    if not capability_ui_coverable(cid):
        return "uncoverable_event"
    return ""


def _blocked_item(
    *,
    phase: str,
    case_step: int,
    clause: str,
    reason: str,
    event_id: str = "",
    user_message: str = "",
    event_name: str = "",
) -> dict[str, Any]:
    row = {
        "phase": phase,
        "case_step": int(case_step),
        "source_clause": str(clause or "")[:240],
        "reason": reason,
        "event_id": str(event_id or ""),
    }
    if event_name:
        row["event_name"] = event_name
    if user_message:
        row["user_message"] = user_message[:400]
    return row


def _drop_coords(step: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(step, dict):
        return step
    for key in ("x", "y", "px", "py", "bounds"):
        step.pop(key, None)
    params = step.get("params")
    if isinstance(params, dict):
        for key in ("x", "y", "px", "py", "bounds"):
            params.pop(key, None)
    return step


def _observe_for_match(match: dict[str, Any] | None, clause: str, *, layer: str) -> str:
    row = match if isinstance(match, dict) else {}
    raw = str(row.get("observe") or "").strip().lower()
    if raw in ("exec", "visual_each_run", "program"):
        return raw
    cap = str(row.get("hook_cap") or "").strip()
    if not cap:
        keys = row.get("config_keys") if isinstance(row.get("config_keys"), list) else []
        cap = str(keys[0] or "").strip() if keys else ""
    if cap:
        try:
            from mino_nexus.catalog.skill_channel import capability_observe

            obs = capability_observe(cap)
            if obs in ("exec", "visual_each_run", "program"):
                return obs
        except Exception:  # noqa: BLE001
            pass
    kind = str(row.get("kind") or "").strip().lower()
    if kind in ("hook",) and cap in ("wait_ms", "wait_screen_ready", "lease_account", "confirm_login_state"):
        return "program"
    if layer == "expected" and "session=" in str(clause or "").lower():
        return "program"
    return "exec"


def _plan_step_from_match(
    *,
    clause: str,
    match: dict[str, Any] | None,
    key_ref: str,
    case_step: int,
    sub_idx: int,
    layer: str,
) -> dict[str, Any] | None:
    if match is None:
        return None
    observe = _observe_for_match(match, clause, layer=layer)
    hint = _spatial_hint(clause)
    params: dict[str, Any] = {}
    if hint:
        params["spatial_hint"] = hint
    if match.get("kind") == "flow_block":
        bid = str(match.get("block_id") or "").strip()
        row = {
            "id": f"do_s{case_step}_{sub_idx}_{bid}"[:64],
            "key_ref": key_ref,
            "title": clause[:240],
            "kind": "flow_block",
            "block_id": bid,
            "optional": False,
            "source_line": clause,
            "source_clause": clause,
            "subclause_index": sub_idx,
            "observe": observe,
        }
        if params:
            row["params"] = params
        return row
    if match.get("kind") == "nav":
        return {
            "id": f"do_s{case_step}_{sub_idx}_nav"[:64],
            "key_ref": key_ref,
            "title": clause[:240],
            "kind": "nav",
            "nav_target": str(match.get("nav_target") or ""),
            "optional": False,
            "source_line": clause,
            "subclause_index": sub_idx,
            "observe": observe,
        }
    if match.get("kind") == "hook" or str(match.get("hook_cap") or ""):
        cap = str(match.get("hook_cap") or match.get("config_keys", [""])[0] or "tap_element")
        row = {
            "id": f"do_s{case_step}_{sub_idx}_hook"[:64],
            "key_ref": key_ref,
            "title": clause[:240],
            "kind": "hook",
            "hook_cap": cap,
            "optional": False,
            "source_line": clause,
            "source_clause": clause,
            "subclause_index": sub_idx,
            "observe": observe,
        }
        reason = _cap_block_reason(cap)
        if reason:
            row["_block_reason"] = reason
        if params:
            row["params"] = params
        return row
    if layer == "expected":
        mode = "vlm"
        if "session=" in clause.lower():
            mode = "session"
        return {
            "id": f"ck_s{case_step}_{sub_idx}"[:64],
            "key_ref": key_ref,
            "title": clause[:240],
            "kind": "checkpoint",
            "assert": {"mode": mode, "expectation": clause[:500]},
            "optional": False,
            "source_line": clause,
            "subclause_index": sub_idx,
            "observe": observe,
        }
    dsl = str(match.get("dsl") or "")
    kind = "visual_action"
    if "flow_block" in dsl or "block_id" in dsl:
        kind = "flow_block"
    elif "nav" in dsl or "screen" in dsl:
        kind = "nav"
    elif "hook" in dsl or "cap" in dsl:
        kind = "hook"
    cfg = match.get("config_keys") if isinstance(match.get("config_keys"), list) else []
    hook_cap = str(cfg[0] or "") if cfg else ""
    if hook_cap and kind == "visual_action":
        kind = "hook"
    step: dict[str, Any] = {
        "id": f"do_s{case_step}_{sub_idx}"[:64],
        "key_ref": key_ref,
        "title": clause[:240],
        "kind": kind,
        "optional": False,
        "source_line": clause,
        "source_clause": clause,
        "subclause_index": sub_idx,
        "observe": observe,
    }
    if kind == "hook":
        step["hook_cap"] = hook_cap or "tap_element"
        reason = _cap_block_reason(str(step["hook_cap"]))
        if reason:
            step["_block_reason"] = reason
    if params:
        step["params"] = params
    return step


def _input_bind_id(do_steps: list[dict[str, Any]] | None) -> str:
    for step in reversed(do_steps or []):
        if not isinstance(step, dict):
            continue
        cap = str(step.get("hook_cap") or "")
        ref = str(step.get("key_ref") or "")
        if cap == "input_text" or ref == "operation.input_text":
            return str(step.get("id") or "")
    return ""


def _step_from_hit(hit: dict[str, Any], *, case_step: int, sub_idx: int) -> dict[str, Any]:
    clause = str(hit.get("clause") or "")
    rules = dict(hit.get("rules") or {})
    element = dict(hit.get("element") or {})
    schemes = list(hit.get("schemes") or ["visual", "dom"])
    layer = str(hit.get("layer") or "")
    if layer == "expected":
        assert_payload: dict[str, Any] = {
            "mode": str(hit.get("assert_mode") or "vlm"),
            "element": element,
            "rules": rules,
        }
        if hit.get("bind"):
            assert_payload["bind"] = hit["bind"]
        return {
            "id": f"ck_s{case_step}_{sub_idx}"[:64],
            "key_ref": str(hit.get("key_ref") or ""),
            "title": str(hit.get("event_name") or hit.get("key_ref") or clause)[:240],
            "kind": "checkpoint",
            "assert": assert_payload,
            "schemes": schemes,
            "optional": False,
            "source_line": clause[:240],
            "source_clause": clause[:240],
            "source_clauses": list(hit.get("source_clauses") or [clause]),
            "subclause_index": sub_idx,
            "observe": "exec",
        }
    params: dict[str, Any] = {}
    if element:
        params["element"] = element
    if rules:
        params["rules"] = rules
    hint = _spatial_hint(clause)
    if hint:
        params["spatial_hint"] = hint
    row = {
        "id": f"do_s{case_step}_{sub_idx}_hook"[:64],
        "key_ref": str(hit.get("key_ref") or ""),
        "title": clause[:240],
        "kind": "hook",
        "hook_cap": str(hit.get("hook_cap") or "tap_element"),
        "schemes": schemes,
        "optional": False,
        "source_line": clause[:240],
        "source_clause": clause[:240],
        "subclause_index": sub_idx,
        "observe": "exec",
    }
    if params:
        row["params"] = params
    reason = _cap_block_reason(str(row["hook_cap"]))
    if reason:
        row["_block_reason"] = reason
    return row


def _classify_block(
    hit: dict[str, Any],
    *,
    case_step: int,
    author_layer: str,
    platform: str,
) -> dict[str, Any] | None:
    """命中了句式但本层不能执行时，返回 blocked 警告。"""
    from mino_nexus.services.case_clause_events import platform_block, user_message

    clause = str(hit.get("clause") or "")
    phase = "do" if author_layer == "operation" else "check"
    if hit.get("ambiguous"):
        msg = user_message(case_step=case_step, layer=author_layer, clause=clause, reason="ambiguous_clause")
        return {
            "case_step": case_step,
            "layer": author_layer,
            "line": clause,
            "raw_text": clause,
            "blocked": _blocked_item(
                phase=phase,
                case_step=case_step,
                clause=clause,
                reason="ambiguous_clause",
                user_message=msg,
            ),
        }
    hit_layer = str(hit.get("layer") or "")
    if hit_layer and hit_layer != author_layer:
        msg = user_message(
            case_step=case_step,
            layer=author_layer,
            clause=clause,
            reason="layer_mismatch",
            event_name=str(hit.get("event_name") or ""),
        )
        return {
            "case_step": case_step,
            "layer": author_layer,
            "line": clause,
            "raw_text": clause,
            "blocked": _blocked_item(
                phase=phase,
                case_step=case_step,
                clause=clause,
                reason="layer_mismatch",
                event_id=str(hit.get("key_ref") or ""),
                event_name=str(hit.get("event_name") or ""),
                user_message=msg,
            ),
        }
    if platform_block(hit, platform):
        allowed = "、".join(str(x) for x in (hit.get("platforms") or [])) or "Web"
        msg = user_message(
            case_step=case_step,
            layer=author_layer,
            clause=clause,
            reason="platform_unsupported",
            event_name=str(hit.get("event_name") or ""),
            detail=allowed,
            platform=platform,
        )
        return {
            "case_step": case_step,
            "layer": author_layer,
            "line": clause,
            "raw_text": clause,
            "blocked": _blocked_item(
                phase=phase,
                case_step=case_step,
                clause=clause,
                reason="platform_unsupported",
                event_id=str(hit.get("key_ref") or ""),
                event_name=str(hit.get("event_name") or ""),
                user_message=msg,
            ),
        }
    return None


def compile_operation_text(
    instruction: str,
    *,
    case_step: int,
    platform: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """返回 (plan_steps, warnings, key_refs)。"""
    from mino_nexus.services.case_clause_events import classify_clause, user_message

    warnings: list[dict[str, Any]] = []
    key_refs: list[str] = []
    plan_steps: list[dict[str, Any]] = []
    clauses = split_clauses(instruction)
    for idx, clause in enumerate(clauses):
        hit = classify_clause(clause)
        if hit is not None:
            blocked = _classify_block(hit, case_step=case_step, author_layer="operation", platform=platform)
            if blocked:
                warnings.append(blocked)
                continue
            ps = _step_from_hit(hit, case_step=case_step, sub_idx=idx)
            reason = str(ps.pop("_block_reason", "") or "")
            if reason:
                msg = user_message(
                    case_step=case_step,
                    layer="operation",
                    clause=clause,
                    reason="capability_disabled" if reason == "unavailable_cap" else reason,
                    event_name=str(hit.get("event_name") or ps.get("hook_cap") or ""),
                )
                warnings.append(
                    {
                        "case_step": case_step,
                        "layer": "operation",
                        "line": clause,
                        "raw_text": clause,
                        "blocked": _blocked_item(
                            phase="do",
                            case_step=case_step,
                            clause=clause,
                            reason=reason,
                            event_id=str(ps.get("hook_cap") or ""),
                            event_name=str(hit.get("event_name") or ""),
                            user_message=msg,
                        ),
                    }
                )
                continue
            plan_steps.append(_drop_coords(ps))
            key_refs.append(str(ps.get("key_ref") or ""))
            continue
        ent, ref = _match_catalog_clause(clause, "operation")
        if ent is None:
            sug = _slug(clause)
            warnings.append(
                {
                    "case_step": case_step,
                    "layer": "operation",
                    "line": clause,
                    "raw_text": clause,
                    "unmapped": True,
                    "suggested_key_refs": [
                        {
                            "key_ref": sug,
                            "write_examples": [clause[:120]],
                            "note": "写不到具体事件，整案无法 UI 自动化",
                        }
                    ],
                    "blocked": _blocked_item(
                        phase="do",
                        case_step=case_step,
                        clause=clause,
                        reason="unmapped",
                        user_message=user_message(
                            case_step=case_step,
                            layer="operation",
                            clause=clause,
                            reason="unmapped",
                        ),
                    ),
                }
            )
            continue
        ref = ref or _key_ref_of(ent)
        key_refs.append(ref)
        expands = ent.get("expands_to") if isinstance(ent.get("expands_to"), list) else []
        if expands:
            for j, sub in enumerate(expands):
                if not isinstance(sub, dict):
                    continue
                sub_clause = str(sub.get("title") or clause).strip()
                ps = _plan_step_from_match(
                    clause=sub_clause,
                    match={**ent, **sub},
                    key_ref=str(sub.get("key_ref") or ref),
                    case_step=case_step,
                    sub_idx=idx * 10 + j,
                    layer="operation",
                )
                if ps:
                    plan_steps.append(_drop_coords(ps))
            continue
        ps = _plan_step_from_match(
            clause=clause,
            match=ent if isinstance(ent, dict) else None,
            key_ref=ref,
            case_step=case_step,
            sub_idx=idx,
            layer="operation",
        )
        if ps:
            reason = str(ps.pop("_block_reason", "") or "")
            if reason:
                warnings.append(
                    {
                        "case_step": case_step,
                        "layer": "operation",
                        "line": clause,
                        "raw_text": clause,
                        "blocked": _blocked_item(
                            phase="do",
                            case_step=case_step,
                            clause=clause,
                            reason=reason,
                            event_id=str(ps.get("hook_cap") or ps.get("key_ref") or ""),
                        ),
                    }
                )
                continue
            plan_steps.append(_drop_coords(ps))
    collapsed: list[dict[str, Any]] = []
    for step in plan_steps:
        if (
            collapsed
            and str(step.get("kind") or "") == "flow_block"
            and str(collapsed[-1].get("kind") or "") == "flow_block"
            and str(step.get("block_id") or "")
            and str(step.get("block_id") or "") == str(collapsed[-1].get("block_id") or "")
        ):
            continue
        collapsed.append(step)
    refs = [str(s.get("key_ref") or "") for s in collapsed if str(s.get("key_ref") or "")]
    return collapsed, warnings, refs


def compile_expected_text(
    expected: str,
    *,
    case_step: int,
    platform: str = "",
    do_steps: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    from mino_nexus.services.case_clause_events import (
        classify_clause,
        incomplete_reason,
        merge_dialog_checks,
        user_message,
    )

    warnings: list[dict[str, Any]] = []
    key_refs: list[str] = []
    checkpoints: list[dict[str, Any]] = []
    clauses = split_clauses(expected)
    if not clauses and str(expected or "").strip():
        clauses = [str(expected).strip()]
    pending: list[dict[str, Any]] = []
    catalog_left: list[tuple[int, str]] = []
    for idx, clause in enumerate(clauses):
        hit = classify_clause(clause)
        if hit is not None:
            blocked = _classify_block(hit, case_step=case_step, author_layer="expected", platform=platform)
            if blocked:
                warnings.append(blocked)
                continue
            hit = dict(hit)
            hit["_idx"] = idx
            pending.append(hit)
            continue
        catalog_left.append((idx, clause))
    bind_id = _input_bind_id(do_steps)
    for hit in merge_dialog_checks(pending):
        if hit.get("needs_bind"):
            if bind_id:
                hit["bind"] = {"from_step_id": bind_id, "path": "params.text"}
        missing = incomplete_reason(hit)
        clause = str(hit.get("clause") or "")
        if missing:
            msg = user_message(
                case_step=case_step,
                layer="expected",
                clause=clause,
                reason="params_incomplete",
                event_name=str(hit.get("event_name") or ""),
                detail=missing,
            )
            warnings.append(
                {
                    "case_step": case_step,
                    "layer": "expected",
                    "line": clause,
                    "raw_text": clause,
                    "blocked": _blocked_item(
                        phase="check",
                        case_step=case_step,
                        clause=clause,
                        reason="params_incomplete",
                        event_id=str(hit.get("key_ref") or ""),
                        event_name=str(hit.get("event_name") or ""),
                        user_message=msg,
                    ),
                }
            )
            continue
        ps = _step_from_hit(hit, case_step=case_step, sub_idx=int(hit.get("_idx") or 0))
        checkpoints.append(ps)
        key_refs.append(str(ps.get("key_ref") or ""))
    for idx, clause in catalog_left:
        ent, ref = _match_catalog_clause(clause, "expected")
        if ent is None or (ref == "expected.ui_text" and "不展示" in clause):
            sug = _slug(clause, prefix="expected")
            warnings.append(
                {
                    "case_step": case_step,
                    "layer": "expected",
                    "line": clause,
                    "raw_text": clause,
                    "unmapped": True,
                    "suggested_key_refs": [
                        {
                            "key_ref": sug,
                            "write_examples": [clause[:120]],
                            "note": "写不到具体检查点，整案无法 UI 自动化",
                        }
                    ],
                    "blocked": _blocked_item(
                        phase="check",
                        case_step=case_step,
                        clause=clause,
                        reason="unmapped",
                        user_message=user_message(
                            case_step=case_step,
                            layer="expected",
                            clause=clause,
                            reason="unmapped",
                        ),
                    ),
                }
            )
            continue
        ref = ref or _key_ref_of(ent)
        key_refs.append(ref)
        ps = _plan_step_from_match(
            clause=clause,
            match=ent,
            key_ref=ref,
            case_step=case_step,
            sub_idx=idx,
            layer="expected",
        )
        if ps:
            checkpoints.append(ps)
    return checkpoints, warnings, key_refs


def build_do_program_plan_for_step(
    case_step: int,
    instruction: str,
    *,
    platform: str = "",
) -> dict[str, Any]:
    steps, warnings, key_refs = compile_operation_text(
        instruction, case_step=case_step, platform=platform
    )
    return {
        "schema": "mino.do_program_plan.v1",
        "case_step": int(case_step),
        "steps": steps,
        "key_refs": key_refs,
        "fallback_lines": [w for w in warnings if w.get("fallback") or w.get("unmapped") or w.get("blocked")],
        "compile_warnings": warnings,
    }


def build_check_program_plan_for_step(
    case_step: int,
    expected: str,
    *,
    instruction: str = "",
    platform: str = "",
    do_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    checkpoints, warnings, key_refs = compile_expected_text(
        expected,
        case_step=case_step,
        platform=platform,
        do_steps=do_steps,
    )
    return {
        "schema": "mino.check_program_plan.v1",
        "case_step": int(case_step),
        "instruction_hint": str(instruction or "")[:200],
        "checkpoints": checkpoints,
        "key_refs": key_refs,
        "fallback_lines": [w for w in warnings if w.get("fallback") or w.get("unmapped") or w.get("blocked")],
        "compile_warnings": warnings,
    }


def compile_case_step_program_keys(case: dict[str, Any]) -> dict[str, Any]:
    """整案编译 → step_program_keys + 各步 plan 缓存 + warnings + ui_automation。"""
    step_keys: dict[str, Any] = {}
    all_warnings: list[dict[str, Any]] = []
    platform = str(case.get("platform") or "")
    for node in build_seq_nodes(case):
        n = int(node.n)
        do_plan = build_do_program_plan_for_step(n, str(node.instruction or ""), platform=platform)
        ck_plan = build_check_program_plan_for_step(
            n,
            str(node.expected or ""),
            instruction=str(node.instruction or ""),
            platform=platform,
            do_steps=list(do_plan.get("steps") or []),
        )
        step_keys[str(n)] = {
            "operations": list(do_plan.get("key_refs") or []),
            "expected": list(ck_plan.get("key_refs") or []),
            "do_program_plan": do_plan,
            "check_program_plan": ck_plan,
        }
        all_warnings.extend(do_plan.get("compile_warnings") or [])
        all_warnings.extend(ck_plan.get("compile_warnings") or [])
    blocked: list[dict[str, Any]] = []
    for w in all_warnings:
        item = w.get("blocked") if isinstance(w, dict) else None
        if isinstance(item, dict):
            blocked.append(item)
    return {
        "step_program_keys": step_keys,
        "key_compile_warnings": all_warnings,
        "ui_automation": {
            "schema": "mino.ui_automation.v1",
            "coverable": not blocked,
            "blocked_by": blocked,
        },
    }


def sync_case_step_program_keys(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    compiled = compile_case_step_program_keys(out)
    meta = out.get("meta") if isinstance(out.get("meta"), dict) else {}
    if not meta and isinstance(out.get("resource_key"), dict):
        meta = {}
    meta = dict(meta)
    meta["step_program_keys"] = compiled["step_program_keys"]
    meta["key_compile_warnings"] = compiled["key_compile_warnings"]
    meta["ui_automation"] = compiled["ui_automation"]
    out["meta"] = meta
    return out


def step_bundle(case: dict[str, Any] | None, case_step: int) -> dict[str, Any]:
    if not isinstance(case, dict) or case_step <= 0:
        return {}
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    return dict(spk.get(str(case_step)) or spk.get(str(int(case_step))) or {})
