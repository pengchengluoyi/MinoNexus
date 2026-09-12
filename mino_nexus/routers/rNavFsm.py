"""NavFSM 配置与校准证据。设计稿 docs/NAVIGATION_ATLAS.md §0.2.3、§10.2。

配置真源在 `mino.db`（`nav_fsm*` 三张表），校准证据在 `data_dir()`。两者都不进 git。

路由前缀按本仓约定用 `/nav-fsm`（本仓 router 一律不带 `/api`）。

请求模型必须留在模块级 —— CLAUDE.md §7.1，函数内定义会让整个 app 起不来。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session
from mino_nexus.services import nav_calibration_store as calib
from mino_nexus.services import nav_capture_store as capture
from mino_nexus.services import nav_candidate_compiler as compiler
from mino_nexus.services import nav_candidates_store as candidates
from mino_nexus.services import nav_fsm_humanize as humanize
from mino_nexus.services import nav_fsm_store as store
from mino_nexus.services import nav_fsm_template as tpl
from mino_nexus.services import nav_live_graph
from mino_nexus.services import nav_route
from mino_nexus.services import nav_telemetry as telemetry

router = APIRouter(prefix="/nav-fsm", tags=["NavFSM"])


class CalibrationStartBody(BaseModel):
    account_id: str = ""
    calibration_id: str = ""
    note: str = ""


class CalibrationStepBody(BaseModel):
    step_key: str = ""
    hierarchy_text: str = ""
    nodes: list[dict[str, Any]] | None = None
    run_id: str = ""
    case_id: str = ""
    turn_id: int = 0
    note: str = ""


class CalibrationFinishBody(BaseModel):
    conclusions: dict[str, Any] = {}


class AnnotateBody(BaseModel):
    session_id: str
    target_seq: int
    verdict: str
    note: str = ""


class DraftBody(BaseModel):
    doc: dict[str, Any] = {}


class NavFsmBody(BaseModel):
    project_id: str = ""
    version: str = "v1"
    meta: dict[str, Any] = {}
    test_data: dict[str, Any] = {}
    states: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []


class FeedbackBody(BaseModel):
    session_id: str
    turn_id: int
    kind: str = "localize_wrong"
    note: str = ""


class CandidateReviewBody(BaseModel):
    status: str = "accepted"
    note: str = ""


class NavRouteBody(BaseModel):
    from_state: str = ""
    to_state: str = ""
    version: str = store.DEFAULT_VERSION
    use_live: bool = True
    project_id: str = ""


@router.get("")
def list_nav_fsm(_sess: dict = Depends(current_session)):
    return ok(store.list_apps())


@router.get("/{app_id}")
def get_nav_fsm(app_id: str, version: str = store.DEFAULT_VERSION, _sess: dict = Depends(current_session)):
    """编辑口：原样返回，附上 runtime 是否会接受它。

    草稿里带 `__CALIBRATE__` 不是错误 —— 那正是等着人去补录的部分（§8.5）。
    """
    doc = store.read_raw(app_id, version=version)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"app_id={app_id} 没有 nav_fsm 配置")
    _, reason = store.load_with_reason(app_id, version=version)
    quality = humanize.assess_nav_fsm_quality(doc)
    return ok(
        {
            **doc,
            "runtime_ready": not reason,
            "runtime_reason": reason,
            "runtime_reason_human": humanize.humanize_runtime_reason(reason),
            "quality": quality,
        }
    )


@router.put("/{app_id}")
def put_nav_fsm(app_id: str, body: NavFsmBody, sess: dict = Depends(current_session)):
    try:
        saved = store.save(
            app_id,
            body.model_dump(),
            updated_by=str(sess.get("username") or sess.get("user_id") or ""),
        )
    except store.NavFsmInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ok(saved)


@router.delete("/{app_id}")
def delete_nav_fsm(app_id: str, version: str = store.DEFAULT_VERSION, _sess: dict = Depends(current_session)):
    return ok({"deleted": store.delete(app_id, version=version)})


@router.post("/{app_id}/route")
def plan_nav_route(app_id: str, body: NavRouteBody, _sess: dict = Depends(current_session)):
    """路线图最短路（与 recovery 能力 fsm_navigate 同一逻辑）。"""
    result = nav_route.plan_route_for_app(
        app_id,
        from_state=body.from_state,
        to_state=body.to_state,
        version=body.version or store.DEFAULT_VERSION,
        use_live=body.use_live,
        project_id=body.project_id,
    )
    return ok(result)


# ---------------- v2.5 被动采集 + 候选（§19） ----------------


@router.get("/{app_id}/captures")
def list_captures(app_id: str, limit: int = 50, _sess: dict = Depends(current_session)):
    return ok(capture.list_sessions(app_id, limit=max(1, min(200, limit))))


@router.get("/{app_id}/captures/{session_id}")
def get_capture_session(app_id: str, session_id: str, _sess: dict = Depends(current_session)):
    row = capture.read_session_index(app_id, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有该 session 的采集")
    return ok(row)


@router.get("/{app_id}/captures/{session_id}/turn/{turn_id}")
def get_capture_turn(app_id: str, session_id: str, turn_id: int, _sess: dict = Depends(current_session)):
    row = capture.read_turn(app_id, session_id, int(turn_id))
    if row is None:
        raise HTTPException(status_code=404, detail="没有该 turn 的采集")
    return ok(row)


@router.get("/{app_id}/captures/{session_id}/turn/{turn_id}/screen")
def get_capture_turn_screen(
    app_id: str,
    session_id: str,
    turn_id: int,
    _sess: dict = Depends(current_session),
):
    path = capture.screen_path(app_id, session_id, int(turn_id))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="该 turn 无截图")
    return FileResponse(path, media_type="image/jpeg")


@router.delete("/{app_id}/captures")
def clear_captures(
    app_id: str,
    also_config: bool = False,
    sess: dict = Depends(current_session),
):
    """清空被动采集；可选同时删除已发布 NavFSM 配置。"""
    result = capture.clear_all(app_id)
    if also_config:
        result["config_deleted"] = store.delete(app_id)
    return ok(result)


@router.get("/{app_id}/live-graph")
def get_live_graph(
    app_id: str,
    sync: bool = True,
    project_id: str = "",
    sess: dict = Depends(current_session),
):
    """实时合成导航图 + 轨迹；默认 sync 写入正式库，UI 无需手点发布。"""
    row = nav_live_graph.get_live_graph(
        app_id,
        project_id=project_id,
        updated_by=str(sess.get("username") or sess.get("user_id") or ""),
        sync=bool(sync),
    )
    return ok(row)


@router.get("/{app_id}/calibration-report")
def get_calibration_report(app_id: str, _sess: dict = Depends(current_session)):
    """v2.5 采集报告（非 walkthrough 批次）。"""
    report = capture.capture_report(app_id)
    batch = candidates.load_batch(app_id)
    report["pending_candidates"] = int(batch.get("pending_count") or 0)
    latest_sessions = capture.list_sessions(app_id, limit=1)
    latest_sid = str((latest_sessions[0] or {}).get("session_id") or "") if latest_sessions else ""
    report["latest_capture_session_id"] = latest_sid
    report["latest_capture_updated_at"] = int((latest_sessions[0] or {}).get("updated_at") or 0) if latest_sessions else 0
    doc = store.read_raw(app_id)
    report["has_formal_config"] = doc is not None
    report["has_draft"] = calib.read_draft(app_id) is not None
    published_turns = 0
    if doc:
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        published_turns = int(meta.get("capture_turns") or 0)
    report["published_capture_turns"] = published_turns
    report["capture_pending_publish"] = int(report.get("turns_captured") or 0) > published_turns
    if doc:
        _, reason = store.load_with_reason(app_id)
    else:
        reason = "尚未配置导航图"
    report["runtime_ready"] = bool(doc) and not bool(reason)
    report["runtime_reason"] = reason or ""
    report["runtime_reason_human"] = humanize.humanize_runtime_reason(reason or "")
    return ok(report)


@router.get("/{app_id}/candidates")
def list_candidates(app_id: str, _sess: dict = Depends(current_session)):
    return ok(compiler.filter_noise_candidates(candidates.load_batch(app_id)))


@router.post("/{app_id}/candidates/compile")
def compile_candidates(app_id: str, _sess: dict = Depends(current_session)):
    return ok(compiler.compile_from_captures(app_id))


@router.post("/{app_id}/candidates/{candidate_id}/review")
def review_candidate(
    app_id: str,
    candidate_id: str,
    body: CandidateReviewBody,
    _sess: dict = Depends(current_session),
):
    try:
        batch = candidates.review_candidate(
            app_id,
            candidate_id,
            status=str(body.status or "pending"),
            note=body.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ok(batch)


@router.post("/{app_id}/candidates/apply-to-draft")
def apply_candidates_to_draft(app_id: str, _sess: dict = Depends(current_session)):
    try:
        merged = compiler.apply_accepted_to_draft(app_id)
        draft = compiler.autofill_draft_from_captures(app_id, merged.get("draft") or {})
        saved = calib.save_draft(app_id, draft)
        pending = tpl.pending_marks(saved)
        return ok(
            {
                **merged,
                "draft": saved,
                "pending": pending,
                "pending_count": len(pending),
                "human_summary": humanize.humanize_pending_list(pending),
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{app_id}/publish")
def publish_nav_fsm(app_id: str, sess: dict = Depends(current_session)):
    """一键发布：自动接受高置信候选、填平草稿、校验后写入正式配置。"""
    try:
        result = compiler.prepare_publish(
            app_id,
            promote=True,
            updated_by=str(sess.get("username") or sess.get("user_id") or ""),
        )
    except store.NavFsmInvalid as exc:
        draft = calib.read_draft(app_id) or {}
        pending = tpl.pending_marks(draft)
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "message_human": humanize.humanize_runtime_reason(str(exc)),
                "pending": pending,
                "human_summary": humanize.humanize_pending_list(pending),
            },
        ) from exc
    if result.get("publish_blocked"):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "仍有待填项，无法发布",
                "message_human": result.get("human_summary") or "仍有待填项",
                "pending": result.get("pending") or [],
                "human_summary": result.get("human_summary") or "",
            },
        )
    result["runtime_reason_human"] = ""
    return ok(result)


@router.post("/{app_id}/feedback")
def nav_feedback(app_id: str, body: FeedbackBody, _sess: dict = Depends(current_session)):
    """失败点反馈 → 定点候选（不写正式库）。"""
    try:
        return ok(
            compiler.compile_from_feedback(
                app_id,
                session_id=body.session_id,
                turn_id=int(body.turn_id),
                kind=body.kind,
                note=body.note,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------- 遗留 walkthrough 批次（只读兼容，新采集走 /captures） ----------------


@router.get("/{app_id}/calibration")
def list_calibration(app_id: str, _sess: dict = Depends(current_session)):
    return ok(calib.list_calibrations(app_id))


@router.get("/{app_id}/calibration/{calibration_id}")
def get_calibration(app_id: str, calibration_id: str, _sess: dict = Depends(current_session)):
    row = calib.read(app_id, calibration_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这份校准证据")
    return ok(row)


@router.get("/{app_id}/calibration/{calibration_id}/step/{index}")
def get_calibration_step(
    app_id: str,
    calibration_id: str,
    index: int,
    _sess: dict = Depends(current_session),
):
    """单步 hierarchy 片段。正文可能很大，按需取，不塞进 §10.2 的响应里。"""
    return ok({"index": index, "hierarchy_text": calib.read_step(app_id, calibration_id, index)})


# ---------------- 空壳模板与草稿（§8.5） ----------------


@router.get("/{app_id}/template")
def get_template(app_id: str, _sess: dict = Depends(current_session)):
    """产一份待校准骨架。**不落库** —— 库里永远不该有 `__CALIBRATE__`（§10.5）。

    拿去当填空题：状态名先按实际界面改，实测值等 walkthrough 采完再填。
    """
    from mino_nexus.services import project_store as ps

    app = ps.find_app(app_id)
    doc = tpl.build_template(app_id, project_id=str((app or {}).get("project_id") or ""))
    return ok({"doc": doc, "pending": tpl.pending_marks(doc)})


@router.get("/{app_id}/draft")
def get_draft(app_id: str, _sess: dict = Depends(current_session)):
    doc = calib.read_draft(app_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="没有草稿")
    return ok({"doc": doc, "pending": tpl.pending_marks(doc)})


@router.put("/{app_id}/draft")
def put_draft(app_id: str, body: DraftBody, _sess: dict = Depends(current_session)):
    """存半成品。允许残留 `__CALIBRATE__`，落在 `data_dir` 而不是库里。"""
    doc = calib.save_draft(app_id, body.doc)
    return ok({"pending": tpl.pending_marks(doc), "saved": True})


@router.delete("/{app_id}/draft")
def delete_draft(app_id: str, _sess: dict = Depends(current_session)):
    return ok({"deleted": calib.delete_draft(app_id)})


@router.post("/{app_id}/draft/promote")
def promote_draft(app_id: str, sess: dict = Depends(current_session)):
    """草稿 → 正式配置。还有占位符没填就 422，并告诉你差哪些字段。"""
    try:
        saved = calib.promote_draft(
            app_id, updated_by=str(sess.get("username") or sess.get("user_id") or "")
        )
    except store.NavFsmInvalid as exc:
        draft = calib.read_draft(app_id) or {}
        pending = tpl.pending_marks(draft)
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "message_human": humanize.humanize_runtime_reason(str(exc)),
                "pending": pending,
                "human_summary": humanize.humanize_pending_list(pending),
            },
        ) from exc
    return ok(saved)


# ---------------- walkthrough 写入（§8.4） ----------------


@router.post("/{app_id}/calibration")
def start_calibration(app_id: str, body: CalibrationStartBody, _sess: dict = Depends(current_session)):
    """【遗留】手动 walkthrough 批次。v2.5 起请用被动采集 `/captures` + `/candidates/compile`。"""
    from mino_nexus.services import project_store as ps

    app = ps.find_app(app_id)
    manifest = calib.start(
        app_id,
        project_id=str((app or {}).get("project_id") or ""),
        account_id=body.account_id,
        calibration_id=body.calibration_id,
        extra={"note": body.note} if body.note else None,
    )
    return ok(manifest)


@router.post("/{app_id}/calibration/{calibration_id}/step")
def append_calibration_step(
    app_id: str,
    calibration_id: str,
    body: CalibrationStepBody,
    _sess: dict = Depends(current_session),
):
    """记一步。**顺序即语义**：中途会改账号状态，跳步补采无效（§8.4.0）。"""
    if calib.read(app_id, calibration_id) is None:
        raise HTTPException(status_code=404, detail="没有这批校准，先 POST /calibration 开批次")
    step = calib.append_step(
        app_id,
        calibration_id,
        step_key=body.step_key,
        hierarchy_text=body.hierarchy_text,
        nodes=body.nodes,
        run_id=body.run_id,
        case_id=body.case_id,
        turn_id=body.turn_id,
        note=body.note,
    )
    return ok(step)


@router.post("/{app_id}/calibration/{calibration_id}/finish")
def finish_calibration(
    app_id: str,
    calibration_id: str,
    body: CalibrationFinishBody,
    _sess: dict = Depends(current_session),
):
    """收工：把比对结论并进 manifest，供人照着填 `nav_fsm*`。"""
    if calib.read(app_id, calibration_id) is None:
        raise HTTPException(status_code=404, detail="没有这批校准")
    return ok(calib.finish(app_id, calibration_id, conclusions=body.conclusions))


# ---------------- 指标与人工标注（§9） ----------------


@router.get("/{app_id}/metrics")
def get_metrics(
    app_id: str,
    run_id: str = "",
    limit: int = 50,
    _sess: dict = Depends(current_session),
):
    """§9 的指标。**指标为 `null` 表示没样本**，不是达标 —— 别把「没抽检过」读成「没问题」。"""
    return ok(telemetry.aggregate_app(app_id, run_id=run_id, limit=limit))


@router.get("/{app_id}/reviews")
def list_reviews(app_id: str, session_id: str = "", limit: int = 50, _sess: dict = Depends(current_session)):
    """待人工标注的 guard 事件。给 Console 的抽检待办列表用。"""
    from mino_nexus.services import session_store

    if session_id:
        return ok({"pending": telemetry.pending_reviews(session_id)})
    sessions, _ = session_store.list_sessions(app_id=app_id, limit=max(1, min(200, limit)))
    rows: list[dict[str, Any]] = []
    for row in sessions:
        rows.extend(telemetry.pending_reviews(str(row.get("session_id") or "")))
    return ok({"pending": rows})


@router.get("/{app_id}/localize-samples")
def get_localize_samples(
    app_id: str,
    session_id: str,
    limit: int = 20,
    _sess: dict = Depends(current_session),
):
    """localize 人工抽检样本。§9 的 `localize_acc` 只能这么算，没有自动判定。"""
    return ok({"samples": telemetry.localize_samples(session_id, limit=limit)})


@router.post("/{app_id}/annotate")
def annotate_guard_event(app_id: str, body: AnnotateBody, sess: dict = Depends(current_session)):
    """QA 把 fp / fn 判断写回 session log（append-only，不改原事件）。"""
    try:
        row = telemetry.annotate(
            body.session_id,
            target_seq=body.target_seq,
            verdict=body.verdict,
            note=body.note,
            by=str(sess.get("username") or sess.get("user_id") or ""),
        )
    except telemetry.AnnotationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ok(row)
