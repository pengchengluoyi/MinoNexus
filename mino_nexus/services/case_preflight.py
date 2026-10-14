"""提交前只读检查用例可覆盖性；不编译、不租资源、不创建任务。"""
from __future__ import annotations

from typing import Any


def partition_cases(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from mino_nexus.services.case_clause_events import user_message

    runnable, blocked = [], []
    for case in cases:
        meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
        automation = meta.get("ui_automation") if isinstance(meta.get("ui_automation"), dict) else {}
        if automation.get("coverable") is True:
            runnable.append(case)
            continue
        reasons, diagnostics = [], []
        for item in automation.get("blocked_by") or []:
            if not isinstance(item, dict):
                continue
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
