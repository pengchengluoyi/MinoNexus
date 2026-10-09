"""编号句 → 事件身份 + 参数。句式来自用例密钥目录的 compile_patterns，不写产品文案。"""
from __future__ import annotations

import re
from typing import Any

_RELATION = {
    "右侧": ("right", "horizontal"),
    "右边": ("right", "horizontal"),
    "左侧": ("left", "horizontal"),
    "左边": ("left", "horizontal"),
    "上方": ("above", "vertical"),
    "下方": ("below", "vertical"),
}

_LAYER_ZH = {"operation": "操作", "expected": "预期"}


def _patterns() -> list[dict[str, Any]]:
    from mino_nexus.services.case_resource_key_catalog import catalog_payload

    rows: list[dict[str, Any]] = []
    for ent in catalog_payload().get("entries") or []:
        if not isinstance(ent, dict):
            continue
        layer = str(ent.get("key_layer") or "")
        if layer not in ("operation", "expected"):
            continue
        for pat in ent.get("compile_patterns") or []:
            if not isinstance(pat, dict) or not str(pat.get("regex") or "").strip():
                continue
            rows.append(
                {
                    **pat,
                    "layer": layer,
                    "key_ref": str(ent.get("key_ref") or ""),
                    "platforms": list(ent.get("platforms") or pat.get("platforms") or []),
                    "schemes": list(ent.get("schemes") or ["visual", "dom"]),
                    "event_name": str(pat.get("event_name") or ent.get("event_name") or ent.get("write_category") or ""),
                }
            )
    rows.sort(key=lambda r: int(r.get("priority") or 0), reverse=True)
    return rows


def _clean_label(value: str) -> str:
    text = str(value or "").strip()
    for noise in ("默认", "一个", "当前"):
        if text == noise:
            return ""
    return text[:80]


def _rules_from_match(pat: dict[str, Any], match: re.Match[str], clause: str) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    const = pat.get("rules_const") if isinstance(pat.get("rules_const"), dict) else {}
    rules.update(const)
    wanted = pat.get("rules_from") if isinstance(pat.get("rules_from"), list) else []
    for name in wanted:
        raw = match.groupdict().get(str(name))
        if raw is None or str(raw).strip() == "":
            continue
        text = str(raw).strip()
        if name == "max_length":
            try:
                rules["max_length"] = int(text)
            except ValueError:
                continue
            continue
        if name == "relation_zh":
            relation, axis = _RELATION.get(text, ("", ""))
            if relation:
                rules["relation"] = relation
                rules["axis"] = axis
            continue
        if name in ("label", "which", "relative_to", "subject", "title", "body"):
            text = _clean_label(text)
            if name == "subject":
                text = re.sub(r"^一个", "", text).strip()
            if name == "which" and text and "弹窗" not in text:
                text = f"{text}弹窗"
            if not text:
                continue
        rules[str(name)] = text
    flags = pat.get("flag_if") if isinstance(pat.get("flag_if"), dict) else {}
    for token, extra in flags.items():
        if token in clause and isinstance(extra, dict):
            rules.update(extra)
    if str(pat.get("key_ref") or "") == "operation.check_box":
        rules["checked"] = not bool(match.groupdict().get("cancel"))
    if rules.get("which"):
        rules["which"] = str(rules["which"]).strip(" ，,。")
    return rules


def classify_clause(clause: str) -> dict[str, Any] | None:
    """返回最高优先级命中。操作和预期同时命中则 ambiguous。"""
    text = str(clause or "").strip()
    if len(text) < 2:
        return None
    hits: list[dict[str, Any]] = []
    for pat in _patterns():
        reject = str(pat.get("reject_if") or "")
        if reject and re.search(reject, text):
            continue
        try:
            found = re.search(str(pat["regex"]), text, flags=re.I)
        except re.error:
            continue
        if not found:
            continue
        layer = str(pat.get("layer") or "")
        rules = _rules_from_match(pat, found, text)
        role = str(pat.get("element_role") or "")
        if role == "overlay" and "弹窗" in str(rules.get("subject") or text):
            role = "dialog"
        hit = {
            "layer": layer,
            "key_ref": str(pat.get("key_ref") or ""),
            "event_name": str(pat.get("event_name") or ""),
            "hook_cap": str(pat.get("hook_cap") or ""),
            "assert_mode": str(pat.get("assert_mode") or ""),
            "element": {"role": role} if role else {},
            "rules": rules,
            "platforms": list(pat.get("platforms") or []),
            "schemes": list(pat.get("schemes") or ["visual", "dom"]),
            "merge": str(pat.get("merge") or ""),
            "needs_bind": str(pat.get("needs_bind") or ""),
            "priority": int(pat.get("priority") or 0),
            "clause": text,
        }
        if not hits:
            hits.append(hit)
            continue
        if hit["layer"] != hits[0]["layer"]:
            return {"ambiguous": True, "clause": text, "hits": [hits[0], hit]}
        if hit["key_ref"] == hits[0]["key_ref"]:
            merged = dict(hits[0]["rules"])
            merged.update(rules)
            hits[0]["rules"] = merged
            if hit.get("needs_bind") and not hits[0].get("needs_bind"):
                hits[0]["needs_bind"] = hit["needs_bind"]
            continue
        break
    return hits[0] if hits else None


