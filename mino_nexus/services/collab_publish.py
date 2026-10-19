"""协作议题用例发布（§3.2 publish / P3 auto_publish）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem
from mino_nexus.models.task import Task
from mino_nexus.services.case_store import get_case


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def publish_payload_for_case(project_id: str, case_id: str) -> dict[str, Any]:
    case = get_case(project_id, case_id) or {}
    rev = int(case.get("v5_draft_revision") or 1)
    job_id = str(case.get("latest_compile_job_id") or "")
    return {
        "case_id": case_id,
        "expected_revision": rev,
        "expected_compile_job_id": job_id,
        "reviewed_expected_strength": True,
    }


def run_publish_case(
    project_id: str,
    case_id: str,
    payload: dict[str, Any],
    *,
    auto: bool = False,
    work_item_id: str = "",
) -> dict[str, Any]:
    from mino_nexus.services.case_review import describe
    from mino_nexus.services.collab_extended import approvals_satisfied
    from mino_nexus.services.v5_publication import preview, publish

    cid = str(case_id or payload.get("case_id") or "").strip()
    if not cid:
        return {"status": "error", "reason_code": "case_missing", "message": "未指定 case_id"}
    if not auto:
        ok_ap, reason = approvals_satisfied(work_item_id)
        if not ok_ap:
            return {"status": "blocked", "reason_code": reason, "message": "工作项尚未通过维护者审批"}
        if not bool(payload.get("confirm")):
            return {
                "status": "blocked",
                "reason_code": "confirm_required",
                "message": "正式发布需要确认",
            }
        if not bool(payload.get("reviewed_expected_strength")):
            return {
                "status": "blocked",
                "reason_code": "expected_strength_review_required",
                "message": "需确认预期强度审阅",
            }
    case = get_case(project_id, cid)
    if not case:
        return {"status": "error", "reason_code": "case_missing", "message": "用例不存在"}
    rev = int(payload.get("expected_revision") or case.get("v5_draft_revision") or 1)
    job_id = str(payload.get("expected_compile_job_id") or case.get("latest_compile_job_id") or "")
    review = describe(case)
    pending = [
        r
        for r in (review.get("requirements") or [])
        if str(r.get("review_status") or "") in ("pending_review", "needs_input", "needs_configuration")
    ]
    if pending:
        return {
            "status": "blocked",
            "reason_code": "case_review_pending",
            "message": f"用例仍有 {len(pending)} 项待评审，请先在 CaseReview 处理",
            "pending_count": len(pending),
            "evidence": {
                "case_id": cid,
                "case_review_path": f"/project/{project_id}/cases/{cid}/review-requirements",
                "pending_count": len(pending),
            },
        }
    prev = preview(case)
    if not prev.get("eligible"):
        return {
            "status": "blocked",
            "reason_code": str(prev.get("reason_code") or "v5_ineligible"),
            "message": "发布预检未通过",
            "errors": list(prev.get("errors") or [])[:20],
        }
    try:
        published = publish(
            project_id,
            cid,
            rev,
            retry_policy=payload.get("retry_policy") or {"on": [], "max": 0},
            reversible=bool(payload.get("reversible")),
            expected_compile_job_id=job_id,
        )
    except ValueError as exc:
        return {"status": "blocked", "reason_code": str(exc), "message": str(exc)}
    plan = (published or {}).get("v5_plan_state")
    evidence = {
        "case_id": cid,
        "v5_plan_state": plan,
        "compile_job_id": job_id,
        "expected_revision": rev,
    }
    return {
        "status": "ok",
        "reason_code": "published",
        "message": "用例已正式发布",
        "case_id": cid,
        "v5_plan_state": plan,
        "evidence": evidence,
    }


def augment_pipeline_specs(
    specs: list[dict[str, Any]],
    project_id: str,
    app_id: str,
) -> list[dict[str, Any]]:
    from mino_nexus.services.collab_policy import get_policy

    pol = get_policy(project_id, app_id=app_id)
    if not pol.get("auto_publish"):
        return specs
    out = [dict(s) for s in specs]
    if not any(str(s.get("action_kind") or "") == "compile" for s in out):
        return out
    if any(str(s.get("action_kind") or "") in ("publish", "v5_publish") for s in out):
        return out
    insert_at = 0
    for i, s in enumerate(out):
        if str(s.get("_slot") or "") == "compile" or str(s.get("action_kind") or "") == "compile":
            insert_at = i + 1
            break
    out.insert(
        insert_at,
        {
            "_slot": "publish",
            "title": "正式发布用例",
            "goal": "编译通过后按项目策略自动发布",
            "action_kind": "publish",
            "depends_on_slots": ["compile"],
            "origin": "pipeline",
        },
    )
    for s in out:
        if str(s.get("action_kind") or "") == "case_run":
            s["depends_on_slots"] = ["publish"]
    return out


def enqueue_publish_for_compiled_cases(
    db,
    topic: CollabTopic,
    compile_wi: CollabWorkItem,
    case_ids: list[str],
    *,
    holder_user_id: str,
    input_version: str,
) -> list[str]:
    from mino_nexus.services.mino_bots import _idempotency_key

    created: list[str] = []
    now = _now()
    ver = str(input_version or topic.input_version or "v1")
    for cid in case_ids:
        cid = str(cid).strip()
        if not cid:
            continue
        ikey = _idempotency_key(topic.topic_id, ver, "", f"auto_publish:{cid}:{compile_wi.work_item_id}")
        existing = db.query(CollabWorkItem).filter_by(topic_id=topic.topic_id, idempotency_key=ikey).first()
        if existing is not None:
            created.append(existing.work_item_id)
            continue
        payload = publish_payload_for_case(topic.project_id, cid)
        task_row = Task(
            id=str(uuid.uuid4()),
            app_id=str(topic.app_id or ""),
            name=f"发布 {cid}"[:200],
            task_type="collab_work_item",
            status="pending",
            created_at=now,
        )
        db.add(task_row)
        wi = CollabWorkItem(
            work_item_id=str(uuid.uuid4()),
            topic_id=topic.topic_id,
            task_id=task_row.id,
            title=f"正式发布：{cid}",
            goal="策略开启 auto_publish",
            acceptance="v5_plan_state 写入 evidence",
            assignee_kind="bot",
            holder_user_id=holder_user_id,
            input_version=ver,
            idempotency_key=ikey,
            status="pending",
            action_kind="publish",
            action_payload=payload,
            depends_on=[compile_wi.work_item_id],
            case_ids=[cid],
            origin="auto_publish",
            created_at=now,
            updated_at=now,
        )
        db.add(wi)
        created.append(wi.work_item_id)
    return created
