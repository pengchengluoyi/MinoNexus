"""用例步骤：操作 / 预期编号行 → step_program_keys + do/check_program_plan（保存时编译）。"""
from __future__ import annotations

import re
import copy
import unicodedata
from typing import Any

from mino_nexus.ai.case_text import parse_numbered_items_rules
from mino_nexus.contract.step_pointer import build_seq_nodes

_BLOCK_RE = re.compile(r"【块[:：]\s*([^】]+)】")
_NAV_RE = re.compile(r"【导航[:：]\s*([^】]+)】")
_CAP_RE = re.compile(r"【cap[:：]\s*([^】]+)】", re.I)
_KEY_REF_RE = re.compile(r"\b((?:operation|expected)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*)\b", re.I)
from mino_nexus.contract.build_identity import COMPILE_CONTRACT_REVISION as _COMPILE_CONTRACT_REVISION  # noqa: F401,E402
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
    from mino_nexus.resources.case_resource_key_catalog import catalog_payload

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
        from mino_nexus.casework.case_key_registry import default_block_key_ref
        from mino_nexus.navigation.atlas.nav_flow_block_catalog import load_block_row, normalize_block_id

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
        from mino_nexus.casework.case_key_registry import default_screen_key_ref

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
    return [b for b in bodies if len(b) >= 2] or ([_strip_op_prefix(raw)] if raw else [])


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
    from mino_nexus.catalog.skill_channel import capability_enabled

    if not capability_enabled(cid):
        return "unavailable_cap"
    # ui_coverable=false 只进覆盖统计，不再把事件判成不可编译、不可下发（规划 §4/§6）。
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
    detail: str = "",
    severity: str = "",
) -> dict[str, Any]:
    from mino_nexus.casework.case_step_intent import blocked_severity

    row = {
        "phase": phase,
        "case_step": int(case_step),
        "source_clause": str(clause or "")[:240],
        "reason": reason,
        "event_id": str(event_id or ""),
        "severity": blocked_severity(severity or reason, key_ref=str(event_id or "")),
    }
    if event_name:
        row["event_name"] = event_name
    if user_message:
        row["user_message"] = user_message[:400]
    if detail:
        row["detail"] = str(detail)[:240]
    return row


_CAPABILITY_BLOCK_REASONS = frozenset(
    {"unavailable_cap", "uncoverable_event", "platform_unsupported", "capability_disabled", "missing_ref"}
)


def _block_class(item: dict[str, Any]) -> str:
    phase = str(item.get("phase") or "")
    reason = str(item.get("reason") or "")
    if phase == "prep":
        return "setup"
    if phase == "check":
        return "oracle"
    if reason in _CAPABILITY_BLOCK_REASONS:
        return "operation"
    return "source"


