"""协作扩展：审批、用例正式发布、知识图谱、远程市场、AgentScope 编排。"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from mino_nexus.services.mino_bots import _now, _require_access, _uid

APPROVAL_ACTIONS = frozenset({"v5_publish", "apply_drafts"})


def _nid(prefix: str = "ap") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def merge_marketplace_items(local: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remote = _fetch_remote_marketplace()
    if not remote:
        return local
    seen = {str(x.get("id") or "") for x in local}
    out = list(local)
    for row in remote:
        rid = str(row.get("id") or "").strip()
        if not rid or rid in seen:
            continue
        row = dict(row)
        row["source"] = "remote"
        out.append(row)
        seen.add(rid)
    return out


def _fetch_remote_marketplace() -> list[dict[str, Any]]:
    url = str(os.environ.get("MINO_COLLAB_MARKETPLACE_URL") or "").strip()
    if not url:
        return []
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for row in items:
        if isinstance(row, dict) and str(row.get("id") or row.get("template_id") or "").strip():
            out.append(row)
    return out


def ensure_work_item_approval(
    db,
    *,
    work_item_id: str,
    topic_id: str,
    action_kind: str,
    payload: dict[str, Any],
    requester_id: str,
) -> None:
    from mino_nexus.models.collab_approval import CollabApproval

    action = str(action_kind or "")
    need = action in APPROVAL_ACTIONS and bool(payload.get("requires_approval", action == "v5_publish"))
    if not need:
        return
    existing = (
        db.query(CollabApproval)
        .filter_by(work_item_id=work_item_id, step="maintainer")
        .order_by(CollabApproval.created_at.desc())
        .first()
    )
    if existing is not None:
        return
    now = _now()
    db.add(
        CollabApproval(
            approval_id=_nid(),
            work_item_id=work_item_id,
            topic_id=topic_id,
            step="maintainer",
            approver_user_id="",
            status="pending",
            comment=f"requested_by:{requester_id}",
            created_at=now,
            decided_at="",
        )
    )


def list_work_item_approvals(project_id: str, work_item_id: str, sess: dict[str, Any]) -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.collab_approval import CollabApproval
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

    _require_access(project_id, sess)
    with session_scope() as db:
        wi = db.get(CollabWorkItem, work_item_id)
        if wi is None:
            raise KeyError("work_item_not_found")
        topic = db.get(CollabTopic, wi.topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        rows = db.query(CollabApproval).filter_by(work_item_id=work_item_id).order_by(CollabApproval.created_at).all()
        return [_approval_public(r) for r in rows]


def decide_work_item_approval(
    project_id: str,
    work_item_id: str,
    approval_id: str,
    sess: dict[str, Any],
    *,
    decision: str,
    comment: str = "",
) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.collab_approval import CollabApproval
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

    _require_access(project_id, sess)
    uid = _uid(sess)
    dec = str(decision or "").strip().lower()
    if dec not in ("approve", "reject"):
        raise ValueError("invalid_decision")
    now = _now()
    with session_scope() as db:
        wi = db.get(CollabWorkItem, work_item_id)
        if wi is None:
            raise KeyError("work_item_not_found")
        topic = db.get(CollabTopic, wi.topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        row = db.get(CollabApproval, approval_id)
        if row is None or row.work_item_id != work_item_id:
            raise KeyError("approval_not_found")
        if str(row.status or "") != "pending":
            raise ValueError("approval_already_decided")
        holder = str(wi.holder_user_id or "").strip()
        if holder and holder == uid and dec == "approve":
            raise PermissionError("cannot_self_approve")
        row.status = "approved" if dec == "approve" else "rejected"
        row.approver_user_id = uid
        row.comment = str(comment or "").strip()
        row.decided_at = now
        wi.updated_at = now
        db.flush()
        return _approval_public(row)


def approvals_satisfied(work_item_id: str) -> tuple[bool, str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.collab_approval import CollabApproval

    with session_scope() as db:
        rows = db.query(CollabApproval).filter_by(work_item_id=work_item_id).all()
        if not rows:
            return True, ""
        for row in rows:
            st = str(row.status or "")
            if st == "rejected":
                return False, "approval_rejected"
            if st != "approved":
                return False, "approval_pending"
        return True, ""


def v5_publish_spec_from_topic(topic) -> dict[str, Any] | None:
    anchor = topic.anchor if isinstance(topic.anchor, dict) else {}
    if str(anchor.get("kind") or "") != "case":
        return None
    case_id = str(anchor.get("case_id") or "").strip()
    if not case_id:
        return None
    from mino_nexus.services.case_store import get_case

    case = get_case(str(topic.project_id or ""), case_id)
    if not case:
        return None
    if str(case.get("platform") or "").lower() != "web":
        return None
    if not int(case.get("v5_draft_revision") or 0):
        return None
    title = str(anchor.get("case_title") or case.get("name") or case_id)
    rev = int(case.get("v5_draft_revision") or 1)
    job_id = str(case.get("latest_compile_job_id") or "")
    return {
        "title": "正式发布用例",
        "goal": f"编译与评审通过后冻结并发布用例：{title}",
        "acceptance": "用例已发布，可参与执行绑定与跑批",
        "action_kind": "v5_publish",
        "action_payload": {
            "case_id": case_id,
            "expected_revision": rev,
            "expected_compile_job_id": job_id,
            "requires_approval": True,
            "reviewed_expected_strength": False,
        },
    }


def trigger_v5_publish(project_id: str, topic, wi, payload: dict[str, Any], *, confirm: bool) -> dict[str, Any]:
    from mino_nexus.services.collab_publish import run_publish_case

    case_id = str(payload.get("case_id") or "").strip()
    if not case_id:
        anchor = topic.anchor if isinstance(topic.anchor, dict) else {}
        case_id = str(anchor.get("case_id") or "").strip()
    pl = {**payload, "confirm": confirm}
    return run_publish_case(project_id, case_id, pl, auto=False, work_item_id=wi.work_item_id)


def refresh_topic_knowledge(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic
    from mino_nexus.services import app_intel
    from mino_nexus.services.mino_bots import _discussion_text
    from mino_nexus.services.qa_cover import run_llm_job

    _require_access(project_id, sess)
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        app_id = str(topic.app_id or "").strip()
        discussion = _discussion_text(db, topic_id, limit=30)
        goal = str(topic.goal or "")
        memory = dict(topic.memory or {}) if isinstance(topic.memory, dict) else {}
    qa_process: dict[str, Any] = {}
    if app_id:
        from mino_nexus.services import app_automation as aas
        from mino_nexus.services import project_store as ps

        try:
            app = ps.require_app(app_id)
            cfg = aas.get_automation_config(app)
            qa_process = dict(cfg.get("qa_process") or {})
        except Exception:
            qa_process = {}
    req = {
        "id": topic_id,
        "title": goal[:120] or "协作议题",
        "understanding": {"summary": memory.get("summary") or discussion[:2000]},
    }
    art = run_llm_job(
        "propose_atlas",
        requirement=req,
        cases=[],
        qa_process=qa_process,
    )
    proposal_ref = None
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    if app_id and art.get("status") == "ok" and payload:
        prop = app_intel.create_proposal(
            app_id=app_id,
            kind="atlas_patch",
            payload={
                "topic_id": topic_id,
                "project_id": project_id,
                "propose_atlas": payload,
            },
            note=f"协作议题知识刷新 {topic_id}",
        )
        if prop.get("ok"):
            proposal_ref = prop.get("item")
    graph = {
        "updated_at": _now(),
        "propose_atlas": payload,
        "llm_status": art.get("status"),
        "llm_error": art.get("error"),
        "app_intel_proposal": proposal_ref,
    }
    now = _now()
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None:
            raise KeyError("topic_not_found")
        topic.knowledge_graph = graph
        topic.updated_at = now
        db.flush()
    return {"topic_id": topic_id, "knowledge_graph": graph}


def run_agentscope_pipeline(
    *,
    topic_id: str,
    topic_title: str,
    goal: str,
    discussion: str,
    memory: str,
    app_id: str,
) -> dict[str, Any]:
    """AgentScope 多角色顺序管线；不可用时回退 LangGraph。"""
    try:
        from mino_nexus.services.collab_agentscope import run_delivery_agentscope

        return run_delivery_agentscope(
            topic_id=topic_id,
            topic_title=topic_title,
            goal=goal,
            discussion=discussion,
            memory=memory,
            app_id=app_id,
        )
    except ImportError:
        from mino_nexus.services.collab_langgraph import run_delivery_pipeline

        out = run_delivery_pipeline(
            topic_id=topic_id,
            topic_title=topic_title,
            goal=goal,
            discussion=discussion,
            memory=memory,
            app_id=app_id,
        )
        trace = list(out.get("pipeline_trace") or [])
        trace.append({"node": "orchestrator", "status": "fallback", "detail": "agentscope_unavailable"})
        out["pipeline_trace"] = trace
        out["engine"] = "langgraph_fallback"
        return out


def _approval_public(row) -> dict[str, Any]:
    return {
        "approval_id": row.approval_id,
        "work_item_id": row.work_item_id,
        "topic_id": row.topic_id,
        "step": row.step or "",
        "approver_user_id": row.approver_user_id or "",
        "status": row.status or "pending",
        "comment": row.comment or "",
        "created_at": row.created_at or "",
        "decided_at": row.decided_at or "",
    }
