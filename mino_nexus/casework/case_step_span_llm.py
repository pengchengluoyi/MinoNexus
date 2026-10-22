"""编号步拆成跨度：模型交摘录，程序算下标并校验。不读目录正则。"""
from __future__ import annotations

import json
import re
from typing import Any

_GLUE = set(" \t\r\n，,。；;、并")
_RELATION = {
    "右侧": "right",
    "右边": "right",
    "左侧": "left",
    "左边": "left",
    "上方": "above",
    "下方": "below",
    "right": "right",
    "left": "left",
    "above": "above",
    "below": "below",
}
JOB_ID = "case-step-compile"


def layer_menu(layer: str) -> list[dict[str, Any]]:
    from mino_nexus.resources.case_resource_key_catalog import catalog_payload

    menu: list[dict[str, Any]] = []
    for ent in catalog_payload().get("entries") or []:
        if not isinstance(ent, dict) or str(ent.get("key_layer") or "") != layer:
            continue
        ref = str(ent.get("key_ref") or "").strip()
        if not ref:
            continue
        slots = []
        for slot in ent.get("slots") or []:
            if not isinstance(slot, dict) or not str(slot.get("id") or "").strip():
                continue
            slots.append(
                {
                    "id": str(slot.get("id") or ""),
                    "label": str(slot.get("label") or slot.get("id") or ""),
                    "required": bool(slot.get("required")),
                }
            )
        params = []
        for slot in ent.get("param_slots") or []:
            if not isinstance(slot, dict) or not str(slot.get("id") or "").strip():
                continue
            row: dict[str, Any] = {
                "id": str(slot.get("id") or ""),
                "required": bool(slot.get("required")),
            }
            if slot.get("enum"):
                row["enum"] = [str(x) for x in slot.get("enum") or []]
            if slot.get("description"):
                row["description"] = str(slot["description"])
            params.append(row)
        cfg = [str(x) for x in (ent.get("config_keys") or []) if str(x)]
        hook = ""
        assert_mode = ""
        for item in cfg:
            if item.startswith("assert.mode="):
                assert_mode = item.split("=", 1)[1]
            elif not hook:
                hook = item
        menu.append(
            {
                "key_ref": ref,
                "name": str(ent.get("event_name") or ent.get("write_category") or ref),
                "description": str(ent.get("runtime") or ""),
                "kind": str(ent.get("kind") or ""),
                "block_id": str(ent.get("block_id") or ""),
                "platforms": [str(x) for x in (ent.get("platforms") or []) if str(x)],
                "schemes": [str(x) for x in (ent.get("schemes") or ["visual", "dom"])],
                "slots": slots,
                "param_slots": params,
                "hook_cap": hook,
                "assert_mode": assert_mode,
            }
        )
    return menu


def _menu_index(menu: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("key_ref") or ""): row for row in menu}


def _find_quote(source: str, quote: str, occupied: list[tuple[int, int]]) -> tuple[int, int] | None:
    text = str(quote or "")
    if not text:
        return None
    start = 0
    while True:
        found = source.find(text, start)
        if found < 0:
            return None
        end = found + len(text)
        if not any(not (end <= a or found >= b) for a, b in occupied):
            return found, end
        start = found + 1