def _enrich_blocker(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    row["class"] = str(row.get("class") or _block_class(row))
    reason = str(row.get("reason") or "")
    if row["class"] == "setup":
        row["severity"] = "degraded"
    elif reason in ("human_only", "advisory"):
        row["severity"] = reason
    elif reason in _CAPABILITY_BLOCK_REASONS:
        # 能力缺口是运行时归因 A/B 的输入，不是编译期死刑（规划 §4/§6）。
        row["severity"] = "risk"
    else:
        row["severity"] = str(row.get("severity") or "blocking")
    from mino_nexus.contract.attribution import classify_reason

    attribution = classify_reason(reason, layer="compile")
    row.setdefault("reason_code", reason)
    row.setdefault("layer", "compile")
    row.setdefault("attribution_class", attribution["attribution_class"])
    row.setdefault("attribution_label", attribution["attribution_label"])
    return row


def _has_manual_check(step_keys: dict[str, Any]) -> bool:
    for bundle in step_keys.values():
        if not isinstance(bundle, dict):
            continue
        plan = bundle.get("check_program_plan") if isinstance(bundle.get("check_program_plan"), dict) else {}
        for step in plan.get("checkpoints") or []:
            if not isinstance(step, dict):
                continue
            assert_payload = step.get("assert") if isinstance(step.get("assert"), dict) else {}
            if (str(step.get("key_ref") or "") == "expected.human_review"
                    or str(assert_payload.get("mode") or "") == "human_only"):
                return True
    return False


def _setup_state(case: dict[str, Any]) -> str:
    from mino_nexus.casework.case_semantic_integrity import coverage_matches, precondition_lines

    precondition = str(case.get("precondition") or case.get("precondition_raw") or "")
    if not precondition_lines(precondition):
        return "ready"
    claim = case.get("resource_key") or (case.get("meta") or {}).get("resource_key") or {}
    rows = claim.get("precondition_coverage") or []
    if not rows:
        return "unknown"
    if any(str(row.get("status") or "mapped") == "source" for row in rows if isinstance(row, dict)):
        return "blocked"
    if any(str(row.get("status") or "mapped") != "mapped" for row in rows if isinstance(row, dict)):
        return "needs_setup"
    if any(not coverage_matches(row, claim) for row in rows if isinstance(row, dict)):
        return "needs_setup"
    return "ready"


def _build_ui_automation(case: dict[str, Any], step_keys: dict[str, Any],
                         blocked: list[dict[str, Any]]) -> dict[str, Any]:
    do_steps = 0
    check_steps = 0
    for bundle in step_keys.values():
        if not isinstance(bundle, dict):
            continue
        do_steps += len((bundle.get("do_program_plan") or {}).get("steps") or [])
        check_steps += len((bundle.get("check_program_plan") or {}).get("checkpoints") or [])
    blocking = [item for item in blocked if str(item.get("severity") or "blocking") == "blocking"]
    risks = [item for item in blocked if str(item.get("severity") or "") == "risk"]
    ui_blocking = [item for item in blocking if str(item.get("class") or "") in {"operation", "source"}]
    oracle_blocking = [item for item in blocking if str(item.get("class") or "") == "oracle"]
    setup_items = [item for item in blocked if str(item.get("class") or "") == "setup"]
    setup_state = _setup_state(case)
    manual_oracle = _has_manual_check(step_keys)
    if not do_steps and not check_steps:
        ui_plan = "none"
    elif ui_blocking or risks:
        ui_plan = "partial"
    else:
        ui_plan = "complete"
    if oracle_blocking:
        oracle_state = "missing"
    elif manual_oracle:
        oracle_state = "manual"
    else:
        oracle_state = "automated"
    source_in_prep = any(
        str(item.get("class") or "") == "source" and str(item.get("phase") or "") == "prep"
        for item in blocking
    )
    if source_in_prep or setup_state == "blocked":
        status = "blocked"
    elif ui_plan == "none":
        # 完全没有步骤，且没有任何可归因的能力缺口：这是定义/编译问题。
        status = "blocked" if not risks else "runnable"
    elif setup_state != "ready":
        status = "needs_setup"
    else:
        # 部分步骤或断言无法自动化只记在 ui_plan / blocked_by，不把整条用例标成不可执行。
        status = "runnable"
    coverable = status in {"runnable", "needs_setup"}
    total = do_steps + check_steps
    from mino_nexus.contract.attribution import merge_classes

    return {
        "schema": "mino.ui_automation.v2",
        "status": status,
        "coverable": coverable,
        "runnable_now": status == "runnable",
        "ui_plan": ui_plan,
        "setup": {
            "state": setup_state,
            "verified": setup_state == "ready",
            "items": setup_items,
        },
        "oracle": {
            "state": oracle_state,
            "manual": manual_oracle,
            "missing_items": oracle_blocking,
        },
        "coverage": {
            "total_steps": total,
            "covered_steps": max(0, total - len(ui_blocking) - len(oracle_blocking) - len(risks)),
            "manual": 1 if manual_oracle else 0,
            "risk_steps": len(risks),
            "risk_items": risks,
            "attribution_classes": merge_classes(blocked),
        },
        "blocked_by": blocked,
    }


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
        from mino_nexus.knowledge.checkpoint_text_policy import compiled_text_rules
        rules = compiled_text_rules(str(hit.get("assert_mode") or "vlm"), rules, clause)
        target = hit.get("target") if isinstance(hit.get("target"), dict) else {}
        if target:
            element = {**element, **{key: value for key, value in target.items() if value}}
        assert_payload: dict[str, Any] = {
            "mode": str(hit.get("assert_mode") or "vlm"),
            "element": element,
            "rules": rules,
        }
        if hit.get("bind"):
            assert_payload["bind"] = hit["bind"]
        if clause:
            # 结构参数只能提炼信息，完整原句始终保留逻辑关系与视觉约束。
            assert_payload["expectation"] = clause
        key_ref = str(hit.get("key_ref") or "")
        if key_ref == "expected.human_review":
            assert_payload = {
                "mode": "human_only",
                "rules": rules,
                "expectation": clause,
            }
        elif key_ref == "expected.ui_absent":
            assert_payload["mode"] = "ui_absent"
        row = {
            "id": f"ck_s{case_step}_{sub_idx}"[:64],
            "key_ref": key_ref,
            "title": str(hit.get("event_name") or key_ref or clause)[:240],
            "kind": "checkpoint",
            "assert": assert_payload,
            "schemes": schemes,
            "optional": key_ref == "expected.human_review",
            "run_policy": "skip" if key_ref == "expected.human_review" else "",
            "source_line": clause[:240],
            "source_clause": clause[:240],
            "source_clauses": list(hit.get("source_clauses") or [clause]),
            "subclause_index": sub_idx,
            "observe": "human_only" if key_ref == "expected.human_review" else "exec",
            "ui_status": "skipped" if key_ref == "expected.human_review" else "pending",
            "temporal_scope": "instant_only",
        }
        if hit.get("intent_role"):
            row["intent_role"] = str(hit.get("intent_role"))
        if "start" in hit and "end" in hit:
            row["start"] = int(hit["start"])
            row["end"] = int(hit["end"])
        return row
    params: dict[str, Any] = {}
    if element:
        params["element"] = element
    if rules:
        params["rules"] = rules
    target = hit.get("target") if isinstance(hit.get("target"), dict) else {}
    if target:
        params["target"] = {k: v for k, v in target.items() if v}
    if str(hit.get("hook_cap") or "") == "swipe_direction":
        params["direction"] = str(rules.get("direction") or "")
        if "duration_ms" in rules:
            params["duration_ms"] = rules["duration_ms"]
    if rules.get("wait_ms"):
        try:
            params["wait_ms"] = int(rules["wait_ms"])
        except (TypeError, ValueError):
            pass
    hint = _spatial_hint(clause)
    if hint:
        params["spatial_hint"] = hint
    key_ref = str(hit.get("key_ref") or "")
    if key_ref == "operation.author_remark":
        return {
            "id": f"do_s{case_step}_{sub_idx}_remark"[:64],
            "key_ref": key_ref,
            "title": clause[:240],
            "kind": "remark",
            "optional": False,
            "run_policy": "skip",
            "source_line": clause[:240],
            "source_clause": clause[:240],
            "subclause_index": sub_idx,
            "observe": "author_only",
            "ui_status": "skipped",
            "intent_role": str(hit.get("intent_role") or "read_only"),
        }
    kind = str(hit.get("kind") or "hook")
    if key_ref == "operation.nav_target":
        kind = "nav"
    if key_ref == "operation.system_app":
        kind = "hook"
    if kind not in ("hook", "flow_block", "checkpoint", "nav", "remark"):
        kind = "hook"
    hook_cap = "" if kind == "flow_block" else str(hit.get("hook_cap") or "tap_element")
    if key_ref == "operation.system_app":
        rules = hit.get("rules") if isinstance(hit.get("rules"), dict) else {}
        hook_cap = str(rules.get("hook_cap") or hit.get("hook_cap") or "kill_app")
    row = {
        "id": f"do_s{case_step}_{sub_idx}_{kind}"[:64],
        "key_ref": key_ref,
        "title": clause[:240],
        "kind": kind,
        "hook_cap": hook_cap,
        "schemes": schemes,
        "optional": False,
        "source_line": clause[:240],
        "source_clause": clause[:240],
        "subclause_index": sub_idx,
        "observe": "exec",
        "ui_status": "pending",
    }
    if "start" in hit and "end" in hit:
        row["start"] = int(hit["start"])
        row["end"] = int(hit["end"])
    if hit.get("slots"):
        row["slots"] = list(hit["slots"])
    if hit.get("block_id"):
        row["block_id"] = str(hit["block_id"])
    if params:
        row["params"] = params
    if kind == "nav":
        rules = hit.get("rules") if isinstance(hit.get("rules"), dict) else {}
        target = hit.get("target") if isinstance(hit.get("target"), dict) else {}
        nav = str(hit.get("nav_target") or rules.get("nav_target") or target.get("name") or "").strip()
        if nav:
            row["nav_target"] = nav
    if hit.get("intent_role"):
        row["intent_role"] = str(hit.get("intent_role"))
    if kind not in ("flow_block", "remark") and kind != "nav" and key_ref != "operation.observe":
        reason = _cap_block_reason(str(row.get("hook_cap") or ""))
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
    from mino_nexus.casework.case_clause_events import platform_block, user_message

    from mino_nexus.casework.case_step_intent import reconcile_hit_layer

    hit = reconcile_hit_layer(hit, author_layer=author_layer)
    clause = str(hit.get("clause") or "")
    phase = "do" if author_layer == "operation" else "check"
    if str(hit.get("key_ref") or "") == "operation.observe":
        return None
    if str(hit.get("key_ref") or "") == "operation.author_remark":
        from mino_nexus.casework.case_semantic_integrity import explicit_remark
        if explicit_remark(clause):
            return None
        return {"case_step": case_step, "layer": author_layer, "line": clause,
                "blocked": _blocked_item(phase=phase, case_step=case_step, clause=clause,
                    reason="required_operation_skipped", user_message="必需操作或观察不能被编译为可跳过说明。")}
    if str(hit.get("key_ref") or "") == "expected.human_review":
        return None
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
    from mino_nexus.casework.case_clause_events import user_message
    from mino_nexus.casework.case_step_span_llm import compile_step_spans

    warnings: list[dict[str, Any]] = []
    key_refs: list[str] = []
    plan_steps: list[dict[str, Any]] = []
    units: list[tuple[str, dict[str, Any] | None]] = []
    for body in split_clauses(instruction):
        hits, fail = compile_step_spans(body, "operation", platform=platform)
        if fail:
            units.append((body, {"_block": str(fail.get("reason") or "unmapped"), "detail": str(fail.get("detail") or "")}))
            continue
        if not hits:
            units.append((body, None))
            continue
        for hit in hits:
            if hit.get("missing_slots"):
                units.append((str(hit.get("clause") or body), {"_block": "missing_slot", "detail": "、".join(hit["missing_slots"]), "hit": hit}))
            else:
                units.append((str(hit.get("clause") or body), hit))
    for idx, (clause, hit) in enumerate(units):
        if isinstance(hit, dict) and hit.get("_block"):
            reason = str(hit.get("_block") or "")
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
                        detail=str(hit.get("detail") or ""),
                        user_message=user_message(
                            case_step=case_step,
                            layer="operation",
                            clause=clause,
                            reason=reason,
                            detail=str(hit.get("detail") or ""),
                        ),
                    ),
                }
            )
            continue
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
                    detail=str(ps.get("hook_cap") or ""),
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
        from mino_nexus.platform.settings_store import compile_regex_fallback_enabled

        if not compile_regex_fallback_enabled():
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
                            "note": "编译未识别到事件（正则兜底已关闭）",
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
                            detail="未识别到可执行事件，请配置导入编译 AI 或开启正则兜底",
                        ),
                    ),
                }
            )
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
    from mino_nexus.casework.case_clause_events import (
        incomplete_reason,
        merge_dialog_checks,
        user_message,
    )
    from mino_nexus.casework.case_step_span_llm import compile_step_spans

    warnings: list[dict[str, Any]] = []
    key_refs: list[str] = []
    checkpoints: list[dict[str, Any]] = []
    clauses = split_clauses(expected)
    if not clauses and str(expected or "").strip():
        clauses = [str(expected).strip()]
    pending: list[dict[str, Any]] = []
    catalog_left: list[tuple[int, str]] = []
    expanded: list[tuple[int, str, dict[str, Any] | None]] = []
    cursor = 0
    for body in clauses:
        hits, fail = compile_step_spans(body, "expected", platform=platform)
        if fail or not hits:
            reason = str((fail or {}).get("reason") or ("uncovered" if fail else "unmapped"))
            uncovered = str((fail or {}).get("detail") or "")
            warnings.append(
                {
                    "case_step": case_step,
                    "layer": "expected",
                    "line": body,
                    "raw_text": body,
                    "blocked": _blocked_item(
                        phase="check",
                        case_step=case_step,
                        clause=body,
                        reason=reason,
                        detail=uncovered,
                        user_message=user_message(
                            case_step=case_step,
                            layer="expected",
                            clause=body,
                            reason=reason,
                            detail=uncovered,
                        ),
                    ),
                }
            )
            continue
        for hit in hits:
            expanded.append((cursor, str(hit.get("clause") or body), hit))
            cursor += 1
    for idx, clause, hit in expanded:
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
    from mino_nexus.platform.settings_store import compile_regex_fallback_enabled

    for idx, clause in catalog_left:
        if not compile_regex_fallback_enabled():
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
                            "note": "编译未识别到检查点（正则兜底已关闭）",
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
                            detail="未识别到可验收检查点，请配置导入编译 AI 或开启正则兜底",
                        ),
                    ),
                }
            )
            continue
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


