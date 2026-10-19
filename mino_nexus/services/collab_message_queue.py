"""Persistent, serial message replies. Never replay an uncertain started action."""
import asyncio
import threading
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.core.execution_user import execution_user
from mino_nexus.core.log import SLog
from mino_nexus.models.collab_message_job import CollabMessageJob
from mino_nexus.models.mino_bots import CollabMessage

_WAKE = threading.Event()


def wake():
    _WAKE.set()


def recover_interrupted():
    # On restart no unfinished utterance is replayed, including queued messages.
    from mino_nexus.services.mino_bots import _now
    with session_scope() as db:
        db.query(CollabMessageJob).filter(CollabMessageJob.status.in_(("queued", "running"))).update({
            "status": "interrupted", "updated_at": _now(),
            "error": "服务重启，回复处理结果未确认；请先查看任务状态，再决定是否重发。",
        }, synchronize_session=False)


def _notify(project_id, topic_id, message_id):
    from mino_nexus.websocket.observers import broadcast_sync
    try:
        broadcast_sync({"action": "collab_message_updated", "data": {
            "project_id": project_id, "topic_id": topic_id, "message_id": message_id,
        }})
    except Exception as exc:
        SLog.w("CollabMessage", f"notification: {type(exc).__name__}")


def run_one():
    from mino_nexus.services.mino_bots import _now, _require_access
    from mino_nexus.services.auth_store import api_session_for
    from mino_nexus.services.collab_conductor import process as orchestrate_after_user_message
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        job = db.query(CollabMessageJob).filter_by(status="queued").order_by(
            CollabMessageJob.created_at, CollabMessageJob.message_id).first()
        if job is None:
            return False
        message = db.get(CollabMessage, job.message_id)
        if message is None or message.topic_id != job.topic_id:
            job.status, job.error, job.updated_at = "failed", "原消息已不存在。", _now()
            return True
        mid, pid, tid, owner, body = job.message_id, job.project_id, job.topic_id, job.owner_user_id, message.body
        job.status, job.updated_at = "running", _now()
    status, error = "succeeded", ""
    try:
        # Reload the account and permissions; never store or reuse a login token.
        sess = api_session_for(owner)
        if not sess:
            raise PermissionError("发送账号已不可用。")
        with execution_user(owner):
            _require_access(pid, sess)
            _notify(pid, tid, mid)
            orchestrate_after_user_message(pid, tid, sess, body, source_message_id=mid)
    except Exception as exc:
        SLog.w("CollabMessage", f"reply failed: {type(exc).__name__}")
        status, error = "failed", str(exc)[:500]
        from mino_nexus.services import collab_workspace as ws
        with session_scope() as db:
            try:
                topic = ws.owned(db, pid, tid)
                ws.message(db, topic, '本轮处理未完成：' + error + '。消息已保留，未确认的动作不会自动重发。')
            except KeyError:
                pass
    with session_scope() as db:
        job = db.get(CollabMessageJob, mid)
        if job is not None and job.status == "running":
            job.status, job.error, job.updated_at = status, error, _now()
    with execution_user(owner):
        _notify(pid, tid, mid)
    return True


async def worker():
    while True:
        try:
            # Clear before checking durable work so a concurrent wake is not lost.
            _WAKE.clear()
            worked = await asyncio.to_thread(run_one)
            if not worked:
                await asyncio.to_thread(_WAKE.wait, 1.0)
        except Exception as exc:
            SLog.w("CollabMessage", f"worker: {type(exc).__name__}")
            await asyncio.sleep(1)