def merge_dialog_checks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一预期里「某弹窗弹出」与「不展示其他弹窗」合成一条。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        merge = str(row.get("merge") or "")
        if (
            merge == "dialog_exclusive"
            and out
            and str(out[-1].get("key_ref") or "") == "expected.dialog"
            and str(out[-1].get("merge") or "") == "dialog_present"
        ):
            rules = dict(out[-1].get("rules") or {})
            rules["exclusive"] = True
            out[-1]["rules"] = rules
            clauses = list(out[-1].get("source_clauses") or [out[-1].get("clause")])
            clauses.append(str(row.get("clause") or ""))
            out[-1]["source_clauses"] = clauses
            continue
        if (
            merge == "dialog_present"
            and out
            and str(out[-1].get("merge") or "") == "dialog_exclusive"
            and not str((out[-1].get("rules") or {}).get("which") or "").strip()
        ):
            rules = dict(row.get("rules") or {})
            rules["exclusive"] = True
            item = dict(row)
            item["rules"] = rules
            item["source_clauses"] = [str(out[-1].get("clause") or ""), str(row.get("clause") or "")]
            out[-1] = item
            continue
        item = dict(row)
        item["source_clauses"] = [str(row.get("clause") or "")]
        out.append(item)
    return out


def incomplete_reason(row: dict[str, Any]) -> str:
    ref = str(row.get("key_ref") or "")
    rules = row.get("rules") if isinstance(row.get("rules"), dict) else {}
    if ref == "expected.dialog" and rules.get("exclusive") and not str(rules.get("which") or "").strip():
        if not rules.get("present"):
            return "哪个弹窗"
    if ref == "expected.input_value" and row.get("needs_bind") and not row.get("bind"):
        return "上一步输入的文案"
    if ref == "expected.spatial_relation" and not str(rules.get("relative_to") or "").strip():
        return "相对元素"
    return ""


_GLUE = set(" \t\r\n，,。；;、")


def _layer_entries(layer: str) -> list[dict[str, Any]]:
    from mino_nexus.services.case_resource_key_catalog import catalog_payload

    return [
        e
        for e in (catalog_payload().get("entries") or [])
        if isinstance(e, dict) and str(e.get("key_layer") or "") == layer
    ]


