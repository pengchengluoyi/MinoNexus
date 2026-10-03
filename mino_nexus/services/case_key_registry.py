"""用例密钥注册表：catalog + 逻辑块 + Nav 屏幕 key_ref 聚合与校验（P8）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.services.case_resource_key_catalog import catalog_payload
from mino_nexus.services.case_step_key_compiler import compile_case_step_program_keys, sync_case_step_program_keys

_BLOCK_ID_RE = re.compile(r"【块[:：]\s*([^】]+)】")
_NAV_ID_RE = re.compile(r"【导航[:：]\s*([^】]+)】")


def default_block_key_ref(block_id: str) -> str:
    bid = str(block_id or "").strip()
    slug = re.sub(r"[^\w]+", "_", bid).strip("_").lower()
    return f"operation.block.{slug or 'unknown'}"


def default_screen_key_ref(state_id: str) -> str:
    sid = str(state_id or "").strip()
    slug = re.sub(r"[^\w]+", "_", sid).strip("_").lower()
    return f"operation.nav.{slug or 'unknown'}"


def catalog_key_refs(*, layers: tuple[str, ...] = ("operation", "expected", "precondition")) -> set[str]:
    out: set[str] = set()
    for ent in catalog_payload().get("entries") or []:
        if not isinstance(ent, dict):
            continue
        layer = str(ent.get("key_layer") or "precondition")
        if layer not in layers:
            continue
        ref = str(ent.get("key_ref") or "").strip()
        if ref:
            out.add(ref)
    return out


def flow_block_key_refs() -> dict[str, str]:
    """block_id → key_ref（全局库）。"""
    from mino_nexus.services.nav_flow_block_catalog import list_catalog

    mapping: dict[str, str] = {}
    for row in list_catalog():
        bid = str(row.get("block_id") or "").strip()
        if not bid:
            continue
        ref = str(row.get("key_ref") or "").strip() or default_block_key_ref(bid)
        mapping[bid] = ref
    return mapping


def nav_screen_key_refs(app_id: str) -> dict[str, str]:
    """state_id → key_ref（NavFSM state.meta）。"""
    from mino_nexus.services import nav_fsm_store as store

    app = str(app_id or "").strip()
    if not app:
        return {}
    doc = store.load(app) or {}
    out: dict[str, str] = {}
    for st in doc.get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "").strip()
        if not sid:
            continue
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        ref = str(meta.get("key_ref") or "").strip() or default_screen_key_ref(sid)
        out[sid] = ref
    return out


def ui_automation_summary(case: dict[str, Any] | None) -> dict[str, Any]:
    """Studio 选用例 / 详情：能否 UI 自动化 + blocked_by。"""
    empty = {"schema": "mino.ui_automation.v1", "coverable": True, "blocked_by": []}
    if not isinstance(case, dict):
        return dict(empty)
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    ua = meta.get("ui_automation") if isinstance(meta.get("ui_automation"), dict) else None
    if isinstance(ua, dict) and "coverable" in ua:
        return {
            "schema": str(ua.get("schema") or "mino.ui_automation.v1"),
            "coverable": ua.get("coverable") is not False,
            "blocked_by": [x for x in (ua.get("blocked_by") or []) if isinstance(x, dict)][:24],
        }
    try:
        from mino_nexus.services.case_step_key_compiler import compile_case_step_program_keys

        compiled = compile_case_step_program_keys(case)
        row = compiled.get("ui_automation")
        if isinstance(row, dict):
            return {
                "schema": str(row.get("schema") or "mino.ui_automation.v1"),
                "coverable": row.get("coverable") is not False,
                "blocked_by": [x for x in (row.get("blocked_by") or []) if isinstance(x, dict)][:24],
            }
    except Exception:  # noqa: BLE001
        pass
    return dict(empty)


def step_keys_summary_for_case(case: dict[str, Any] | None) -> dict[str, Any]:
    """Console 用例详情：密钥编译摘要（不重复全文 warnings）。"""
    if not isinstance(case, dict):
        return {"telemetry": "green", "warning_count": 0, "fallback_count": 0, "steps": {}}
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    warnings = list(meta.get("key_compile_warnings") or [])
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    fb = sum(1 for w in warnings if isinstance(w, dict) and w.get("fallback"))
    ua = ui_automation_summary(case)
    blocked = ua.get("blocked_by") or []
    blocked_by_step: dict[str, list[dict[str, Any]]] = {}
    for item in blocked:
        if not isinstance(item, dict):
            continue
        blocked_by_step.setdefault(str(item.get("case_step") or ""), []).append(item)
    steps_out: dict[str, Any] = {}
    for key, bundle in spk.items():
        if not isinstance(bundle, dict):
            continue
        do_plan = bundle.get("do_program_plan") if isinstance(bundle.get("do_program_plan"), dict) else {}
        ck_plan = bundle.get("check_program_plan") if isinstance(bundle.get("check_program_plan"), dict) else {}
        do_steps = [s for s in (do_plan.get("steps") or []) if isinstance(s, dict)]
        checks = [s for s in (ck_plan.get("checkpoints") or []) if isinstance(s, dict)]
        lights = []
        for s in do_steps:
            lights.append(
                {
                    "id": str(s.get("id") or ""),
                    "phase": "do",
                    "title": str(s.get("title") or "")[:160],
                    "observe": str(s.get("observe") or "exec"),
                    "key_ref": str(s.get("key_ref") or ""),
                    "source_clause": str(s.get("source_clause") or s.get("source_line") or "")[:200],
                }
            )
        for s in checks:
            lights.append(
                {
                    "id": str(s.get("id") or ""),
                    "phase": "check",
                    "title": str(s.get("title") or "")[:160],
                    "observe": str(s.get("observe") or "exec"),
                    "key_ref": str(s.get("key_ref") or ""),
                    "source_clause": str(s.get("source_clause") or s.get("source_line") or "")[:200],
                }
            )
        steps_out[str(key)] = {
            "operations": list(bundle.get("operations") or []),
            "expected": list(bundle.get("expected") or []),
            "do_step_count": len(do_steps),
            "check_count": len(checks),
            "blocked_by": list(blocked_by_step.get(str(key), [])),
            "lights": lights,
        }
    return {
        "telemetry": "red" if fb or not ua.get("coverable", True) else "green",
        "warning_count": len(warnings),
        "fallback_count": fb,
        "warnings_preview": warnings[:12],
        "ui_automation": ua,
        "coverable": ua.get("coverable") is not False,
        "steps": steps_out,
    }


def catalog_entries_by_layer() -> dict[str, Any]:
    """Console 用例密钥页：按层分组条目（含 operation/expected 高亮）。"""
    buckets: dict[str, list[dict[str, Any]]] = {
        "precondition": [],
        "operation": [],
        "expected": [],
        "generic": [],
    }
    for ent in catalog_payload().get("entries") or []:
        if not isinstance(ent, dict):
            continue
        layer = str(ent.get("key_layer") or "precondition").strip().lower()
        if layer not in buckets:
            layer = "generic"
        buckets[layer].append(
            {
                "key_ref": str(ent.get("key_ref") or "").strip(),
                "section": str(ent.get("section") or ""),
                "write_category": str(ent.get("write_category") or ""),
                "write_examples": list(ent.get("write_examples") or [])[:8],
                "dsl": str(ent.get("dsl") or ""),
                "runtime": str(ent.get("runtime") or ""),
                "observe": str(ent.get("observe") or "exec"),
            }
        )
    return {
        "entries_by_layer": buckets,
        "operation_count": len(buckets["operation"]),
        "expected_count": len(buckets["expected"]),
        "precondition_count": len(buckets["precondition"]),
        "compile_hint": "保存用例或 POST step-keys/compile 生成 meta.step_program_keys；warnings 在 meta.key_compile_warnings",
    }


def registry_payload(*, app_id: str = "") -> dict[str, Any]:
    blocks = flow_block_key_refs()
    screens = nav_screen_key_refs(app_id) if app_id else {}
    return {
        "catalog_operation": sorted(catalog_key_refs(layers=("operation",))),
        "catalog_expected": sorted(catalog_key_refs(layers=("expected",))),
        "flow_blocks": [
            {"block_id": bid, "key_ref": ref, "dsl": f"【块:{bid}】"}
            for bid, ref in sorted(blocks.items())
        ],
        "nav_screens": [
            {"state_id": sid, "key_ref": ref, "dsl": f"【导航:{sid}】"}
            for sid, ref in sorted(screens.items())
        ],
        "app_id": str(app_id or ""),
        "catalog_layers": catalog_entries_by_layer(),
    }


def _extract_structured_refs(case: dict[str, Any]) -> tuple[list[str], list[str]]:
    blocks: list[str] = []
    navs: list[str] = []
    blob = "\n".join(
        [
            str(case.get("precondition") or ""),
            str(case.get("steps_raw") or ""),
            str(case.get("expected_raw") or ""),
            "\n".join(str(x) for x in (case.get("steps") or [])),
            "\n".join(str(x) for x in (case.get("expected") or [])),
        ]
    )
    for m in _BLOCK_ID_RE.finditer(blob):
        blocks.append(m.group(1).strip())
    for m in _NAV_ID_RE.finditer(blob):
        navs.append(m.group(1).strip())
    return blocks, navs


def validate_case_step_keys(
    case: dict[str, Any],
    *,
    app_id: str = "",
    known_blocks: dict[str, str] | None = None,
    known_screens: dict[str, str] | None = None,
) -> dict[str, Any]:
    """保存/导入前校验：未知块、Nav 屏、fallback 行、建议补 key。"""
    synced = sync_case_step_program_keys(dict(case))
    warnings = list((synced.get("meta") or {}).get("key_compile_warnings") or [])
    block_map = known_blocks if known_blocks is not None else flow_block_key_refs()
    screen_map = known_screens if known_screens is not None else nav_screen_key_refs(app_id)
    block_refs, nav_refs = _extract_structured_refs(synced)
    unknown_blocks = sorted({b for b in block_refs if b and b not in block_map})
    unknown_screens = sorted({s for s in nav_refs if s and s not in screen_map})
    fallback_count = sum(1 for w in warnings if w.get("fallback"))
    issues: list[dict[str, Any]] = []
    for bid in unknown_blocks:
        issues.append(
            {
                "kind": "unknown_block",
                "block_id": bid,
                "suggested_key_ref": default_block_key_ref(bid),
                "dsl": f"【块:{bid}】",
            }
        )
    for sid in unknown_screens:
        issues.append(
            {
                "kind": "unknown_screen",
                "state_id": sid,
                "suggested_key_ref": default_screen_key_ref(sid),
                "dsl": f"【导航:{sid}】",
            }
        )
    for w in warnings:
        if w.get("fallback"):
            issues.append({"kind": "fallback_line", **w})
    return {
        "ok": not issues,
        "fallback_count": fallback_count,
        "unknown_block_count": len(unknown_blocks),
        "unknown_screen_count": len(unknown_screens),
        "issues": issues,
        "warnings": warnings,
        "step_program_keys": (synced.get("meta") or {}).get("step_program_keys"),
    }


_BLOCK_REASON_ZH = {
    "unmapped": "未映射到事件",
    "uncoverable_event": "当前不能做",
    "unavailable_cap": "事件已下线",
}


def _block_reason_zh(reason: str) -> str:
    raw = str(reason or "").strip()
    return _BLOCK_REASON_ZH.get(raw, raw or "无法覆盖")


def _format_plan_line(step: dict[str, Any], *, case_step: int) -> str:
    title = str(step.get("title") or step.get("source_clause") or step.get("source_line") or "").strip()
    ref = str(step.get("key_ref") or "").strip()
    rules = {}
    assert_payload = step.get("assert") if isinstance(step.get("assert"), dict) else {}
    params = step.get("params") if isinstance(step.get("params"), dict) else {}
    if isinstance(assert_payload.get("rules"), dict):
        rules = assert_payload["rules"]
    elif isinstance(params.get("rules"), dict):
        rules = params["rules"]
    element = assert_payload.get("element") if isinstance(assert_payload.get("element"), dict) else {}
    if not element and isinstance(params.get("element"), dict):
        element = params["element"]
    bit = title or ref or "（空）"
    role = str(element.get("role") or "").strip()
    if role and role not in bit:
        bit = f"{bit} · {role}"
    if rules:
        shown = "，".join(f"{k}={v}" for k, v in list(rules.items())[:4])
        bit = f"{bit}（{shown}）"
    elif ref and title and ref not in title:
        bit = f"{bit} → {ref}"
    return f"{int(case_step)}. {bit}"[:240]


def _format_block_line(item: dict[str, Any], *, case_step: int) -> str:
    message = str(item.get("user_message") or "").strip()
    if message:
        return message[:240]
    clause = str(item.get("source_clause") or item.get("line") or item.get("raw_text") or "").strip()
    reason = _block_reason_zh(str(item.get("reason") or "unmapped"))
    return f"{int(case_step)}. × {clause}（{reason}）"[:240]


def compile_preview_view(case: dict[str, Any] | None) -> dict[str, Any]:
    """导入预览：把程序图和未映射句编成步骤/预期/备注文案。"""
    if not isinstance(case, dict):
        return {
            "steps_parsed": [],
            "expected_parsed": [],
            "remark": "",
            "ui_automation": {"schema": "mino.ui_automation.v1", "coverable": True, "blocked_by": []},
        }
    from mino_nexus.services.case_step_key_compiler import sync_case_step_program_keys

    synced = sync_case_step_program_keys(dict(case))
    meta = synced.get("meta") if isinstance(synced.get("meta"), dict) else {}
    ua = ui_automation_summary(synced)
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    steps_parsed: list[str] = []
    expected_parsed: list[str] = []

    def _step_num(key: str) -> int:
        try:
            return int(key)
        except (TypeError, ValueError):
            return 0

    for key in sorted(spk.keys(), key=_step_num):
        bundle = spk.get(key) if isinstance(spk.get(key), dict) else {}
        n = _step_num(str(key)) or 1
        do_plan = bundle.get("do_program_plan") if isinstance(bundle.get("do_program_plan"), dict) else {}
        ck_plan = bundle.get("check_program_plan") if isinstance(bundle.get("check_program_plan"), dict) else {}
        for s in do_plan.get("steps") or []:
            if isinstance(s, dict):
                steps_parsed.append(_format_plan_line(s, case_step=n))
        for w in do_plan.get("compile_warnings") or []:
            blocked = w.get("blocked") if isinstance(w, dict) and isinstance(w.get("blocked"), dict) else None
            if blocked:
                steps_parsed.append(_format_block_line(blocked, case_step=n))
        for s in ck_plan.get("checkpoints") or []:
            if isinstance(s, dict):
                expected_parsed.append(_format_plan_line(s, case_step=n))
        for w in ck_plan.get("compile_warnings") or []:
            blocked = w.get("blocked") if isinstance(w, dict) and isinstance(w.get("blocked"), dict) else None
            if blocked:
                expected_parsed.append(_format_block_line(blocked, case_step=n))
    remarks: list[str] = []
    for item in ua.get("blocked_by") or []:
        if not isinstance(item, dict):
            continue
        n = int(item.get("case_step") or 0) or 1
        message = str(item.get("user_message") or "").strip()
        if message:
            remarks.append(message)
            continue
        clause = str(item.get("source_clause") or "").strip()
        reason = _block_reason_zh(str(item.get("reason") or ""))
        if clause:
            remarks.append(f"第{n}步「{clause}」{reason}")
        else:
            remarks.append(f"第{n}步{reason}")
    remark = ""
    if remarks:
        remark = "无法 UI 自动化：" + "；".join(remarks[:8])
    return {
        "steps_parsed": steps_parsed[:40],
        "expected_parsed": expected_parsed[:40],
        "remark": remark[:800],
        "ui_automation": ua,
    }


def import_row_key_summary(row: dict[str, Any], *, app_id: str = "") -> dict[str, Any]:
    case_like = {
        "precondition": row.get("precondition") or "",
        "steps": row.get("steps") or [],
        "expected": row.get("expected") or [],
        "steps_raw": row.get("steps_raw") or "",
        "expected_raw": row.get("expected_raw") or "",
    }
    v = validate_case_step_keys(case_like, app_id=app_id)
    view = compile_preview_view(case_like)
    ua = view.get("ui_automation") or {}
    return {
        "telemetry": "red" if v.get("issues") or not ua.get("coverable", True) else "green",
        "fallback_count": v.get("fallback_count"),
        "unknown_block_count": v.get("unknown_block_count"),
        "unknown_screen_count": v.get("unknown_screen_count"),
        "issues": (v.get("issues") or [])[:24],
        "ui_automation": ua,
        "coverable": ua.get("coverable") is not False,
        "steps_parsed": list(view.get("steps_parsed") or []),
        "expected_parsed": list(view.get("expected_parsed") or []),
        "remark": str(view.get("remark") or ""),
    }
