"""Session Event Log：append-only 会话轨迹 API。"""
from __future__ import annotations

import contextvars
import hashlib
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from mino_nexus.core.log import SLog
from mino_nexus.services import session_store

TAG = "SessionLog"
_ACTIVE: contextvars.ContextVar[Optional["SessionWriter"]] = contextvars.ContextVar(
    "session_writer", default=None
)
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()

_PAYLOAD_CAP = 8000
_SLOT_CAP = 1200


def _session_lock(session_id: str) -> threading.Lock:
    sid = str(session_id or "").strip()
    with _LOCK_GUARD:
        lock = _SESSION_LOCKS.get(sid)
        if lock is None:
            lock = threading.RLock()
            _SESSION_LOCKS[sid] = lock
        return lock


def _hash_text(text: str) -> str:
    raw = str(text or "").encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


_THUMB_CAP = 65536


def _clip_value(val: Any, cap: int = _PAYLOAD_CAP) -> Any:
    if val is None or isinstance(val, (bool, int, float)):
        return val
    if isinstance(val, str):
        s = val.strip()
        if len(s) <= cap:
            return s
        return s[: cap - 20] + f"...(+{len(s) - cap + 20})"
    if isinstance(val, list):
        return [_clip_value(x, min(cap, _SLOT_CAP)) for x in val[:80]]
    if isinstance(val, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(val.items()):
            if i >= 60:
                out["_truncated"] = len(val) - i
                break
            out[str(k)] = _clip_value(v, min(cap, _SLOT_CAP))
        return out
    return _clip_value(str(val), cap)


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in (payload or {}).items():
        cap = _THUMB_CAP if str(key) == "thumb" else _PAYLOAD_CAP
        clipped = _clip_value(val, cap=cap)
        if isinstance(clipped, dict):
            inner: dict[str, Any] = {}
            for ik, iv in clipped.items():
                icap = _THUMB_CAP if str(ik) == "thumb" else min(cap, _SLOT_CAP)
                inner[str(ik)] = _clip_value(iv, cap=icap)
            out[str(key)] = inner
        else:
            out[str(key)] = clipped
    return out


def active_writer() -> Optional["SessionWriter"]:
    return _ACTIVE.get()


class SessionWriter:
    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        case_id: str = "",
        app_id: str = "",
    ):
        self.session_id = str(session_id or "").strip()
        self.run_id = str(run_id or "").strip()
        self.case_id = str(case_id or "").strip()
        self.app_id = str(app_id or "").strip()
        self._turn = 0
        self._phase = ""
        self._closed = False
        self._lock = _session_lock(self.session_id)

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def phase(self) -> str:
        return self._phase

    def set_turn(self, turn: int) -> None:
        self._turn = max(0, int(turn or 0))

    def set_phase(self, phase: str) -> None:
        self._phase = str(phase or "")

    def append(self, type: str, payload: dict[str, Any] | None = None) -> int:
        if self._closed or not self.session_id:
            return 0
        evt_type = str(type or "").strip()
        if not evt_type:
            return 0
        body = _sanitize_payload(payload or {})
        with self._lock:
            try:
                return session_store.append_event(
                    session_id=self.session_id,
                    type=evt_type,
                    payload=body,
                    turn=self._turn,
                    phase=self._phase,
                )
            except Exception as exc:
                SLog.w(TAG, f"append {evt_type} failed session={self.session_id}: {exc!r}")
                return 0

    def close(self, *, status: str, summary: str = "", step_count: int = 0) -> None:
        with self._lock:
            if self._closed:
                return
            meta = get_meta(self.session_id)
            if meta and str(meta.get("finished_at") or "").strip():
                self._closed = True
                return
            self.append(
                "session/end",
                {
                    "status": status,
                    "summary": summary,
                    "step_count": step_count,
                },
            )
            try:
                session_store.close_meta(
                    session_id=self.session_id,
                    status=status,
                    summary=summary,
                )
            except Exception as exc:
                SLog.w(TAG, f"close_meta failed session={self.session_id}: {exc!r}")
            self._closed = True


