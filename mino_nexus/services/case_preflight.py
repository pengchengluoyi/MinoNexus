"""提交前只读检查用例可覆盖性；不编译、不租资源、不创建任务。"""
from __future__ import annotations

from typing import Any


def partition_cases(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from mino_nexus.services.case_clause_events import user_message

    runnable, blocked = [], []
    for case in cases:
        from mino_nexus.services.case_compilation import readiness
        compilation_issue = readiness(case)
        if compilation_issue:
            blocked.append({"case_id": str(case.get("case_id") or ""), "name": str(case.get("name") or ""),
                "reasons": ["最新原文的编译任务尚未形成有效产物，请在用例详情处理编译状态。"],
                "blocked_by": [{"reason": compilation_issue, "job_id": case.get("latest_compile_job_id")}]})
            continue
        from mino_nexus.services.case_semantic_integrity import operation_diagnostics, precondition_diagnostics
        semantic_issues = operation_diagnostics(case) + precondition_diagnostics(case)
        if semantic_issues:
            blocked.append({"case_id": str(case.get("case_id") or ""), "name": str(case.get("name") or ""),
                "reasons": [item["user_message"] for item in semantic_issues], "blocked_by": semantic_issues})
            continue
        from mino_nexus.runtime.session_gate import clamp_case_scene
        try:
            clamp_case_scene(case.get("case_scene"))
        except ValueError as exc:
            blocked.append({"case_id": str(case.get("case_id") or ""), "name": str(case.get("name") or ""),
                "reasons": ["用例场景缺失或无效，请重新编译当前用例；本条未创建执行、未占用资源。"],
                "blocked_by": [{"reason": str(exc), "field": "case_scene"}]})
            continue
        meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
        automation = meta.get("ui_automation") if isinstance(meta.get("ui_automation"), dict) else {}
        from mino_nexus.services.execution_requirements import ordinary_compilation_diagnostics
        binding_issues = ordinary_compilation_diagnostics(case)
        if binding_issues:
            blocked.append({"case_id": str(case.get("case_id") or ""), "name": str(case.get("name") or ""),
                "reasons": list(dict.fromkeys(item["user_message"] for item in binding_issues)),
                "blocked_by": binding_issues})
            continue
        blocking_items = [
            item
            for item in (automation.get("blocked_by") or [])
            if isinstance(item, dict) and str(item.get("severity") or "blocking") == "blocking"
        ]
        if automation.get("coverable") is not False and not blocking_items:
            runnable.append(case)
            continue
        reasons, diagnostics = [], []
        for item in blocking_items:
            phase = str(item.get("phase") or "")
            reason = str(item.get("reason") or "unmapped")
            message = user_message(
                case_step=int(item.get("case_step") or 0),
                layer={"prep": "precondition", "do": "operation", "check": "expected"}.get(phase, "operation"),
                clause=str(item.get("source_clause") or ""),
                reason=reason,
                event_name=str(item.get("event_name") or ""),
                detail=str(item.get("detail") or ""),
            )
            if reason != "uncovered" and item.get("user_message"):
                message = str(item["user_message"])
            diagnostics.append({**item, "user_message": message})
            if message not in reasons:
                reasons.append(message)
        if not reasons:
            reasons = ["该用例尚未完整编译为可执行事件，请在 Workbench 用例页查看编译预览。"]
        blocked.append({"case_id": str(case.get("case_id") or ""), "name": str(case.get("name") or ""),
                        "reasons": reasons, "blocked_by": diagnostics})
    return runnable, {"runnable": [str(case.get("case_id") or "") for case in runnable],
                      "blocked": blocked, "requested_total": len(cases)}


def preflight(app: dict[str, Any], case_ids: list[str]) -> dict[str, Any]:
    from mino_nexus.services.case_store import list_cases

    available = {str(case.get("case_id")): case for case in list_cases(str(app.get("project_id") or ""))}
    if not case_ids or any(str(case_id) not in available for case_id in case_ids):
        raise ValueError("所选用例为空、已删除或不属于当前项目；本次未创建任务。")
    return partition_cases([available[str(case_id)] for case_id in dict.fromkeys(case_ids)])[1]


def blocked_message(blocked: list[dict[str, Any]]) -> str:
    lines = []
    for case in blocked:
        lines.append(f"{case.get('name') or '未命名用例'}（{case.get('case_id') or '未知编号'}）")
        lines.extend("- " + str(reason) for reason in case.get("reasons") or ["请在 Workbench 查看编译预览。"])
    return "\n".join(lines)
