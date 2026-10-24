"""Read-only eligibility checks, split by *why* a case cannot run.

预编译保留。某几步或断言无法自动化时，用例仍然进入执行，这些步骤记在 risks 里。
``needs_setup`` 只表示前置或数据还没准备好。编译产物缺失、场景无效、绑定合同
过期仍然不创建执行。
"""
from __future__ import annotations

from typing import Any

_STATUSES = {"runnable", "needs_setup", "partial", "blocked"}
_CAPABILITY_REASONS = {"unavailable_cap", "uncoverable_event", "platform_unsupported", "capability_disabled"}


def _block_class(item: dict[str, Any]) -> str:
    phase = str(item.get("phase") or "")
    reason = str(item.get("reason") or "")
    if phase == "prep":
        return "setup"
    if phase == "check":
        return "oracle"
    if reason in _CAPABILITY_REASONS:
        return "operation"
    return "source"


def _normalise_blocker(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    row["class"] = str(row.get("class") or _block_class(row))
    if row["class"] == "setup":
        row["severity"] = "degraded"
    elif str(row.get("reason") or "") in _CAPABILITY_REASONS:
        # 兼容旧编译产物：能力缺口即使存成 blocking，预检也只当风险，不再拦整案。
        row["severity"] = "risk"
    else:
        row["severity"] = str(row.get("severity") or "blocking")
    from mino_nexus.contract.attribution import classify_reason

    code = str(row.get("reason") or row.get("reason_code") or "unclassified")
    got = classify_reason(code, layer="preflight")
    row.setdefault("reason_code", code)
    row.setdefault("layer", "preflight")
    row.setdefault("attribution_class", got["attribution_class"])
    row.setdefault("attribution_label", got["attribution_label"])
    return row


def _entry(case: dict[str, Any], *, status: str, klass: str,
           reasons: list[str], blocked_by: list[dict[str, Any]]) -> dict[str, Any]:
    from mino_nexus.contract.attribution import CLASS_LABELS, merge_classes

    normalized = [_normalise_blocker(item) for item in blocked_by if isinstance(item, dict)]
    classes = merge_classes(normalized)
    return {
        "case_id": str(case.get("case_id") or ""),
        "name": str(case.get("name") or ""),
        "status": status,
        "class": klass,
        "reasons": list(dict.fromkeys(str(reason) for reason in reasons if str(reason).strip())) or [],
        "blocked_by": normalized,
        "attribution_classes": classes,
        "attribution_label": "、".join(CLASS_LABELS.get(code, code) for code in classes),
    }


def _message(item: dict[str, Any]) -> str:
    return str(item.get("user_message") or item.get("detail") or item.get("reason") or "").strip()


def _setup_items(automation: dict[str, Any], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        row = dict(issue)
        source = str(row.get("source_clause") or "")
        if source and source in seen:
            continue
        seen.add(source)
        items.append(row)
    setup_doc = automation.get("setup") if isinstance(automation.get("setup"), dict) else {}
    candidates = list(setup_doc.get("items") or [])
    candidates += [item for item in automation.get("blocked_by") or []
                   if isinstance(item, dict) and str(item.get("phase") or "") == "prep"]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        row = _normalise_blocker(item)
        source = str(row.get("source_clause") or "")
        if source and source in seen:
            continue
        seen.add(source)
        items.append(row)
    return items


def _ui_items(automation: dict[str, Any], operation_issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [_normalise_blocker(item) for item in automation.get("blocked_by") or []
             if isinstance(item, dict)]
    items = [item for item in items
             if str(item.get("class") or "") in {"operation", "source", "oracle"}
             and str(item.get("severity") or "blocking") == "blocking"]
    seen = {(str(item.get("source_clause") or ""), str(item.get("reason") or "")) for item in items}
    for issue in operation_issues:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("severity") or "blocking") != "blocking":
            continue
        key = (str(issue.get("source_clause") or ""), str(issue.get("reason") or ""))
        if key in seen:
            continue
        seen.add(key)
        items.append(dict(issue))
    return items


def _legacy_status(automation: dict[str, Any]) -> str:
    if automation.get("coverable") is True:
        return "runnable"
    blockers = [item for item in automation.get("blocked_by") or [] if isinstance(item, dict)]
    if any(str(item.get("phase") or "") in {"do", "check"} for item in blockers):
        return "partial"
    if any(str(item.get("phase") or "") == "prep" for item in blockers):
        return "needs_setup"
    return "blocked"


def partition_cases(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    runnable: list[dict[str, Any]] = []
    needs_setup: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    hard_blocked: list[dict[str, Any]] = []
    verdicts: dict[str, dict[str, Any]] = {}
    for case in cases:
        case_id = str(case.get("case_id") or "")
        from mino_nexus.casework.case_compilation import readiness

        compilation_issue = readiness(case)
        handoff: list[dict[str, Any]] = []
        if compilation_issue:
            handoff.append({"reason": compilation_issue, "class": "infra", "severity": "risk",
                            "handoff": "intent", "job_id": case.get("latest_compile_job_id"),
                            "user_message": "编译产物尚未就绪或与原文不一致，执行时交给意图识别。"})

        from mino_nexus.casework.case_semantic_integrity import (
            operation_diagnostics,
            precondition_diagnostics,
        )
        operation_issues = operation_diagnostics(case)
        meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
        refs = []
        for bundle in (meta.get("step_program_keys") or {}).values():
            for plan_key in ("do_program_plan", "check_program_plan"):
                plan = bundle.get(plan_key) if isinstance(bundle, dict) else None
                for step in (plan or {}).get("steps") or []:
                    bid = str((step or {}).get("block_id") or "").strip()
                    if bid and bid not in refs:
                        refs.append(bid)
        if refs:
            from mino_nexus.catalog.flow_block_seed import verify_referenced_blocks
            for bid in verify_referenced_blocks(refs, app_id=str(case.get("app_id") or case.get("project_id") or "")):
                operation_issues.append({"reason": "missing_ref", "reason_code": "missing_ref",
                                         "class": "operation", "severity": "risk",
                                         "attribution_class": "B", "layer": "preflight",
                                         "user_message": f"用例引用的块 {bid} 没有可执行步骤。"})
        precondition_issues = precondition_diagnostics(case)
        source_precondition = [item for item in precondition_issues
                               if str(item.get("class") or "setup") != "setup"]
        setup_issues = [item for item in precondition_issues
                        if str(item.get("class") or "setup") == "setup"]

        from mino_nexus.runtime.session_gate import case_scene_reference
        ref = case_scene_reference(case.get("case_scene") if isinstance(case.get("case_scene"), dict) else None)
        ref_notes = list(ref.get("reference_notes") or [])
        if ref_notes:
            handoff.append({"reason": "; ".join(ref_notes), "class": "source", "severity": "info",
                            "field": "case_scene", "handoff": "reference",
                            "user_message": "场景字段已降级为执行参考，不再拒跑。"})

        from mino_nexus.casework.execution_requirements import ordinary_compilation_diagnostics
        binding_issues = ordinary_compilation_diagnostics(case)
        for item in binding_issues:
            handoff.append({**item, "class": item.get("class") or "infra",
                            "severity": "risk", "handoff": "intent",
                            "user_message": str(item.get("user_message") or item.get("reason") or "绑定合同过期，执行时继续。")})

        meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
        automation = meta.get("ui_automation") if isinstance(meta.get("ui_automation"), dict) else {}
        setup_items = _setup_items(automation, setup_issues)
        ui_items = _ui_items(automation, operation_issues)
        runnable.append(case)
        risks = [item for item in (automation.get("coverage") or {}).get("risk_items") or []
                 if isinstance(item, dict)]
        if not risks:
            risks = [item for item in (_normalise_blocker(row) for row in automation.get("blocked_by") or []
                                        if isinstance(row, dict))
                     if str(item.get("severity") or "") == "risk"]
        for item in [*ui_items, *source_precondition, *setup_items, *handoff]:
            if item not in risks:
                tagged = dict(item)
                if item in setup_items:
                    tagged["handoff"] = "resource"
                elif "handoff" not in tagged:
                    tagged["handoff"] = "intent"
                risks.append(tagged)
        verdicts[case_id] = {"case_id": case_id, "name": str(case.get("name") or ""),
                             "status": "runnable", "class": "ui",
                             "reasons": [], "blocked_by": [],
                             "risks": risks, "risk_count": len(risks),
                             "attribution_classes": sorted({str(item.get("attribution_class") or "B")
                                                            for item in risks if item.get("attribution_class")})}

    blocked = [*needs_setup, *hard_blocked]
    report = {
        "runnable": [str(case.get("case_id") or "") for case in runnable],
        "needs_setup": needs_setup,
        "partial": partial,
        "hard_blocked": hard_blocked,
        # Backward-compatible meaning: every case that must not enter this run.
        "blocked": blocked,
        "requested_total": len(cases),
        "buckets": {
            "runnable": len(runnable),
            "needs_setup": len(needs_setup),
            "partial": len(partial),
            "blocked": len(hard_blocked),
        },
        "verdicts": verdicts,
    }
    return runnable, report


def prepare_cases_for_execution(project_id: str, case_ids: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from mino_nexus.casework.case_compilation import ensure_execution_compilation

    cases, jobs = ensure_execution_compilation(project_id, case_ids)
    runnable, report = partition_cases(cases)
    report["compilation_jobs"] = jobs
    pending = {job["case_id"]: job for job in jobs}
    for blocked in report["blocked"]:
        job = pending.get(blocked["case_id"])
        if job:
            blocked["reasons"] = ["编译产物尚未就绪，已排队重编或复用在途任务；请编译完成后重新执行。"]
            blocked["compilation_job"] = job
    return runnable, report


def preflight(app: dict[str, Any], case_ids: list[str], *, recompile_stale: bool = False) -> dict[str, Any]:
    if recompile_stale:
        return prepare_cases_for_execution(str(app.get("project_id") or ""), case_ids)[1]
    from mino_nexus.casework.case_store import list_cases

    available = {str(case.get("case_id")): case for case in list_cases(str(app.get("project_id") or ""))}
    if not case_ids or any(str(case_id) not in available for case_id in case_ids):
        raise ValueError("所选用例为空、已删除或不属于当前项目；本次未创建任务。")
    return partition_cases([available[str(case_id)] for case_id in dict.fromkeys(case_ids)])[1]


def rejection_message(report: dict[str, Any]) -> str:
    pending = report.get("compilation_jobs") or []
    buckets = report.get("buckets") or {}
    parts: list[str] = []
    if pending:
        parts.append(f"{len(pending)} 条用例正在编译或已排队重编")
    if buckets.get("needs_setup"):
        parts.append(f"{buckets['needs_setup']} 条用例 UI 可执行，但前置/数据未准备")
    if buckets.get("partial"):
        parts.append(f"{buckets['partial']} 条用例仅部分步骤/断言可 UI 自动化")
    if buckets.get("blocked"):
        parts.append(f"{buckets['blocked']} 条用例定义或编译状态无效")
    if not parts:
        parts.append("所选用例均未通过执行预检")
    return "；".join(parts) + "；本次未创建任务。\n" + blocked_message(report.get("blocked") or [])


def blocked_message(blocked: list[dict[str, Any]]) -> str:
    lines = []
    labels = {"needs_setup": "待准备", "partial": "部分可执行", "blocked": "阻断"}
    for case in blocked:
        label = labels.get(str(case.get("status") or ""), "")
        prefix = f"[{label}] " if label else ""
        lines.append(f"{prefix}{case.get('name') or '未命名用例'}（{case.get('case_id') or '未知编号'}）")
        lines.extend("- " + str(reason) for reason in case.get("reasons") or ["请在 Workbench 查看编译预览。"])
    return "\n".join(lines)