def force_close_session(
    *,
    session_id: str,
    status: str,
    summary: str = "",
    step_count: int = 0,
) -> bool:
    """取消 / 断连 / 批次结束时关闭仍 running 的 session。"""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    with _session_lock(sid):
        writer = active_writer()
        if writer is not None and writer.session_id == sid and not writer._closed:
            writer.close(status=status, summary=summary, step_count=step_count)
            return True
        return session_store.force_close(
            session_id=sid,
            status=status,
            summary=summary,
            step_count=step_count,
        )


def open_session(
    *,
    session_id: str,
    run_id: str,
    case_id: str = "",
    app_id: str = "",
    provider_id: str = "",
    sn: str = "",
    sop_id: str = "run-case",
    extra: dict[str, Any] | None = None,
) -> SessionWriter:
    writer = SessionWriter(
        session_id=session_id,
        run_id=run_id,
        case_id=case_id,
        app_id=app_id,
    )
    try:
        session_store.open_meta(
            session_id=session_id,
            run_id=run_id,
            case_id=case_id,
            app_id=app_id,
        )
    except Exception as exc:
        SLog.w(TAG, f"open_meta failed session={session_id}: {exc!r}")
        return writer
    payload: dict[str, Any] = {
        "case_id": case_id,
        "run_id": run_id,
        "app_id": app_id,
        "sn": sn,
        "provider_id": provider_id,
        "sop_id": sop_id,
    }
    if extra:
        payload.update(extra)
    writer.append("session/start", payload)
    return writer


@contextmanager
def bind_writer(writer: SessionWriter) -> Iterator[SessionWriter]:
    token = _ACTIVE.set(writer)
    try:
        yield writer
    finally:
        _ACTIVE.reset(token)


def mirror_stream_event(data: dict[str, Any]) -> int:
    """双写 Studio emit 载荷到 stream/emit。"""
    writer = active_writer()
    if writer is None:
        return 0
    slim = dict(data or {})
    slim.pop("image_base64", None)
    phase = str(slim.get("loop_phase") or slim.get("phase") or writer.phase or "")
    if phase:
        writer.set_phase(phase)
    step = slim.get("step")
    if isinstance(step, int) and step > 0:
        writer.set_turn(step)
    return writer.append("stream/emit", slim)


def append_llm_response(dispatch_row: dict[str, Any]) -> int:
    writer = active_writer()
    if writer is None:
        return 0
    system = str(dispatch_row.get("system_prompt") or "")
    user = str(dispatch_row.get("input") or "")
    output = str(dispatch_row.get("output") or "")
    return writer.append(
        "llm/response",
        {
            "dispatch_id": dispatch_row.get("id") or "",
            "job": dispatch_row.get("job") or "",
            "model": dispatch_row.get("model") or "",
            "status": dispatch_row.get("status") or "",
            "elapsed_ms": int(dispatch_row.get("elapsed_ms") or 0),
            "prompt_tokens": int(dispatch_row.get("prompt_tokens") or 0),
            "completion_tokens": int(dispatch_row.get("completion_tokens") or 0),
            "total_tokens": int(dispatch_row.get("total_tokens") or 0),
            "system_hash": _hash_text(system),
            "input_hash": _hash_text(user),
            "output_hash": _hash_text(output),
            "system_len": len(system),
            "input_len": len(user),
            "output_len": len(output),
            "error": str(dispatch_row.get("error") or "")[:400],
            "tool_name": str(dispatch_row.get("tool_name") or ""),
        },
    )


def summarize_messages(messages: list | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if isinstance(content, list):
            text_bits = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_bits.append(str(part.get("text") or ""))
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    text_bits.append("[image]")
            content = "\n".join(text_bits)
        text = str(content or "")
        rows.append({"role": role, "len": len(text), "hash": _hash_text(text)})
    return rows


def append_llm_request(*, job_id: str, messages: list | None) -> int:
    writer = active_writer()
    if writer is None:
        return 0
    return writer.append(
        "llm/request",
        {
            "job_id": job_id,
            "messages": summarize_messages(messages),
        },
    )


def read_events(session_id: str, *, from_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    return session_store.read_events(session_id, from_seq=from_seq, limit=limit)


def get_meta(session_id: str) -> dict[str, Any] | None:
    return session_store.get_meta(session_id)