def _ranges_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def _login_span(text: str, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    entry = next((e for e in entries if str(e.get("key_ref") or "") == "operation.login_flow"), None)
    if not entry:
        return None
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for slot in entry.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        hit = None
        for lit in slot.get("literals") or []:
            token = str(lit or "")
            if not token:
                continue
            at = text.find(token)
            if at >= 0:
                hit = {
                    "id": str(slot.get("id") or ""),
                    "start": at,
                    "end": at + len(token),
                    "text": token,
                }
                break
        if hit:
            found.append(hit)
        elif slot.get("required"):
            missing.append(str(slot.get("id") or "slot"))
    if not found:
        return None
    start = min(int(s["start"]) for s in found)
    end = max(int(s["end"]) for s in found)
    return {
        "layer": "operation",
        "key_ref": "operation.login_flow",
        "event_name": str(entry.get("event_name") or entry.get("write_category") or "登录流"),
        "hook_cap": "",
        "kind": "flow_block",
        "block_id": str(entry.get("block_id") or "fb.global.login"),
        "element": {},
        "rules": {},
        "platforms": list(entry.get("platforms") or []),
        "schemes": list(entry.get("schemes") or ["visual", "dom"]),
        "priority": 100,
        "start": start,
        "end": end,
        "clause": text[start:end],
        "slots": found,
        "missing_slots": missing,
        "param_slots": list(entry.get("param_slots") or []),
    }


def _target_from_hit(hit: dict[str, Any]) -> dict[str, str]:
    rules = hit.get("rules") if isinstance(hit.get("rules"), dict) else {}
    role = str((hit.get("element") or {}).get("role") or "")
    if role == "checkbox":
        role = "checkbox"
    name = str(rules.get("name") or rules.get("label") or rules.get("target") or "").strip()
    out: dict[str, str] = {}
    if role:
        out["role"] = role
    if name:
        out["name"] = name
    return out


def _missing_param_slots(hit: dict[str, Any]) -> list[str]:
    missing = [str(x) for x in (hit.get("missing_slots") or []) if str(x)]
    target = hit.get("target") if isinstance(hit.get("target"), dict) else {}
    rules = hit.get("rules") if isinstance(hit.get("rules"), dict) else {}
    for slot in hit.get("param_slots") or []:
        if not isinstance(slot, dict) or not slot.get("required"):
            continue
        sid = str(slot.get("id") or "")
        if sid == "target.role" and not str(target.get("role") or ""):
            missing.append("target.role")
        elif sid == "target.name" and not str(target.get("name") or ""):
            missing.append("target.name")
        elif sid == "target" and not str(target.get("name") or rules.get("target") or ""):
            missing.append("target")
        elif sid not in ("target.role", "target.name", "target") and not str(rules.get(sid) or target.get(sid) or ""):
            missing.append(sid)
    enum = []
    for slot in hit.get("param_slots") or []:
        if isinstance(slot, dict) and str(slot.get("id") or "") == "target.role":
            enum = [str(x) for x in (slot.get("enum") or [])]
    role = str(target.get("role") or "")
    if role and enum and role not in enum:
        missing.append("target.role")
    return list(dict.fromkeys(missing))


def compile_text_spans(text: str, layer: str) -> tuple[list[dict[str, Any]], str]:
    """一整段编号步上的跨度。下标来自目录句式，不再按标点切开。"""
    raw = str(text or "")
    entries = _layer_entries(layer)
    reserved: list[tuple[int, int]] = []
    chosen: list[dict[str, Any]] = []
    if layer == "operation":
        login = _login_span(raw, entries)
        if login is not None:
            chosen.append(login)
            reserved.append((int(login["start"]), int(login["end"])))
    candidates: list[dict[str, Any]] = []
    for pat in _patterns():
        if str(pat.get("layer") or "") != layer:
            continue
        if str(pat.get("key_ref") or "") == "operation.login_flow":
            continue
        reject = str(pat.get("reject_if") or "")
        try:
            found_iter = list(re.finditer(str(pat["regex"]), raw, flags=re.I))
        except re.error:
            continue
        for found in found_iter:
            if reject and re.search(reject, found.group(0)):
                continue
            start, end = found.span()
            if any(_ranges_overlap(start, end, a, b) for a, b in reserved):
                continue
            rules = _rules_from_match(pat, found, found.group(0))
            unit = str(rules.get("wait_unit") or "")
            if rules.get("wait_ms") and unit in ("秒", "s"):
                try:
                    rules["wait_ms"] = int(rules["wait_ms"]) * 1000
                except (TypeError, ValueError):
                    pass
                rules.pop("wait_unit", None)
            role = str(pat.get("element_role") or "")
            hit = {
                "layer": layer,
                "key_ref": str(pat.get("key_ref") or ""),
                "event_name": str(pat.get("event_name") or ""),
                "hook_cap": str(pat.get("hook_cap") or ""),
                "assert_mode": str(pat.get("assert_mode") or ""),
                "element": {"role": role} if role else {},
                "rules": rules,
                "platforms": list(pat.get("platforms") or []),
                "schemes": list(pat.get("schemes") or ["visual", "dom"]),
                "merge": str(pat.get("merge") or ""),
                "needs_bind": str(pat.get("needs_bind") or ""),
                "priority": int(pat.get("priority") or 0),
                "start": start,
                "end": end,
                "clause": found.group(0),
                "param_slots": [],
            }
            ent = next((e for e in entries if str(e.get("key_ref") or "") == hit["key_ref"]), None)
            if isinstance(ent, dict):
                hit["param_slots"] = list(ent.get("param_slots") or [])
                hit["kind"] = str(ent.get("kind") or "")
                if ent.get("block_id"):
                    hit["block_id"] = ent.get("block_id")
            hit["target"] = _target_from_hit(hit)
            if hit["target"].get("role") == "input" and not hit["target"].get("name"):
                named = str(rules.get("target") or "").strip()
                if named:
                    hit["target"]["name"] = named
            hit["missing_slots"] = _missing_param_slots(hit)
            candidates.append(hit)
    candidates.sort(key=lambda h: (-int(h.get("priority") or 0), int(h["start"]), -(int(h["end"]) - int(h["start"]))))
    for hit in candidates:
        start, end = int(hit["start"]), int(hit["end"])
        if any(_ranges_overlap(start, end, int(k["start"]), int(k["end"])) for k in chosen):
            continue
        chosen.append(hit)
    chosen.sort(key=lambda h: int(h["start"]))
    login = next(
        (
            h for h in chosen
            if h.get("key_ref") == "operation.login_flow" and not h.get("missing_slots")
        ),
        None,
    )
    if login is not None:
        slots = list(login.get("slots") or [])
        for h in chosen:
            if h is login:
                continue
            slots.append({
                "id": "entry",
                "start": int(h["start"]),
                "end": int(h["end"]),
                "text": str(h.get("clause") or "")[:80],
                "target": dict(h.get("target") or {}),
            })
        login["slots"] = slots
        login["start"] = 0
        login["end"] = len(raw)
        login["clause"] = raw.strip()
        chosen = [login]
    uncovered: list[str] = []
    for i, ch in enumerate(raw):
        if ch in _GLUE:
            continue
        if any(int(h["start"]) <= i < int(h["end"]) for h in chosen):
            continue
        uncovered.append(ch)
    return chosen, "".join(uncovered)[:80]


def platform_block(row: dict[str, Any], platform: str) -> bool:
    allowed = [str(x).strip().lower() for x in (row.get("platforms") or []) if str(x).strip()]
    plat = str(platform or "").strip().lower()
    if not allowed or not plat:
        return False
    return plat not in allowed


def user_message(
    *,
    case_step: int,
    layer: str,
    clause: str,
    reason: str,
    event_name: str = "",
    detail: str = "",
    platform: str = "",
    scheme: str = "",
    supported_schemes: list[str] | None = None,
) -> str:
    bar = _LAYER_ZH.get(layer, "步骤")
    other = "预期" if layer == "operation" else "操作"
    text = str(clause or "").strip()
    head = f"步骤 {int(case_step)} {bar}「{text}」"
    if reason == "layer_mismatch":
        return f"{head}无法执行：这是{other}（{event_name or '事件'}），写在了{bar}栏。"
    if reason == "ambiguous_clause":
        return f"{head}无法执行：同一句里既有操作又有预期，请拆成两栏。"
    if reason == "platform_unsupported":
        names = {"web": "Web", "android": "Android", "ios": "iOS"}
        where = names.get(str(detail or "").strip().lower(), detail or "Web")
        current = names.get(str(platform or "").strip().lower(), platform or "未知")
        return f"{head}无法执行：{event_name or '该事件'}只在 {where} 执行，当前平台是 {current}。"
    if reason == "scheme_unsupported":
        got = "看图" if scheme == "visual" else "DOM" if scheme == "dom" else scheme or "当前路线"
        other_scheme = "、".join("看图" if s == "visual" else "DOM" if s == "dom" else s for s in (supported_schemes or []))
        return f"{head}无法执行：当前任务是{got}，{event_name or '该事件'}只实现了{other_scheme or '另一路线'}。"
    if reason == "params_incomplete":
        return f"{head}无法验证：{event_name or '该检查'}缺少{detail or '必要参数'}。"
    if reason == "missing_slot":
        return f"{head}无法执行：缺少{detail or '必填槽'}。"
    if reason == "uncovered":
        return f"{head}未执行：{detail}"
    if reason == "unmapped":
        return f"{head}无法执行：当前目录没有对应事件。"
    if reason in ("unavailable_cap", "capability_disabled"):
        return f"{head}无法执行：事件 {event_name or detail or '该能力'} 已停用。"
    if reason == "uncoverable_event":
        return f"{head}无法执行：事件 {event_name or '该能力'} 当前不能做。"
    return f"{head}无法执行：{detail or reason or '无法覆盖'}。"
