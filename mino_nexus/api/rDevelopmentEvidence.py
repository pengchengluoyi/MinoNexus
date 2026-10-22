"""Read only the image already attached to an owned session event."""
import base64
import binascii
from fastapi import APIRouter, Depends, HTTPException, Query
from mino_nexus.core.http_util import ok
from mino_nexus.core.database import session_scope
from mino_nexus.models.session_event import SessionMeta, SessionEvent
from mino_nexus.api.deps import current_session

router = APIRouter(prefix="/case-runner", tags=["DevelopmentEvidence"])


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
