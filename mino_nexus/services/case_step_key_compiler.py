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
_COMPOUND_SPLIT = re.compile(r"[；;。]|(?:\s+然后\s+)|(?:\s+并\s+)")
_LOGIN_FLOW_RE = re.compile(
    r"(完成登录|登录成功|验证码登录|邮箱验证码|手机.*验证码|输入.*验证码.*登录)",
    re.I,
)
_SLUG_BAD = re.compile(r"[^\w]+", re.UNICODE)


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
    if layer == "operation" and _LOGIN_FLOW_RE.search(text):
        from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID
        from mino_nexus.services.case_key_registry import default_block_key_ref
        from mino_nexus.services.nav_flow_block_catalog import load_block_row

        canon = "fb.global.login"
        row = load_block_row(app_id=GLOBAL_APP_ID, block_id=canon)
        if row:
            ref = str(row.get("key_ref") or "").strip() or default_block_key_ref(canon)
            return {"key_ref": ref, "kind": "flow_block", "block_id": canon}, ref
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
    if match.get("kind") == "flow_block":
        bid = str(match.get("block_id") or "").strip()
        return {
            "id": f"do_s{case_step}_{sub_idx}_{bid}"[:64],
            "key_ref": key_ref,
            "title": clause[:240],
            "kind": "flow_block",
            "block_id": bid,
            "optional": False,
            "source_line": clause,
            "subclause_index": sub_idx,
        }
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
        }
    if match.get("kind") == "hook" or str(match.get("hook_cap") or ""):
        cap = str(match.get("hook_cap") or match.get("config_keys", [""])[0] or "tap_element")
        return {
            "id": f"do_s{case_step}_{sub_idx}_hook"[:64],
            "key_ref": key_ref,
            "title": clause[:240],
            "kind": "hook",
            "hook_cap": cap,
            "optional": False,
            "source_line": clause,
            "subclause_index": sub_idx,
        }
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
        "subclause_index": sub_idx,
    }
    if kind == "hook":
        step["hook_cap"] = hook_cap or "tap_element"
    return step


def compile_operation_text(
    instruction: str,
    *,
    case_step: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """返回 (plan_steps, warnings, key_refs)。"""
    warnings: list[dict[str, Any]] = []
    key_refs: list[str] = []
    plan_steps: list[dict[str, Any]] = []
    clauses = split_clauses(instruction)
    for idx, clause in enumerate(clauses):
        ent, ref = _match_catalog_clause(clause, "operation")
        if ent is None:
            sug = _slug(clause)
            warnings.append(
                {
                    "case_step": case_step,
                    "layer": "operation",
                    "line": clause,
                    "raw_text": clause,
                    "fallback": True,
                    "suggested_key_refs": [
                        {
                            "key_ref": sug,
                            "write_examples": [clause[:120]],
                            "note": "建议加入 catalog operation 条目后回写用例",
                        }
                    ],
                }
            )
            plan_steps.append(
                {
                    "id": f"do_s{case_step}_{idx}_see"[:64],
                    "key_ref": sug,
                    "title": clause[:240],
                    "kind": "visual_action",
                    "exec_caps": ["tap_element", "input_text", "swipe_direction", "press_key"],
                    "optional": False,
                    "source": "vision_fallback",
                    "source_line": clause,
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
                    plan_steps.append(ps)
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
            plan_steps.append(ps)
    return plan_steps, warnings, key_refs


def compile_expected_text(
    expected: str,
    *,
    case_step: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    warnings: list[dict[str, Any]] = []
    key_refs: list[str] = []
    checkpoints: list[dict[str, Any]] = []
    clauses = split_clauses(expected)
    if not clauses and str(expected or "").strip():
        clauses = [str(expected).strip()]
    for idx, clause in enumerate(clauses):
        ent, ref = _match_catalog_clause(clause, "expected")
        if ent is None:
            sug = _slug(clause, prefix="expected")
            warnings.append(
                {
                    "case_step": case_step,
                    "layer": "expected",
                    "line": clause,
                    "raw_text": clause,
                    "fallback": True,
                    "suggested_key_refs": [
                        {
                            "key_ref": sug,
                            "write_examples": [clause[:120]],
                            "note": "建议加入 catalog expected 条目",
                        }
                    ],
                }
            )
            ck = {
                "id": f"ck_s{case_step}_{idx}_fb"[:64],
                "key_ref": sug,
                "title": clause[:240],
                "kind": "checkpoint",
                "assert": {"mode": "vlm", "expectation": clause[:500]},
                "optional": False,
                "source": "llm_fallback",
                "source_line": clause,
            }
            checkpoints.append(ck)
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


def build_do_program_plan_for_step(case_step: int, instruction: str) -> dict[str, Any]:
    steps, warnings, key_refs = compile_operation_text(instruction, case_step=case_step)
    return {
        "schema": "mino.do_program_plan.v1",
        "case_step": int(case_step),
        "steps": steps,
        "key_refs": key_refs,
        "fallback_lines": [w for w in warnings if w.get("fallback")],
        "compile_warnings": warnings,
    }


def build_check_program_plan_for_step(case_step: int, expected: str, *, instruction: str = "") -> dict[str, Any]:
    checkpoints, warnings, key_refs = compile_expected_text(expected, case_step=case_step)
    return {
        "schema": "mino.check_program_plan.v1",
        "case_step": int(case_step),
        "instruction_hint": str(instruction or "")[:200],
        "checkpoints": checkpoints,
        "key_refs": key_refs,
        "fallback_lines": [w for w in warnings if w.get("fallback")],
        "compile_warnings": warnings,
    }


def compile_case_step_program_keys(case: dict[str, Any]) -> dict[str, Any]:
    """整案编译 → step_program_keys + 各步 plan 缓存 + warnings。"""
    step_keys: dict[str, Any] = {}
    all_warnings: list[dict[str, Any]] = []
    for node in build_seq_nodes(case):
        n = int(node.n)
        do_plan = build_do_program_plan_for_step(n, str(node.instruction or ""))
        ck_plan = build_check_program_plan_for_step(
            n, str(node.expected or ""), instruction=str(node.instruction or "")
        )
        step_keys[str(n)] = {
            "operations": list(do_plan.get("key_refs") or []),
            "expected": list(ck_plan.get("key_refs") or []),
            "do_program_plan": do_plan,
            "check_program_plan": ck_plan,
        }
        all_warnings.extend(do_plan.get("compile_warnings") or [])
        all_warnings.extend(ck_plan.get("compile_warnings") or [])
    return {
        "step_program_keys": step_keys,
        "key_compile_warnings": all_warnings,
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
    out["meta"] = meta
    return out


def step_bundle(case: dict[str, Any] | None, case_step: int) -> dict[str, Any]:
    if not isinstance(case, dict) or case_step <= 0:
        return {}
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    return dict(spk.get(str(case_step)) or spk.get(str(int(case_step))) or {})