def _spans_from_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict) or "start" not in step:
            continue
        out.append(
            {
                "start": int(step.get("start") or 0),
                "end": int(step.get("end") or 0),
                "key_ref": str(step.get("key_ref") or ""),
                "block_id": str(step.get("block_id") or ""),
                "slots": list(step.get("slots") or []),
                "ui_status": str(step.get("ui_status") or "pending"),
            }
        )
    return out


def build_do_program_plan_for_step(
    case_step: int,
    instruction: str,
    *,
    platform: str = "",
) -> dict[str, Any]:
    steps, warnings, key_refs = compile_operation_text(
        instruction, case_step=case_step, platform=platform
    )
    from mino_nexus.resources.case_resource_key_catalog import CATALOG_VERSION

    return {
        "schema": "mino.do_program_plan.v1",
        "compiler": "case-step-compile",
        "catalog_version": CATALOG_VERSION,
        "compile_contract_revision": _COMPILE_CONTRACT_REVISION,
        "platform": str(platform or ""),
        "source": str(instruction or ""),
        "case_step": int(case_step),
        "steps": steps,
        "spans": _spans_from_steps(steps),
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
    from mino_nexus.resources.case_resource_key_catalog import CATALOG_VERSION

    return {
        "schema": "mino.check_program_plan.v1",
        "compiler": "case-step-compile",
        "catalog_version": CATALOG_VERSION,
        "compile_contract_revision": _COMPILE_CONTRACT_REVISION,
        "platform": str(platform or ""),
        "source": str(expected or ""),
        "case_step": int(case_step),
        "instruction_hint": str(instruction or "")[:200],
        "instruction_source": str(instruction or ""),
        "checkpoints": checkpoints,
        "spans": _spans_from_steps(checkpoints),
        "key_refs": key_refs,
        "fallback_lines": [w for w in warnings if w.get("fallback") or w.get("unmapped") or w.get("blocked")],
        "compile_warnings": warnings,
    }


from mino_nexus.navigation.atlas.nav_block_digest import block_dependencies as _block_dependencies  # noqa: E402
def _stored_plan_fresh(plan: Any, source: str, *, platform: str = "", instruction: str | None = None,
                       app_id: str = "") -> bool:
    from mino_nexus.resources.case_resource_key_catalog import CATALOG_VERSION

    if not isinstance(plan, dict):
        return False
    from mino_nexus.casework.case_semantic_integrity import operation_diagnostics
    if operation_diagnostics({"meta": {"step_program_keys": {"current": {"do_program_plan": plan}}}}):
        return False
    if str(plan.get("compiler") or "") != "case-step-compile":
        return False
    if int(plan.get("catalog_version") or 0) != int(CATALOG_VERSION):
        return False
    if int(plan.get("compile_contract_revision") or 0) != _COMPILE_CONTRACT_REVISION:
        return False
    if str(plan.get("platform") or "") != str(platform or ""):
        return False
    if plan.get("block_dependencies") != _block_dependencies(plan, app_id):
        return False
    if instruction is not None and str(plan.get("instruction_source") or "") != str(instruction or ""):
        return False
    if str(plan.get("source") or "") != str(source or ""):
        return False
    warnings = plan.get("compile_warnings") if isinstance(plan.get("compile_warnings"), list) else []
    # 能力缺口（risk）/ 前置提示（degraded）不再让计划永久失效；只有真正的编译阻断
    # 才要求重编，避免「每次读都重编一次」的空转（规划 §4）。
    def _hard(row: Any) -> bool:
        if not isinstance(row, dict) or not row.get("blocked"):
            return False
        blocked = row.get("blocked") if isinstance(row.get("blocked"), dict) else {}
        reason = str(blocked.get("reason") or blocked.get("reason_code") or "")
        if reason in _CAPABILITY_BLOCK_REASONS:
            return False
        return str(blocked.get("severity") or "blocking") == "blocking"

    if any(_hard(row) for row in warnings):
        return False
    return True


from mino_nexus.casework.compile_provenance import capture as capture_provenance


@capture_provenance
def compile_case_step_program_keys(case: dict[str, Any]) -> dict[str, Any]:
    """整案编译 → step_program_keys + 各步 plan 缓存 + warnings + ui_automation。"""
    project_id = str(case.get("project_id") or "").strip()
    if not project_id or project_id == "_project":
        raise ValueError("control_project_missing: 请从项目内导入或编译用例")
    from mino_nexus.resources.project_store import require_project

    require_project(project_id)  # 应用 ID 不是控件项目命名空间。
    case = {**case, "project_id": project_id}
    step_keys: dict[str, Any] = {}
    all_warnings: list[dict[str, Any]] = []
    platform = str(case.get("platform") or "")
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    previous = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    for node in build_seq_nodes(case):
        n = int(node.n)
        instr = str(node.instruction or "")
        expected = str(node.expected or "")
        old = previous.get(str(n)) if isinstance(previous.get(str(n)), dict) else {}
        do_old = old.get("do_program_plan") if isinstance(old, dict) else None
        ck_old = old.get("check_program_plan") if isinstance(old, dict) else None
        app_id = str(case.get("app_id") or case.get("project_id") or "")
        if _stored_plan_fresh(do_old, instr, platform=platform, app_id=app_id) and _stored_plan_fresh(
            ck_old, expected, platform=platform, instruction=instr, app_id=app_id
        ):
            do_plan = copy.deepcopy(do_old or {})
            ck_plan = copy.deepcopy(ck_old or {})
        else:
            do_plan = build_do_program_plan_for_step(n, instr, platform=platform)
            ck_plan = build_check_program_plan_for_step(
                n,
                expected,
                instruction=instr,
                platform=platform,
                do_steps=list(do_plan.get("steps") or []),
            )
        from mino_nexus.casework.execution_requirements import compile_steps, case_source_revision
        do_plan["binding_source_revision"] = ck_plan["binding_source_revision"] = case_source_revision(case)
        do_plan["binding_event_contract"] = ck_plan["binding_event_contract"] = 3
        do_plan["binding_requirement_contract"] = ck_plan["binding_requirement_contract"] = 1
        do_plan["binding_steps"] = compile_steps(do_plan, app_id=app_id)
        ck_plan["binding_steps"] = compile_steps(ck_plan, app_id=app_id)
        do_plan["block_dependencies"] = _block_dependencies(do_plan, app_id)
        ck_plan["block_dependencies"] = _block_dependencies(ck_plan, app_id)
        required_caps = {
            str(step.get("hook_cap") or step.get("device_cap") or "")
            for step in do_plan.get("steps") or [] if isinstance(step, dict)
        }
        from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps

        app_id = str(case.get("app_id") or case.get("project_id") or "")
        seen_blocks: set[str] = set()

        def collect(block_id: str) -> None:
            if not block_id or block_id in seen_blocks:
                return
            seen_blocks.add(block_id)
            for block_step in resolve_effective_steps(app_id=app_id, block_id=block_id):
                required_caps.add(str(block_step.get("hook_cap") or block_step.get("device_cap") or ""))
                collect(str(block_step.get("block_id") or ""))

        for step in do_plan.get("steps") or []:
            if isinstance(step, dict):
                collect(str(step.get("block_id") or ""))
        # 意图图标注：步骤 → 意图节点（auth.login / order.place / app 私有块）。
        # 只补字段，不改事件序列（规划 §3）。
        from mino_nexus.knowledge.intent_graph import annotate_step, build_case_graph, validate

        do_plan["steps"] = [annotate_step(step, app_id=app_id) for step in (do_plan.get("steps") or [])]
        ck_plan["checkpoints"] = [annotate_step(item, app_id=app_id) for item in (ck_plan.get("checkpoints") or [])]
        graph = build_case_graph(
            list(do_plan.get("steps") or []) + list(ck_plan.get("checkpoints") or []),
            app_id=app_id, case_id=str(case.get("case_id") or ""), case_step=n,
        )
        for issue in validate(graph, app_id=app_id):
            all_warnings.append({"case_step": n, "layer": "intent", "blocked": issue})
        step_keys[str(n)] = {
            "operations": list(do_plan.get("key_refs") or []),
            "expected": list(ck_plan.get("key_refs") or []),
            "do_program_plan": do_plan,
            "check_program_plan": ck_plan,
            "required_capabilities": sorted(required_caps - {""}),
            "intent_graph": graph,
            "intent_ids": list(graph.get("intent_ids") or []),
        }
        all_warnings.extend(do_plan.get("compile_warnings") or [])
        all_warnings.extend(ck_plan.get("compile_warnings") or [])
        from mino_nexus.knowledge.control_registry import stamp_compiled_plans

        for item in stamp_compiled_plans(case, do_plan, ck_plan):
            all_warnings.append({"case_step": item.get("case_step"), "layer": "operation", "blocked": item})
    from mino_nexus.casework.case_semantic_integrity import precondition_diagnostics
    for issue in precondition_diagnostics(case):
        all_warnings.append({
            "case_step": 0,
            "layer": "precondition",
            "line": issue.get("source_clause") or "",
            "blocked": {
                "phase": "prep",
                "case_step": 0,
                "source_clause": str(issue.get("source_clause") or ""),
                "reason": str(issue.get("reason") or "manual_prerequisite_required"),
                "class": str(issue.get("class") or "setup"),
                "severity": str(issue.get("severity") or "degraded"),
                "user_message": str(issue.get("user_message") or ""),
                "detail": str(issue.get("detail") or ""),
                **({"setup": issue["setup"]} if isinstance(issue.get("setup"), dict) else {}),
            },
        })
    blocked: list[dict[str, Any]] = []
    for w in all_warnings:
        item = w.get("blocked") if isinstance(w, dict) else None
        if isinstance(item, dict):
            item = _enrich_blocker(item)
            w["blocked"] = item
            blocked.append(item)
    intent_ids: list[str] = []
    domain_counts: dict[str, int] = {}
    for bundle in step_keys.values():
        for iid in bundle.get("intent_ids") or []:
            if iid not in intent_ids:
                intent_ids.append(str(iid))
        for node in (bundle.get("intent_graph") or {}).get("nodes") or []:
            dom = str(node.get("domain") or "")
            domain_counts[dom] = domain_counts.get(dom, 0) + 1
    return {
        "step_program_keys": step_keys,
        "key_compile_warnings": all_warnings,
        "ui_automation": _build_ui_automation(case, step_keys, blocked),
        "intent_summary": {
            "schema": "mino.intent_graph.v1",
            "case_id": str(case.get("case_id") or ""),
            "intents": intent_ids,
            "domains": domain_counts,
        },
    }


def sync_case_step_program_keys(row: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.casework.v5_publication import guard_case_row
    out = guard_case_row(row)
    compiled = compile_case_step_program_keys(out)
    meta = out.get("meta") if isinstance(out.get("meta"), dict) else {}
    if not meta and isinstance(out.get("resource_key"), dict):
        meta = {}
    meta = dict(meta)
    meta["step_program_keys"] = compiled["step_program_keys"]
    meta["key_compile_warnings"] = compiled["key_compile_warnings"]
    meta["ui_automation"] = compiled["ui_automation"]
    meta["intent_summary"] = compiled.get("intent_summary") or {}
    meta["compile_provenance"] = compiled.get("compile_provenance", [])
    out["meta"] = meta
    return out


def step_bundle(case: dict[str, Any] | None, case_step: int) -> dict[str, Any]:
    if not isinstance(case, dict) or case_step <= 0:
        return {}
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    return dict(spk.get(str(case_step)) or spk.get(str(int(case_step))) or {})