def _coerce_param(slot_id: str, value: Any) -> Any:
    if slot_id == "relation":
        return _RELATION.get(str(value or "").strip(), str(value or "").strip())
    if slot_id in ("exclusive", "present", "accepts_special"):
        if isinstance(value, bool):
            return value
        token = str(value or "").strip().lower()
        if token in ("true", "1", "yes"):
            return True
        if token in ("false", "0", "no"):
            return False
    if slot_id in ("wait_ms", "max_length"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return value


def _hit_from_span(
    source: str,
    span: dict[str, Any],
    spec: dict[str, Any],
    *,
    layer: str,
    start: int,
    end: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    # Catalog param_slots describe the schema; they are not model output values.
    # Reject this shape before missing-slot classification so a provider format
    # error is never presented as missing business input.
    if "param_slots" in span:
        return None, ["模型输出结构错误：span 不得输出 param_slots；参数值必须放在 params 对象，例如 params.direction，而不是 id/value 数组"]
    if "params" in span and not isinstance(span["params"], dict):
        return None, ["模型输出结构错误：params 必须是参数名到值的 JSON 对象"]
    if "slots" in span and (not isinstance(span["slots"], list) or any(not isinstance(slot, dict) for slot in span["slots"])):
        return None, ["模型输出结构错误：slots 必须是含 id 和原文 quote 的对象数组"]
    errors: list[str] = []
    quote = str(span.get("quote") or "")
    if spec.get("key_ref") == "operation.login_flow" and re.match(r"\s*(?:点击|点按|单击|点一下)", quote) and not re.search(r"输入|填写|密码|验证码", quote):
        return None, ["单独点击登录按钮应选择目录中的点击事件，不是完整登录流程；请重新分类"]
    if (spec.get("key_ref") in {"operation.tap", "operation.dynamic_visual"}
            and re.match(r"\s*(?:输入|填写)\s*(?:手机号|手机号码|账号|帐号|验证码|邮箱|密码)", quote)
            and "登录" in quote):
        return None, ["原句要求填写凭证并完成登录，应选择完整登录流；单独点击事件不能吞掉填写动作，请重新分类"]
    raw_params = span.get("params") if isinstance(span.get("params"), dict) else {}
    allowed = {str(s.get("id") or "") for s in (spec.get("param_slots") or [])}
    rules: dict[str, Any] = {}
    target: dict[str, Any] = {}
    missing: list[str] = []
    for slot in spec.get("param_slots") or []:
        sid = str(slot.get("id") or "")
        if sid.startswith("target."):
            key = sid.split(".", 1)[1]
            raw_target = raw_params.get("target") if isinstance(raw_params.get("target"), dict) else {}
            val = raw_target.get(key)
            if val is None:
                val = raw_params.get(sid)
            val = _coerce_param(sid, val)
            enum = [str(x) for x in (slot.get("enum") or [])]
            if slot.get("required") and not str(val or "").strip():
                missing.append(sid)
            elif str(val or "").strip() and enum and str(val) not in enum:
                errors.append(f"{sid} 不在枚举内")
            elif str(val or "").strip():
                target[key] = val
            continue
        if sid not in raw_params or raw_params.get(sid) in ("", None):
            if slot.get("required"):
                missing.append(sid)
            continue
        if sid == "target" and not isinstance(raw_params.get(sid), dict):
            name = str(raw_params.get(sid) or "").strip()
            if name:
                target["name"] = name
                rules["target"] = name
            elif slot.get("required"):
                missing.append(sid)
            continue
        val = _coerce_param(sid, raw_params.get(sid))
        enum = [str(x) for x in (slot.get("enum") or [])]
        if enum and str(val) not in enum:
            errors.append(f"{sid} 不在枚举内")
        else:
            rules[sid] = val
    for key in raw_params:
        if key == "target":
            continue
        if allowed and key not in allowed and not str(key).startswith("target."):
            errors.append(f"参数 {key} 不在目录槽里")
    slots_out: list[dict[str, Any]] = []
    declared = {str(s.get("id") or ""): s for s in (spec.get("slots") or [])}
    occupied_child: list[tuple[int, int]] = []
    seen_slot_ids: set[str] = set()
    parent = source[start:end]
    for slot in span.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        sid = str(slot.get("id") or "").strip()
        if sid not in declared:
            errors.append(f"槽 {sid or '未命名'} 不在 {spec.get('key_ref')} 上")
            continue
        if sid in seen_slot_ids:
            errors.append(f"槽 {sid} 重复声明")
            continue
        seen_slot_ids.add(sid)
        child_quote = str(slot.get("quote") or "")
        # 子槽描述不同语义角色，可以引用同一段作者原文；父事件之间仍禁止重叠。
        local = _find_quote(parent, child_quote, [])
        if local is None:
            errors.append(f"槽 {sid} 的摘录不在这一步里")
            continue
        occupied_child.append(local)
        slots_out.append(
            {
                "id": sid,
                "start": start + local[0],
                "end": start + local[1],
                "text": child_quote[:80],
            }
        )
    # 高层登录目标由完整逻辑块展开，不补造作者没写的凭证/提交槽。
    for sid, slot in declared.items():
        if slot.get("required") and sid not in {str(s.get("id") or "") for s in slots_out}:
            missing.append(sid)
    if missing:
        errors.append("槽 " + "、".join(missing))
        gap = _gap_text(parent, occupied_child)
        if gap:
            errors.append("还没进槽的字：" + gap)
    if errors:
        return None, errors
    wait = rules.get("wait_ms")
    if isinstance(wait, int) and 0 < wait < 1000 and "秒" in quote and "毫秒" not in quote:
        rules["wait_ms"] = wait * 1000
    kind = str(spec.get("kind") or "")
    if spec.get("block_id"):
        kind = kind or "flow_block"
    hit: dict[str, Any] = {
        "layer": layer,
        "key_ref": str(spec.get("key_ref") or ""),
        "event_name": str(spec.get("name") or ""),
        "hook_cap": str(spec.get("hook_cap") or ""),
        "assert_mode": str(spec.get("assert_mode") or ""),
        "kind": kind,
        "rules": rules,
        "target": target,
        "element": {"role": str(target.get("role") or "")} if target.get("role") else {},
        "platforms": list(spec.get("platforms") or []),
        "schemes": list(spec.get("schemes") or ["visual", "dom"]),
        "start": start,
        "end": end,
        "clause": quote,
        "slots": slots_out,
        "missing_slots": [],
        "param_slots": list(spec.get("param_slots") or []),
    }
    if spec.get("block_id"):
        hit["block_id"] = spec.get("block_id")
    if str(spec.get("key_ref") or "") == "expected.input_value":
        hit["needs_bind"] = "input_text"
    if rules.get("exclusive") and not str(rules.get("which") or "").strip():
        hit["merge"] = "dialog_exclusive"
    elif str(spec.get("key_ref") or "") == "expected.dialog":
        hit["merge"] = "dialog_present"
        if "present" not in rules:
            rules["present"] = True
            hit["rules"] = rules
    if rules.get("relation"):
        rules["relation_zh"] = {
            "right": "右侧",
            "left": "左侧",
            "above": "上方",
            "below": "下方",
        }.get(str(rules.get("relation") or ""), "")
    return hit, []


def _text_covered(source: str, occupied: list[tuple[int, int]]) -> bool:
    for i, ch in enumerate(source):
        if ch in _GLUE:
            continue
        if not any(a <= i < b for a, b in occupied):
            return False
    return True


def _gap_text(parent: str, occupied: list[tuple[int, int]]) -> str:
    chars: list[str] = []
    for i, ch in enumerate(parent):
        if ch in _GLUE:
            continue
        if any(a <= i < b for a, b in occupied):
            continue
        chars.append(ch)
    return "".join(chars)[:40]


def assess_spans(
    source: str,
    layer: str,
    raw_spans: Any,
    menu: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型摘录收成跨度。失败时 hits 为空，errors 给下一次调用。"""
    if not isinstance(raw_spans, list):
        return [], ["模型输出结构错误：spans 必须为数组"]
    if any(not isinstance(span, dict) for span in raw_spans):
        return [], ["模型输出结构错误：spans 的每一项必须是对象"]
    index = _menu_index(menu)
    other: dict[str, str] = {}
    if layer in ("operation", "expected"):
        for other_layer in ("operation", "expected"):
            if other_layer == layer:
                continue
            for row in layer_menu(other_layer):
                other[str(row.get("key_ref") or "")] = other_layer
    spans = [row for row in raw_spans if isinstance(row, dict)]
    spans = [
        row for row in spans
        if not any(
            str(row.get("quote") or "")
            and str(row.get("quote") or "") != str(other.get("quote") or "")
            and str(row.get("quote") or "") in str(other.get("quote") or "")
            for other in spans
        )
    ]
    hits: list[dict[str, Any]] = []
    errors: list[str] = []
    occupied: list[tuple[int, int]] = []
    from mino_nexus.casework.case_step_intent import (
        apply_span_intent,
        intent_role_errors,
        normalize_intent_role,
        reconcile_hit_layer,
    )

    for span in spans:
        if not isinstance(span, dict):
            errors.append("有一条跨度不是对象")
            continue
        span = apply_span_intent(span, layer)
        ref = str(span.get("key_ref") or "").strip()
        spec = index.get(ref)
        if spec is None:
            if ref in other:
                errors.append(f"{ref} 不属于这一栏")
            else:
                errors.append(f"{ref or '空'} 不在这一层目录")
            continue
        errors.extend(intent_role_errors(span, spec, layer))
        quote = str(span.get("quote") or "")
        placed = _find_quote(source, quote, occupied)
        if placed is None:
            if occupied and _text_covered(source, occupied):
                continue
            errors.append(f"摘录对不上原文：{quote[:40]}")
            continue
        hit, slot_errors = _hit_from_span(
            source, span, spec, layer=layer, start=placed[0], end=placed[1]
        )
        if slot_errors or hit is None:
            errors.extend(slot_errors or ["跨度无效"])
            continue
        hit["intent_role"] = normalize_intent_role(span.get("intent_role"))
        hit = reconcile_hit_layer(hit, author_layer=layer)
        hit["start"], hit["end"] = placed
        occupied.append(placed)
        hits.append(hit)
    uncovered: list[str] = []
    for i, ch in enumerate(source):
        if ch in _GLUE:
            continue
        if any(a <= i < b for a, b in occupied):
            continue
        uncovered.append(ch)
    if uncovered and (occupied or not errors):
        # Give the model exact source positions, rather than the ambiguous generic
        # failure that previously caused an identical second response. Keep all
        # author text; modifiers must be included in the appropriate span quote.
        positions = [i for i, ch in enumerate(source)
            if ch not in _GLUE and not any(a <= i < b for a, b in occupied)]
        ranges = []
        for index in positions:
            if ranges and index == ranges[-1][1]:
                ranges[-1][1] = index + 1
            else:
                ranges.append([index, index + 1])
        detail = "、".join(f"[{a},{b})「{source[a:b][:60]}」" for a, b in ranges[:8])
        errors.append("原步骤未完整映射到事件；未覆盖的原文区间：" + detail
            + "。请把修饰语纳入相应事件的 quote，保持完整原句，不从残余字符推断新增操作")
    if errors:
        return [], errors
    hits.sort(key=lambda row: int(row.get("start") or 0))
    hits = _fold_reload_wait(hits)
    return hits, []


def _fold_reload_wait(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """刷新句里的等待是刷新的参数，不再另成一条等待事件。"""
    import re

    reload = next((row for row in hits if row.get("key_ref") == "operation.reload_page"), None)
    if reload is None:
        return hits
    waits = [
        row for row in hits
        if row is not reload and (
            row.get("key_ref") == "operation.wait_ready"
            or str(row.get("clause") or "").strip().startswith("等待")
        )
    ]
    if not waits:
        return hits
    rules = dict(reload.get("rules") or {})
    for wait in waits:
        clause = str(wait.get("clause") or "")
        found = re.search(r"(\d+)\s*(毫秒|ms|秒|s)", clause, flags=re.I)
        if not found:
            continue
        amount = int(found.group(1))
        unit = found.group(2).lower()
        rules["wait_ms"] = amount if unit in ("毫秒", "ms") else amount * 1000
    reload["rules"] = rules
    return [row for row in hits if row not in waits]


def _reason_from_errors(errors: list[str]) -> tuple[str, str]:
    detail = "；".join(errors)[:240]
    blob = detail
    if "模型输出结构错误" in blob:
        return "compile_span_schema_invalid", detail
    if "摘录对不上" in blob or "不在这一步里" in blob:
        return "quote_miss", detail
    if "不属于这一栏" in blob:
        return "layer_mismatch", detail
    if any(error.startswith("槽 ") or "还没进槽" in error for error in errors):
        return "missing_slot", detail
    if "没有落进任何事件" in blob or "未完整映射到事件" in blob:
        return "uncovered", "原步骤未完整映射到事件，请核对完整原句和编译预览"
    if "不在这一层目录" in blob or "不是 spans" in blob:
        return "unmapped", detail
    return "unmapped", detail


def _ask(payload: dict[str, Any]) -> tuple[Any, str]:
    from mino_nexus.platform.llm.dispatch_log import bind, reset
    from mino_nexus.platform.llm.client import call_chat_text, resolve_regression_provider
    from mino_nexus.platform.llm.render import JobRenderError, render

    provider, _gate = resolve_regression_provider()
    if provider is None:
        # Infrastructure failure is not a semantic mismatch. Do not retry the
        # same missing dependency for every clause or export raw provider details.
        raise ValueError("compile_provider_unavailable")
    slots = {"payload_json": json.dumps(payload, ensure_ascii=False)}
    try:
        messages, job_meta = render(JOB_ID, slots)
    except JobRenderError as exc:
        raise ValueError("compile_prompt_unavailable") from exc
    call = dict(job_meta.get("call") or {})
    tok = bind(
        trigger="case_step_compile",
        source="case_step_span_llm",
        role="test-engineer",
        job=JOB_ID,
        skill="case-compile",
    )
    try:
        parsed, meta = call_chat_text(
            provider=provider,
            messages=messages,
            temperature=float(call.get("temperature", 0.1)),
            max_tokens=int(call.get("max_tokens", 1200)),
            timeout_sec=float(call.get("timeout_sec", 60)),
            json_mode=bool(call.get("json_mode", True)),
        )
    finally:
        reset(tok)
    from mino_nexus.casework.compile_provenance import record
    record(meta, job_meta)
    # The client may salvage a valid JSON prefix from a truncated response.
    # Such a prefix is not a complete compilation, even if its spans validate.
    if (meta or {}).get("truncated") or (meta or {}).get("salvaged"):
        raise ValueError("compile_model_response_incomplete")
    if not isinstance(parsed, dict):
        # The client already owns transport/JSON retries and their time budget.
        # Never feed raw provider failures back as semantic repair instructions.
        raise ValueError("compile_model_call_failed")
    return parsed.get("spans"), ""


def compile_step_spans(
    text: str,
    layer: str,
    *,
    platform: str = "",
    field_defs: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """一次编号步。返回 (hits, fail)。fail 为 None 时 hits 已通过校验。"""
    source = str(text or "")
    if not source.strip():
        return [], None
    menu = layer_menu(layer)
    if not menu:
        return [], {"reason": "unmapped", "detail": "这一层目录没有事件"}
    other_layer = "expected" if layer == "operation" else "operation" if layer == "expected" else ""
    payload = {
        "layer": layer,
        "platform": str(platform or ""),
        "source": source,
        "catalog": menu,
        "other_layer_key_refs": [str(row.get("key_ref") or "") for row in layer_menu(other_layer)] if other_layer else [],
    }
    if field_defs is not None:
        payload["account_fields"] = [{key: row[key] for key in ("key", "label", "options") if key in row}
                                     for row in field_defs]
    spans, err = _ask(payload)
    errors: list[str] = [err] if err else []
    hits: list[dict[str, Any]] = []
    if not errors:
        hits, errors = assess_spans(source, layer, spans, menu)
    if errors and not all("不属于这一栏" in item for item in errors):
        payload["errors"] = errors[:8]
        spans, err = _ask(payload)
        errors = [err] if err else []
        hits = []
        if not errors:
            hits, errors = assess_spans(source, layer, spans, menu)
    if errors:
        reason, detail = _reason_from_errors(errors)
        return [], {"reason": reason, "detail": detail}
    return hits, None
