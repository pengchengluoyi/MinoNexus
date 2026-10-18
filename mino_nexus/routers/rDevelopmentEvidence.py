"""Read only the image already attached to an owned session event."""
import base64
import binascii
from fastapi import APIRouter, Depends, HTTPException, Query
from mino_nexus.core.http_util import ok
from mino_nexus.core.database import session_scope
from mino_nexus.models.session_event import SessionMeta, SessionEvent
from mino_nexus.routers.deps import current_session

router = APIRouter(prefix="/case-runner", tags=["DevelopmentEvidence"])


@router.get("/sessions/{session_id:path}/execution-evidence")
def execution_evidence(session_id: str, expected_instance_id: str = Query("", max_length=100),
                       expected_node_id: str = Query("", max_length=100),
                       expected_build_id: str = Query("", max_length=200),
                       expected_nexus_build_id: str = Query("", max_length=200),
                       sess: dict = Depends(current_session)):
    from mino_nexus.core.build_info import instance_id
    from mino_nexus.services.execution_evidence import summarize
    current_instance = instance_id()
    if expected_instance_id and expected_instance_id != current_instance:
        raise HTTPException(409, {"reason_code": "instance_mismatch", "message": "目标 Nexus 实例与预期不符，未读取会话"})
    with session_scope() as db:
        meta = db.get(SessionMeta, session_id)
        if not meta or meta.owner_user_id != str(sess["user_id"]):
            raise HTTPException(404, "会话不可读取；未归档或已删除的测试库不会补造历史证据")
        rows = db.query(SessionEvent).filter_by(session_id=session_id).filter(SessionEvent.type.in_(
            ["session/provenance", "tool/result", "device/action", "device/observation"]
        )).order_by(SessionEvent.seq).limit(10001).all()
        data = summarize({"session_id": meta.session_id, "run_id": meta.run_id, "case_id": meta.case_id,
                          "status": meta.status, "finished_at": meta.finished_at, "verdict": meta.verdict},
                         [{"seq": row.seq, "type": row.type, "payload": row.payload} for row in rows[:10000]],
                         instance_id=current_instance, expected_node_id=expected_node_id,
                         expected_build_id=expected_build_id, expected_nexus_build_id=expected_nexus_build_id,
                         truncated=len(rows) > 10000)
        if not expected_instance_id:
            data["issues"].append({"code": "expected_instance_missing", "message": "尚未指定预期测试实例", "failed": False})
            if data["state"] == "verified":
                data["state"] = "incomplete"
        return ok(data)


@router.get("/sessions/{session_id:path}/screenshot")
def screenshot(session_id: str, seq: int = Query(ge=0), sess: dict = Depends(current_session)):
    with session_scope() as db:
        meta = db.get(SessionMeta, session_id)
        if not meta or meta.owner_user_id != str(sess["user_id"]):
            raise HTTPException(404, "会话不存在")
        event = db.query(SessionEvent).filter_by(session_id=session_id, seq=seq).first()
        if not event:
            raise HTTPException(404, "事件不存在")
        payload = event.payload or {}
        image = str(payload.get("thumb") or payload.get("thumb_ref") or "")
        if image.startswith("/static/"):
            from mino_nexus.core.paths import data_dir
            root = (data_dir() / "uploads").resolve()
            path = (root / image[len("/static/"):]).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                raise HTTPException(404, "截图不可读取")
            raw = path.read_bytes()
        else:
            if not image or len(image) > 6 * 1024 * 1024:
                raise HTTPException(404, "此事件没有可读取的截图，请选择 observe/screen 事件")
            try:
                raw = base64.b64decode(image.split(",", 1)[-1] if image.startswith("data:") else image, validate=True)
            except (ValueError, binascii.Error):
                raise HTTPException(422, "历史截图已截断或编码无效")
        mime = "image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else ""
        if not mime:
            raise HTTPException(422, "截图格式无效")
        return ok({"session_id": session_id, "seq": seq, "source": "session_event", "mimeType": mime,
                   "image_base64": base64.b64encode(raw).decode(), "truncated": False})
