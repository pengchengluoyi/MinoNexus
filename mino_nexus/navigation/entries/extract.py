"""从用例步骤与被动采集里抽取入口证据。

两条来源合到同一张 `nav_page_entries`：
- 用例：用例写了“点击 X / 进入 Y / 点击 X 后跳转到 Y”，即使还没跑过，
  也能先得到一条可用的入口；写不清楚的只记缺口，不猜。
- 采集：Atlas 里已经观测到的 `from -> to` 跳转边，带选择器和目标页指纹。

没有 Gherkin/Playwright 导入导出，也没有人工审核。抽取完直接 active。
"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.knowledge.page_semantics import analyze_clause
from mino_nexus.navigation.entries import store
from mino_nexus.navigation.entries.fingerprint import page_fingerprint
from mino_nexus.navigation.entries.text import (
    labels_for_page,
    parse_numbered_items,
    parse_step,
)

TAG = "NavEntryExtract"


def _platforms(value: Any) -> set[str]:
    if isinstance(value, (list, tuple)):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    return {item.strip().lower() for item in str(value or "").replace("，", ",").split(",") if item.strip()}


def _locators(label: str) -> list[dict[str, str]]:
    """定位器优先级：content-desc > text > contains-text；本层不生成 xpath。"""
    text = str(label or "").strip()
    if not text:
        return []
    return [
        {"by": "content_desc", "value": text},
        {"by": "text", "value": text},
        {"by": "contains_text", "value": text},
    ]


def _trigger(label: str) -> dict[str, Any]:
    text = str(label or "").strip()
    return {
        "kind": "tap",
        "text": text,
        "selector": {"text": text, "content_desc": text},
        "locators": _locators(text),
        "requires_visible": True,
    }


def _target_labels(label: str) -> list[str]:
    labels = labels_for_page(label)
    return [item for item in dict.fromkeys(labels) if item]


def _match_expected(index: int, expected_items: list[tuple[int, str]]) -> str:
    for number, text in expected_items:
        if number == index:
            return text
    if 0 <= index - 1 < len(expected_items):
        return expected_items[index - 1][1]
    return ""


def _field(case: Any, key: str, default: Any = "") -> Any:
    if isinstance(case, dict):
        return case.get(key, default)
    return getattr(case, key, default)


def _case_steps(case: Any) -> list[str]:
    steps = _field(case, "steps", None)
    if isinstance(steps, list) and steps:
        return [str(item.get("text") if isinstance(item, dict) else item) for item in steps]
    raw = str(_field(case, "steps_raw", "") or "")
    return [text for _, text in parse_numbered_items(raw)]


def _case_expected(case: Any) -> list[tuple[int, str]]:
    expected = _field(case, "expected", None)
    if isinstance(expected, list) and expected:
        return parse_numbered_items(expected)
    return parse_numbered_items(str(_field(case, "expected_raw", "") or ""))


def _resolve_state(doc: dict[str, Any] | None, label: str) -> tuple[str, list[str]]:
    if not isinstance(doc, dict) or not label:
        return "", []
    try:
        from mino_nexus.navigation.atlas.nav_state_resolve import resolve_state_fuzzy

        out = resolve_state_fuzzy(doc, label, role="to")
        state_id = str(getattr(out, "state_id", "") or "")
    except Exception:  # noqa: BLE001
        state_id = ""
    if not state_id:
        return "", []
    for state in doc.get("states") or []:
        if isinstance(state, dict) and str(state.get("id") or "") == state_id:
            meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
            labels = _target_labels(str(meta.get("display_name") or label))
            for alias in meta.get("aliases") or []:
                labels.extend(_target_labels(str(alias)))
            return state_id, list(dict.fromkeys([item for item in labels if item]))
    return state_id, _target_labels(label)


def _privacy_before_login(case: Any, target: str) -> dict[str, Any] | None:
    """通用常识：登录页之前可能有首次启动的协议/隐私弹窗。

    这里只产出排序提示，不直接点某个 App 的文案；运行时看到真实的
    协议/隐私弹窗后，才用其中的“同意/接受”按钮做一次有界动作。
    """
    if "登录" not in str(target or ""):
        return None
    text = " ".join(_case_steps(case)) + " " + str(_field(case, "precondition", "") or "")
    if not any(word in text for word in ("首次", "协议", "隐私", "同意", "首次启动")):
        return None
    return {
        "rule_id": "prior.first_launch.privacy_before_login.v1",
        "before": ["privacy_consent"],
        "note": "首次启动时先处理系统/隐私协议弹窗，再进入登录页；只在弹窗真实可见时动作。",
    }


def extract_case_entries(
    *,
    app_id: str,
    project_id: str,
    cases: list[Any],
    platform: str = "",
    doc: dict[str, Any] | None = None,
    actor: str = "",
) -> dict[str, Any]:
    created: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(_field(case, "case_id", "") or "")
        case_platform = str(_field(case, "platform", "") or "")
        if platform and case_platform and case_platform.lower() != platform.lower():
            continue
        steps = _case_steps(case)
        expected_items = _case_expected(case)
        previous_target = ""
        for index, raw_step in enumerate(steps, 1):
            parsed = parse_step(raw_step)
            expected_text = _match_expected(index, expected_items)
            expected_parsed = parse_step(expected_text) if expected_text else {}
            trigger = str(parsed.get("trigger") or "")
            raw_target = str(parsed.get("target") or "")
            if not raw_target and expected_parsed:
                raw_target = str(expected_parsed.get("target") or "")
            target = raw_target
            if raw_target:
                sem = analyze_clause(raw_target)
                if sem.page_label and sem.role in ("page", "nav", "action"):
                    target = sem.page_label
                elif sem.role in ("assertion", "flow"):
                    # “查看…”“放弃支付并返回…”不是页面目标；不生成入口/缺口。
                    target = ""
            from_hint = str(parsed.get("from_hint") or previous_target or "")
            if from_hint:
                from_sem = analyze_clause(from_hint)
                if from_sem.page_label and from_sem.role in ("page", "nav", "action"):
                    from_hint = from_sem.page_label
            from_state_id, _ = _resolve_state(doc, from_hint) if from_hint else ("", [])
            target_state_id, resolved_labels = _resolve_state(doc, target)
            if raw_target and target and raw_target != target:
                # 旧的整句目标缺口在规范短名入口建立后关闭，避免同一页
                # 同时出现“商品详情页”和“已配置新人折扣的商品详情页”。
                try:
                    store.resolve_gaps(app_id, target_label=raw_target)
                except Exception:  # noqa: BLE001
                    pass
            if target:
                previous_target = target
            if not target:
                continue
            # 入口的目标名优先用用例原文，状态 display_name 只用来辅助匹配。
            # 否则会把页面标题里的短文案（如“EditText”“背景尽量干净”）当成页名。
            target_labels = _target_labels(target) or resolved_labels
            if not target_labels:
                continue
            source_ref = f"case:{case_id}:step:{index}"
            quote = str(raw_step or "")[:240]
            if expected_text:
                quote = (quote + " / 预期：" + expected_text)[:600]
            base = {
                "app_id": app_id,
                "project_id": project_id,
                "from_state_id": from_state_id,
                "target_state_id": target_state_id,
                "target_label": target_labels[0],
                "platform": case_platform or platform,
                "source_kind": "case",
                "source_ref": source_ref,
                "quote": quote,
                "confidence": float(parsed.get("confidence") or 0.5),
                "preconditions": {
                    "from_label": from_hint,
                    "requires_visible": True,
                },
                "expect": {
                    "target_label": target_labels,
                    "arrival": {
                        "by": "state_or_fingerprint",
                        "state_id": target_state_id,
                        "labels": target_labels,
                    },
                    "expected_text": expected_text,
                },
                "evidence_data": {
                    "rule_id": str(parsed.get("rule_id") or ""),
                    "expected_rule_id": str(expected_parsed.get("rule_id") or ""),
                    "warnings": list(parsed.get("warnings") or []),
                    "step_index": index,
                    "from_hint": from_hint,
                },
            }
            prior = _privacy_before_login(case, target_labels[0])
            if prior:
                base["preconditions"]["prior"] = prior
            if trigger:
                doc_entry = dict(base)
                doc_entry.update({
                    "trigger": _trigger(trigger),
                    "source_ref": source_ref,
                    "relation": "leads_to",
                    "weight": 0.72 if target_state_id else 0.62,
                })
                created.append(store.upsert_entry(doc_entry, actor=actor))
            else:
                gaps.append(store.record_gap(
                    app_id=app_id,
                    project_id=project_id,
                    target_label=target_labels[0],
                    target_state_id=target_state_id,
                    from_state_id=from_state_id,
                    platform=case_platform or platform,
                    kind="target_only",
                    detail=(
                        "用例只写了目标页，没有写出可点击的入口文案。"
                        "运行时不得在页面上盲点，先补入口或通过采集得到跳转边。"
                    ),
                    sample_ref={"case_id": case_id, "step": index, "quote": quote},
                ))
    return {"app_id": app_id, "entries": created, "gaps": gaps, "entry_count": len(created), "gap_count": len(gaps)}


def _looks_like_state_id(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith(("page.", "state.", "screen.")) or bool(
        re.fullmatch(r"[a-z]+[._-]?[0-9a-f]{8,}", text)
    )


def _state_labels(state: dict[str, Any]) -> list[str]:
    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    raw_name = str(meta.get("display_name") or "").strip()
    labels = _target_labels(raw_name) if raw_name else []
    for alias in meta.get("aliases") or []:
        labels.extend(_target_labels(str(alias)))
    if not labels:
        for text in meta.get("chrome_texts") or []:
            labels.extend(_target_labels(str(text)))
    if not labels:
        raw_id = str(state.get("id") or "")
        if raw_id and not _looks_like_state_id(raw_id):
            labels.extend(_target_labels(raw_id))
    return [item for item in dict.fromkeys(labels) if item]


def _fingerprint_from_refs(
    app_id: str,
    state_id: str,
    refs: list[dict[str, Any]],
    *,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if state_id in cache:
        return cache[state_id]
    from mino_nexus.navigation.atlas import nav_capture_store as capture

    for ref in refs:
        session_id = str(ref.get("session_id") or "")
        turn_id = int(ref.get("turn_id") or 0)
        if not session_id or not turn_id:
            continue
        try:
            turn = capture.read_turn(app_id, session_id, turn_id)
        except Exception:  # noqa: BLE001
            turn = None
        nodes = list((turn or {}).get("nodes") or [])
        if nodes:
            fp = page_fingerprint(nodes, platform=str(ref.get("platform") or ""))
            if fp.get("hash"):
                cache[state_id] = fp
                return fp
    cache[state_id] = {}
    return {}


def extract_capture_entries(
    *,
    app_id: str,
    project_id: str,
    platform: str = "",
    doc: dict[str, Any] | None = None,
    actor: str = "",
) -> dict[str, Any]:
    if not isinstance(doc, dict) or not (doc.get("states") or []):
        return {"app_id": app_id, "entries": [], "gaps": [], "entry_count": 0, "gap_count": 0}
    states = {
        str(state.get("id") or ""): state
        for state in doc.get("states") or []
        if isinstance(state, dict)
    }
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    refs_by_state: dict[str, list[dict[str, Any]]] = {}
    for ref in meta.get("atlas_turn_refs") or []:
        if isinstance(ref, dict):
            refs_by_state.setdefault(str(ref.get("state_id") or ""), []).append(ref)
    fp_cache: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for edge in doc.get("edges") or []:
        if not isinstance(edge, dict) or str(edge.get("kind") or "nav") != "nav":
            continue
        edge_id = str(edge.get("id") or "")
        from_state = str(edge.get("from") or "")
        to_state = str(edge.get("to") or "")
        execute = edge.get("execute") if isinstance(edge.get("execute"), dict) else {}
        steps = [str(item) for item in execute.get("steps") or []]
        key = str(execute.get("key") or "").upper()
        if key in ("BACK", "HOME", "MENU") or any(item in ("press_key", "back", "swipe_direction") for item in steps):
            continue
        selector_text = str(
            execute.get("selector_text")
            or execute.get("text")
            or ""
        ).strip()
        target_state = states.get(to_state) or {}
        target_labels = _state_labels(target_state)
        if not target_labels and not _looks_like_state_id(to_state):
            target_labels = _target_labels(to_state)
        if not target_labels:
            continue
        if not selector_text:
            gaps.append(store.record_gap(
                app_id=app_id,
                project_id=project_id,
                target_label=target_labels[0],
                target_state_id=to_state,
                from_state_id=from_state,
                platform=platform,
                kind="target_only",
                detail=f"采集到了 {from_state} -> {to_state} 的跳转，但没有记录到唯一入口文案。",
                sample_ref={"edge_id": edge_id, "session_id": str((refs_by_state.get(to_state) or [{}])[0].get("session_id") or "")},
            ))
            continue
        target_fp = _fingerprint_from_refs(
            app_id, to_state, refs_by_state.get(to_state) or [], cache=fp_cache,
        )
        entry = store.upsert_entry({
            "app_id": app_id,
            "project_id": project_id,
            "from_state_id": from_state,
            "target_state_id": to_state,
            "target_label": target_labels[0],
            "platform": platform,
            "source_kind": "capture",
            "source_ref": f"atlas-edge:{edge_id}",
            "quote": selector_text,
            "relation": "observed",
            "weight": 0.9 if target_fp else 0.82,
            "confidence": 0.93 if target_fp else 0.84,
            "trigger": _trigger(selector_text),
            "preconditions": {
                "from_state_id": from_state,
                "from_label": ", ".join(_state_labels(states.get(from_state) or {})[:2]),
                "requires_visible": True,
            },
            "expect": {
                "target_label": target_labels,
                "arrival": {
                    "by": "state_or_fingerprint",
                    "state_id": to_state,
                    "labels": target_labels,
                    "fingerprint_hash": str(target_fp.get("hash") or ""),
                },
            },
            "fingerprint": target_fp,
            "evidence_data": {
                "edge_id": edge_id,
                "execute": execute,
                "target_state_id": to_state,
                "rule_id": "capture.atlas.edge.v1",
            },
        }, actor=actor)
        entries.append(entry)
    return {"app_id": app_id, "entries": entries, "gaps": gaps, "entry_count": len(entries), "gap_count": len(gaps)}


def extract_for_app(
    *,
    app_id: str,
    project_id: str,
    platform: str = "",
    case_ids: list[str] | None = None,
    include_cases: bool = True,
    include_capture: bool = True,
    actor: str = "",
) -> dict[str, Any]:
    raise ValueError("logic_write_stopped")
    from mino_nexus.navigation.atlas import nav_fsm_store
    from mino_nexus.navigation.atlas import nav_screen_registry

    doc = nav_fsm_store.read_raw(app_id)
    if not isinstance(doc, dict) and include_capture:
        try:
            doc = nav_screen_registry.atlas_doc_for_navigation(app_id, project_id=project_id)
        except Exception as exc:  # noqa: BLE001
            SLog.w(TAG, f"atlas build failed: {type(exc).__name__}: {exc}")
            doc = None
    case_result = {"entry_count": 0, "gap_count": 0, "entries": [], "gaps": []}
    if include_cases:
        from mino_nexus.core.database import session_scope
        from mino_nexus.models.case import ProjectCase

        with session_scope() as db:
            query = db.query(ProjectCase).filter(ProjectCase.project_id == str(project_id or ""))
            if case_ids:
                query = query.filter(ProjectCase.case_id.in_([str(x) for x in case_ids]))
            rows = query.order_by(ProjectCase.sort_index.asc(), ProjectCase.pk.asc()).all()
            cases = [
                {
                    "case_id": row.case_id,
                    "platform": row.platform,
                    "steps": list(row.steps or []),
                    "expected": list(row.expected or []),
                    "steps_raw": row.steps_raw or "",
                    "expected_raw": row.expected_raw or "",
                    "precondition": row.precondition or "",
                }
                for row in rows
            ]
        case_result = extract_case_entries(
            app_id=app_id,
            project_id=project_id,
            cases=cases,
            platform=platform,
            doc=doc,
            actor=actor,
        )
    capture_result = {"entry_count": 0, "gap_count": 0, "entries": [], "gaps": []}
    if include_capture:
        capture_result = extract_capture_entries(
            app_id=app_id,
            project_id=project_id,
            platform=platform,
            doc=doc,
            actor=actor,
        )
    return {
        "app_id": app_id,
        "project_id": project_id,
        "platform": platform,
        "case": case_result,
        "capture": capture_result,
        "entry_count": int(case_result.get("entry_count") or 0) + int(capture_result.get("entry_count") or 0),
        "gap_count": int(case_result.get("gap_count") or 0) + int(capture_result.get("gap_count") or 0),
    }
