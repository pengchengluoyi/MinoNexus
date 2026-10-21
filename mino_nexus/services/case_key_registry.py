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
        return {"telemetry": "muted", "warning_count": 0, "fallback_count": 0, "steps": {}}
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    warnings = list(meta.get("key_compile_warnings") or [])
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    fb = sum(1 for w in warnings if isinstance(w, dict) and w.get("fallback"))
    # Display the same read-only admission verdict used by task submission.
    # Never invoke compilation or trust an old coverable flag on this read path.
    from mino_nexus.services.case_preflight import partition_cases
    runnable, report = partition_cases([case])
    stored = meta.get("ui_automation") or {}
    advisory = [item for item in stored.get("blocked_by") or []
                if isinstance(item, dict) and str(item.get("severity") or "blocking") != "blocking"]
    rejected = report["blocked"]
    reasons = rejected[0]["reasons"] if rejected else []
    blockers = rejected[0]["blocked_by"] if rejected else []
    if rejected and not blockers:
        blockers = [{"reason": "compile_required", "user_message": reasons[0]}]
    ua = {"schema": "mino.ui_automation.v1", "coverable": bool(runnable),
          "blocked_by": blockers + advisory}
    blocked = ua.get("blocked_by") or []
    blocking_items = [
        item for item in blocked if isinstance(item, dict) and str(item.get("severity") or "blocking") == "blocking"
    ]
    advisory_items = [
        item
        for item in blocked
        if isinstance(item, dict) and str(item.get("severity") or "") in ("human_only", "advisory")
    ]
    blocked_by_step: dict[str, list[dict[str, Any]]] = {}
    for item in blocking_items:
        blocked_by_step.setdefault(str(item.get("case_step") or ""), []).append(item)

    def _light_status(step: dict[str, Any], step_key: str) -> str:
        clause = str(step.get("source_clause") or step.get("source_line") or "")
        for item in blocked_by_step.get(step_key, []):
            src = str(item.get("source_clause") or "")
            if not src or src in clause or clause in src:
                return "blocking"
        key_ref = str(step.get("key_ref") or "")
        if key_ref in ("operation.author_remark", "expected.human_review"):
            return "advisory"
        if str(step.get("kind") or "") == "remark" or str(step.get("run_policy") or "") == "skip":
            if key_ref != "expected.ui_absent":
                return "advisory"
        return "ok"
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
                    "status": _light_status(s, str(key)),
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
                    "status": _light_status(s, str(key)),
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
    coverable = ua.get("coverable") is not False
    telemetry = "red" if not coverable else ("yellow" if advisory_items else "green")
    return {
        "telemetry": telemetry,
        "warning_count": len(warnings),
        "fallback_count": fb,
        "blocking_issue_count": len(blocking_items),
        "advisory_issue_count": len(advisory_items),
        "warnings_preview": warnings[:12],
        "ui_automation": ua,
        "coverable": coverable,
        "reasons": reasons,
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
    compiled_case: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存/导入前校验：未知块、Nav 屏、fallback 行、建议补 key。"""
    synced = compiled_case if compiled_case is not None else sync_case_step_program_keys(dict(case))
    from mino_nexus.services.v5_publication import guard_case_row
    synced = guard_case_row(synced)
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


_SLOT_ZH = {"credential": "凭证", "submit": "提交", "entry": "入口"}
_ROLE_ZH = {
    "button": "按钮",
    "element": "元素",
    "image": "图片",
    "checkbox": "勾选框",
    "switch": "开关",
    "input": "输入框",
    "page": "页面",
    "dialog": "弹窗",
}


def _rules_zh(rules: dict[str, Any]) -> str:
    parts: list[str] = []
    if rules.get("present"):
        parts.append("弹出")
    if rules.get("exclusive"):
        parts.append("不展示其他弹窗")
    which = str(rules.get("which") or "").strip()
    if which:
        parts.append(which)
    body = str(rules.get("body") or rules.get("text") or rules.get("title") or "").strip()
    if body:
        parts.append(f"「{body}」")
    name = str(rules.get("name") or rules.get("label") or "").strip()
    if name and name not in " ".join(parts):
        parts.append(name)
    return "，".join(parts)


def _format_plan_line(step: dict[str, Any], *, case_step: int) -> str:
    ref = str(step.get("key_ref") or "").strip()
    if ref == "operation.author_remark" or str(step.get("kind") or "") == "remark":
        title = str(step.get("title") or step.get("source_clause") or "").strip()
        return f"{int(case_step)}. 说明步骤（执行跳过）{title}"[:240]
    if ref == "expected.human_review":
        title = str(step.get("title") or step.get("source_clause") or "").strip()
        return f"{int(case_step)}. 人工复核 {title}"[:240]
    slots = step.get("slots") if isinstance(step.get("slots"), list) else []
    if ref == "operation.login_flow" or (str(step.get("kind") or "") == "flow_block" and slots):
        parts: list[str] = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            label = _SLOT_ZH.get(str(slot.get("id") or ""), str(slot.get("id") or "槽"))
            text = str(slot.get("text") or "").strip()
            target = slot.get("target") if isinstance(slot.get("target"), dict) else {}
            role = _ROLE_ZH.get(str(target.get("role") or ""), str(target.get("role") or ""))
            name = str(target.get("name") or "").strip()
            extra = f" · {role}" if role else ""
            if name and name not in text:
                extra = f"{extra} {name}".rstrip()
            parts.append(f"{label}「{text}」{extra}" if text else label)
        body = "；".join(parts) if parts else "登录流"
        return f"{int(case_step)}. 登录流 {body}"[:240]
    title = str(step.get("title") or step.get("source_clause") or step.get("source_line") or "").strip()
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
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    event = title or ref or "（空）"
    role = _ROLE_ZH.get(str(target.get("role") or element.get("role") or ""), str(target.get("role") or element.get("role") or ""))
    name = str(target.get("name") or rules.get("name") or rules.get("target") or "").strip()
    if role and role not in event:
        event = f"{event} · {role}"
    if name and name not in event:
        event = f"{event} {name}"
    shown = _rules_zh(rules)
    if shown and shown not in event:
        event = f"{event}（{shown}）"
    return f"{int(case_step)}. {event}"[:240]


def _format_block_line(item: dict[str, Any], *, case_step: int) -> str:
    message = str(item.get("user_message") or "").strip()
    if message:
        return message[:240]
    clause = str(item.get("source_clause") or item.get("line") or item.get("raw_text") or "").strip()
    reason = _block_reason_zh(str(item.get("reason") or "unmapped"))
    return f"{int(case_step)}. × {clause}（{reason}）"[:240]


def preview_lines_from_plans(spk: dict[str, Any]) -> tuple[list[str], list[str]]:
    """只读已经落库的程序图，不调用模型。"""
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
        for step in do_plan.get("steps") or []:
            if isinstance(step, dict):
                steps_parsed.append(_format_plan_line(step, case_step=n))
        for warning in do_plan.get("compile_warnings") or []:
            blocked = warning.get("blocked") if isinstance(warning, dict) and isinstance(warning.get("blocked"), dict) else None
            if blocked:
                steps_parsed.append(_format_block_line(blocked, case_step=n))
        for step in ck_plan.get("checkpoints") or []:
            if isinstance(step, dict):
                expected_parsed.append(_format_plan_line(step, case_step=n))
        for warning in ck_plan.get("compile_warnings") or []:
            blocked = warning.get("blocked") if isinstance(warning, dict) and isinstance(warning.get("blocked"), dict) else None
            if blocked:
                expected_parsed.append(_format_block_line(blocked, case_step=n))
    return steps_parsed[:40], expected_parsed[:40]


def stored_preview_lines(case: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    if not isinstance(case, dict):
        return [], []
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    if not spk:
        return [], []
    return preview_lines_from_plans(spk)


def compile_preview_view(case: dict[str, Any] | None, *, compiled_case: dict[str, Any] | None = None) -> dict[str, Any]:
    """导入预览：把程序图和未映射句编成步骤/预期/备注文案。"""
    if not isinstance(case, dict):
        return {
            "steps_parsed": [],
            "expected_parsed": [],
            "remark": "",
            "ui_automation": {"schema": "mino.ui_automation.v1", "coverable": True, "blocked_by": []},
        }
    from mino_nexus.services.case_step_key_compiler import sync_case_step_program_keys

    synced = compiled_case if compiled_case is not None else sync_case_step_program_keys(dict(case))
    from mino_nexus.services.v5_publication import guard_case_row
    synced = guard_case_row(synced)
    meta = synced.get("meta") if isinstance(synced.get("meta"), dict) else {}
    ua = ui_automation_summary(synced)
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    steps_parsed, expected_parsed = preview_lines_from_plans(spk)
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


def _import_automation_details(case: dict[str, Any], env_doc: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    from mino_nexus.services.case_resource_claim import compile_resource_key_from_precondition

    details: dict[str, list[dict[str, Any]]] = {"pre": [], "op": [], "ex": []}
    automation = ui_automation_summary(case)
    for index, line in enumerate(str(case.get("precondition") or "").splitlines(), 1):
        text = re.sub(r"^\s*\d+[.)、．]\s*", "", line).strip()
        if not text:
            continue
        claim = compile_resource_key_from_precondition(text, env_doc=env_doc, platform=str(case.get("platform") or ""))
        requirements = claim.get("account", {}).get("requirements", {})
        value = re.split(r"[：:]", text, maxsplit=1)[-1].strip()
        coverable = bool(
            claim.get("case_scene", {}).get("required_session") != "any"
            or claim.get("device_app", {}).get("prep")
            or requirements.get("all") or requirements.get("prefer")
            or value in ("无", "无需", "不限", "任意")
        )
        blockers = [item for item in automation.get("blocked_by") or [] if item.get("phase") == "prep" and item.get("source_clause") == text]
        reason = "已映射为登录态、账号筛选或环境准备条件" if coverable else "未映射为可执行的准备条件，需要人工准备或补充资源规则"
        if blockers:
            coverable = False
            reason = str(blockers[0].get("user_message") or blockers[0].get("detail") or reason)
        elif value in ("无", "无需", "不限", "任意"):
            reason = "无额外准备要求"
        details["pre"].append({"step": index, "text": text, "coverable": coverable, "reason": reason})
    bundles = (case.get("meta") or {}).get("step_program_keys") or {}
    for key, bundle in bundles.items():
        for kind, plan_key, items_key, phase in (("op", "do_program_plan", "steps", "do"), ("ex", "check_program_plan", "checkpoints", "check")):
            plan = bundle.get(plan_key) or {}
            blockers = [
                item
                for item in automation.get("blocked_by") or []
                if item.get("phase") == phase
                and str(item.get("case_step")) == str(key)
                and str(item.get("severity") or "blocking") == "blocking"
            ]
            used: set[int] = set()
            for item in plan.get(items_key) or []:
                text = str(item.get("source_clause") or item.get("source_line") or item.get("title") or "")
                item_kind = str(item.get("kind") or "")
                key_ref = str(item.get("key_ref") or "")
                run_policy = str(item.get("run_policy") or "")
                assert_payload = item.get("assert") if isinstance(item.get("assert"), dict) else {}
                if item_kind == "remark" or key_ref == "operation.author_remark":
                    details[kind].append(
                        {"step": int(key), "text": text, "coverable": True, "reason": "说明步骤，执行时自动跳过"}
                    )
                    continue
                if key_ref == "expected.human_review" or str(assert_payload.get("mode") or "") == "human_only":
                    details[kind].append(
                        {"step": int(key), "text": text, "coverable": True, "reason": "需人工复核，不阻断自动跑批"}
                    )
                    continue
                if run_policy == "skip" and item_kind != "checkpoint":
                    details[kind].append(
                        {"step": int(key), "text": text, "coverable": True, "reason": "执行策略为跳过，不阻断跑批"}
                    )
                    continue
                matches = [(index, blocked) for index, blocked in enumerate(blockers) if not blocked.get("source_clause") or blocked.get("source_clause") in text or text in str(blocked.get("source_clause"))]
                used.update(index for index, _blocked in matches)
                reason = _format_plan_line(item, case_step=int(key)).split(". ", 1)[-1]
                if matches:
                    reason = "；".join(str(blocked.get("user_message") or blocked.get("detail") or _block_reason_zh(blocked.get("reason"))) for _index, blocked in matches)
                details[kind].append({"step": int(key), "text": text, "coverable": not matches, "reason": reason})
            for index, blocked in enumerate(blockers):
                if index not in used:
                    details[kind].append({"step": int(key), "text": str(blocked.get("source_clause") or plan.get("source") or ""), "coverable": False, "reason": str(blocked.get("user_message") or blocked.get("detail") or _block_reason_zh(blocked.get("reason")))})
    return details


def import_row_key_summary(row: dict[str, Any], *, project_id: str, app_id: str = "", env_doc: dict[str, Any] | None = None) -> dict[str, Any]:
    case_like = {
        **row,
        "project_id": project_id,
        "app_id": app_id or row.get("app_id") or "",
        "precondition": row.get("precondition") or "",
        "steps": row.get("steps") or [],
        "expected": row.get("expected") or [],
        "steps_raw": row.get("steps_raw") or "",
        "expected_raw": row.get("expected_raw") or "",
    }
    synced = sync_case_step_program_keys(case_like)
    v = validate_case_step_keys(case_like, app_id=app_id, compiled_case=synced)
    view = compile_preview_view(case_like, compiled_case=synced)
    ua = view.get("ui_automation") or {}
    details = _import_automation_details(synced, env_doc)
    for item in details["pre"]:
        if not item["coverable"]:
            ua["coverable"] = False
            ua.setdefault("blocked_by", []).append({"phase": "prep", "case_step": 0, "source_clause": item["text"], "reason": "unmapped", "user_message": item["reason"]})
    synced["meta"]["ui_automation"] = ua
    blocked = [x for x in (ua.get("blocked_by") or []) if isinstance(x, dict)]
    blocking_count = sum(1 for x in blocked if str(x.get("severity") or "blocking") == "blocking")
    advisory_count = sum(
        1 for x in blocked if str(x.get("severity") or "") in ("human_only", "advisory")
    )
    return {
        "telemetry": "red" if v.get("issues") or not ua.get("coverable", True) else "green",
        "fallback_count": v.get("fallback_count"),
        "unknown_block_count": v.get("unknown_block_count"),
        "unknown_screen_count": v.get("unknown_screen_count"),
        "blocking_issue_count": blocking_count,
        "advisory_issue_count": advisory_count,
        "issues": (v.get("issues") or [])[:24],
        "ui_automation": ua,
        "coverable": ua.get("coverable") is not False,
        "steps_parsed": list(view.get("steps_parsed") or []),
        "expected_parsed": list(view.get("expected_parsed") or []),
        "remark": str(view.get("remark") or ""),
        "compiled_meta": synced.get("meta") or {},
        "automation_details": details,
    }
