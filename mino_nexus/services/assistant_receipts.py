"""崩溃后不盲重放写操作；只保存 ID/状态，不保存聊天正文和工具参数。"""
import time
import uuid
from sqlalchemy import or_, and_, case
from sqlalchemy.dialects.sqlite import insert
from mino_nexus.core.database import session_scope
from mino_nexus.core.execution_user import require_user_id
from mino_nexus.models.assistant_delivery import AssistantReceipt as R


def acquire(key: str, *, kind: str, lease_seconds: int = 900) -> str:
    owner, now, token = require_user_id(), int(time.time()), uuid.uuid4().hex
    with session_scope() as db:
        values = dict(key=key, owner_user_id=owner, kind=kind, state="processing", lease_token=token,
                      lease_until=now + lease_seconds, side_effects=False, updated_at=now, data_json={})
        result = db.execute(insert(R).values(**values).on_conflict_do_update(index_elements=["key"],
            set_={k: v for k, v in values.items() if k not in ("key", "owner_user_id", "kind")},
            where=and_(R.owner_user_id == owner, R.kind == kind, R.side_effects.is_(False),
                or_(and_(R.state == "retryable", R.lease_until <= now), and_(R.state == "processing", R.lease_until <= now)))))
        return token if result.rowcount == 1 else ""


def finish(key: str, token: str, state: str, data: dict | None = None):
    with session_scope() as db:
        now = int(time.time())
        new_state = case((R.side_effects.is_(True), "uncertain"), else_="retryable") if state == "retryable" else state
        db.query(R).filter(R.key == key, R.owner_user_id == require_user_id(), R.lease_token == token,
                           R.state == "processing").update({R.state: new_state, R.data_json: data or {},
                           R.lease_until: case((R.side_effects.is_(False), now + 30), else_=0) if state == "retryable" else 0,
                           R.updated_at: now}, synchronize_session=False)


def mark_side_effect(key: str, token: str):
    if not key:
        return
    with session_scope() as db:
        updated = db.query(R).filter(R.key == key, R.owner_user_id == require_user_id(), R.lease_token == token, R.state == "processing",
                                    R.lease_until > int(time.time())).update({R.side_effects: True}, synchronize_session=False)
        if not updated:
            raise PermissionError("入站处理租约已失效，禁止继续写操作")


def get(key: str) -> dict | None:
    with session_scope() as db:
        row = db.get(R, key)
        if row is None or row.owner_user_id != require_user_id():
            return None
        return {"state": row.state, "data": row.data_json or {}, "side_effects": row.side_effects}
