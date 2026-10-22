"""Read-only eligibility checks, split by *why* a case cannot run.

``runnable`` is deliberately narrow: UI plan complete, setup ready, binding
contract current.  A case whose UI steps and checks compiled but whose business
fixture is missing is ``needs_setup`` -- not a UI capability gap and not an
instruction to recompile the same source again.
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
    else:
        row["severity"] = str(row.get("severity") or "blocking")
    return row


def _entry(case: dict[str, Any], *, status: str, klass: str,
           reasons: list[str], blocked_by: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_id": str(case.get("case_id") or ""),
        "name": str(case.get("name") or ""),
        "status": status,
        "class": klass,
        "reasons": list(dict.fromkeys(str(reason) for reason in reasons if str(reason).strip())) or [],
        "blocked_by": blocked_by,
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
        if compilation_issue:
            entry = _entry(
                case, status="blocked", klass="infra",
                reasons=["最新原文的编译任务尚未形成有效产物，请在用例详情处理编译状态。"],
                blocked_by=[{"reason": compilation_issue, "class": "infra",
                             "severity": "blocking", "job_id": case.get("latest_compile_job_id")}],
            )
            hard_blocked.append(entry)
            verdicts[case_id] = entry
            continue

        from mino_nexus.casework.case_semantic_integrity import (
            operation_diagnostics,
            precondition_diagnostics,
        )
        operation_issues = operation_diagnostics(case)
        precondition_issues = precondition_diagnostics(case)
        source_precondition = [item for item in precondition_issues
                               if str(item.get("class") or "setup") != "setup"]
        setup_issues = [item for item in precondition_issues
                        if str(item.get("class") or "setup") == "setup"]

        from mino_nexus.runtime.session_gate import clamp_case_scene
        try:
            clamp_case_scene(case.get("case_scene"))
        except ValueError as exc:
            entry = _entry(
                case, status="blocked", klass="source",
                reasons=["用例场景缺失或无效，请重新编译当前用例；本条未创建执行、未占用资源。"],
                blocked_by=[{"reason": str(exc), "class": "source",
                             "severity": "blocking", "field": "case_scene"}],
            )
            hard_blocked.append(entry)
            verdicts[case_id] = entry
            continue

        from mino_nexus.casework.execution_requirements import ordinary_compilation_diagnostics
        binding_issues = ordinary_compilation_diagnostics(case)
        if binding_issues:
            entry = _entry(
                case, status="blocked", klass="infra",
                reasons=list(dict.fromkeys(str(item.get("user_message") or item.get("reason") or "")
                                           for item in binding_issues)),
                blocked_by=[{**item, "class": item.get("class") or "infra",
                             "severity": item.get("severity") or "blocking"}
                            for item in binding_issues],
            )
            hard_blocked.append(entry)
            verdicts[case_id] = entry
            continue

        meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
        automation = meta.get("ui_automation") if isinstance(meta.get("ui_automation"), dict) else {}
        status = str(automation.get("status") or "")
        if status not in _STATUSES:
            status = _legacy_status(automation)
        setup_items = _setup_items(automation, setup_issues)
        ui_items = _ui_items(automation, operation_issues)
        if status == "runnable" and setup_items:
            status = "needs_setup"
        if status in {"runnable", "needs_setup"} and ui_items:
            status = "partial"
        if source_precondition:
            no_plan = str(automation.get("ui_plan") or "") in {"", "none"}
            status = "blocked" if status == "blocked" or no_plan else "partial"
        if status == "needs_setup" and source_precondition:
            status = "partial"

        if status == "runnable":
            runnable.append(case)
            verdicts[case_id] = {"case_id": case_id, "name": str(case.get("name") or ""),
                                 "status": "runnable", "class": "ui",
                                 "reasons": [], "blocked_by": []}
            continue

        if status == "needs_setup":
            reasons = [_message(item) for item in setup_items]
            reasons = [reason for reason in reasons if reason]
            if not reasons:
                reasons = ["UI 步骤可执行，但前置/数据尚未准备；准备完成后可重新提交。"]
            entry = _entry(case, status="needs_setup", klass="setup",
                           reasons=reasons, blocked_by=setup_items)
            needs_setup.append(entry)
            verdicts[case_id] = entry
            continue

        if status == "partial":
            items = ui_items + [item for item in setup_items if item not in ui_items]
            items += [item for item in source_precondition if item not in items]
            reasons = [_message(item) for item in items]
            reasons = [reason for reason in reasons if reason]
            if not reasons:
                reasons = ["用例仅部分步骤/断言可 UI 自动化，请拆成 UI 片段或补能力后重新编译。"]
            entry = _entry(case, status="partial", klass="partial",
                           reasons=reasons, blocked_by=items)
            partial.append(entry)
            verdicts[case_id] = entry
            continue

        items = [_normalise_blocker(item) for item in (automation.get("blocked_by") or [])
                 if isinstance(item, dict) and str(item.get("phase") or "") != "prep"]
        items += [item for item in source_precondition if item not in items]
        reasons = [_message(item) for item in items]
        reasons = [reason for reason in reasons if reason]
        if not reasons:
            reasons = ["用例定义或编译状态无效，请在 Workbench 用例详情处理。"]
        entry = _entry(case, status="blocked", klass="blocked",
                       reasons=reasons, blocked_by=items)
        hard_blocked.append(entry)
        verdicts[case_id] = entry

    blocked = [*needs_setup, *partial, *hard_blocked]
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
