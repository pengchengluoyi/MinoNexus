"""Session Event Log：append-only 会话轨迹 API。"""
from __future__ import annotations

import json

import contextvars
import hashlib
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from mino_nexus.contract.attribution import classify_reason as _classify_reason
from mino_nexus.contract.attribution import enrich as _enrich_attribution
from mino_nexus.core.log import SLog
from mino_nexus.records.eventlog import store as session_store

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
_screenshot_hash: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mino_screenshot_hash", default=""
)


def set_screenshot_hash(value: str) -> None:
    """本回合决策图的文件哈希。llm/request 只记这个，不重复存图。"""
    _screenshot_hash.set(str(value or ""))


def _clip_value(val: Any, cap: int = _PAYLOAD_CAP) -> Any:
    if val is None or isinstance(val, (bool, int, float)):
        return val
    if isinstance(val, dict) and str(val.get("schema") or "") == "mino.success_criteria.v1":
        cap = max(int(cap), 16_000)
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("/static/"):
            return s
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
    from mino_nexus.core.secret_redaction import sanitize
    payload = sanitize(payload)
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


class SessionPersistenceError(RuntimeError):
    reason_code = "session_store_unavailable"

    def __init__(self, stage: str):
        self.failure_stage = stage
        super().__init__(f"会话记录未能持久化，已阻止后续界面写动作：{stage}")


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
        self._meta_persisted = False
        self._last_persisted_seq = 0
        self._store_failure = ""
        self._lock = _session_lock(self.session_id)
        self._t0 = time.perf_counter()
        self._turn_t0 = self._t0
        self._llm_ms = 0
        self._tool_ms = 0
        self._guard_blocks = 0
        self._first_blocker: dict[str, Any] | None = None
        self._latest_error: dict[str, Any] | None = None

    def require_persisted(self) -> None:
        """同一 attempt 的持久化失败保持阻断，不能靠后续写成功自动解锁。"""
        if self._store_failure or not self._meta_persisted or self._last_persisted_seq < 1:
            raise SessionPersistenceError(self._store_failure or "session_start_missing")

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

    def append(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        turn: int | None = None,
        phase: str | None = None,
    ) -> int:
        if self._closed or not self.session_id:
            return 0
        evt_type = str(type or "").strip()
        if not evt_type:
            return 0
        body = _sanitize_payload(payload or {})
        event_turn = self._turn if turn is None else max(0, int(turn))
        event_phase = self._phase if phase is None else str(phase)
        # 失败路径强制带 A/B/C：任何 blocked/skipped/fail 事件只补字段，不改业务状态。
        failure_like = evt_type in ("guard/block", "contract/blocked", "check/blocked",
                                   "orchestrator/blocked", "progress/stop") or str(
            body.get("status") or ""
        ).lower() in ("blocked", "skipped", "fail", "failed")
        if failure_like or any(body.get(k) for k in ("reason_code", "code", "dispatch_gate_code")):
            default_layer = ("guard" if evt_type == "guard/block" else
                             "dispatch" if evt_type in ("contract/blocked", "orchestrator/blocked") else
                             "execution")
            body = _enrich_attribution(body, layer=str(body.get("layer") or default_layer))
        if evt_type == "turn/start":
            self._turn_t0 = time.perf_counter()
        elif evt_type == "turn/end" and "elapsed_ms" not in body:
            body["elapsed_ms"] = max(0, int((time.perf_counter() - self._turn_t0) * 1000))
        elif evt_type == "llm/response":
            self._llm_ms += int(body.get("elapsed_ms") or 0)
        elif evt_type == "tool/result":
            self._tool_ms += int(body.get("elapsed_ms") or 0)
        elif evt_type == "guard/block":
            self._guard_blocks += 1
        # 同一个动作会被写成多条事件（tool/call + device/action + tool/result +
        # stream/emit + review/dispatched），Workbench 时间线上看起来像「同一步做了两次」。
        # 真正的重复是逐字节相同的：同类型 + 同 payload 在 5 秒内只留第一条。
        dedupe_key = (evt_type, json.dumps(body, sort_keys=True, ensure_ascii=False))
        if getattr(self, "_last_event_key", None) == dedupe_key and (
                time.time() - float(getattr(self, "_last_event_at", 0) or 0)) <= 5.0:
            return 0
        self._last_event_key = dedupe_key
        self._last_event_at = time.time()
        if evt_type in ("guard/block", "contract/blocked") and self._first_blocker is None:
            self._first_blocker = {
                "event_type": evt_type,
                "turn": self._turn,
                "reason_code": str(body.get("reason_code") or body.get("code") or body.get("dispatch_gate_code") or "blocked")[:100],
                "capability_id": str(body.get("capability_id") or "")[:80],
                "attribution_class": str(body.get("attribution_class") or "")[:1],
                "attribution_label": str(body.get("attribution_label") or "")[:40],
                "layer": str(body.get("layer") or "")[:40],
            }
        if evt_type == "tool/result" and str(body.get("status") or "").lower() in ("fail", "failed", "error", "timeout"):
            self._latest_error = {
                "event_type": evt_type,
                "turn": self._turn,
                "capability_id": str(body.get("capability_id") or "")[:80],
                "executor_used": str(body.get("executor_used") or "")[:80],
                "error_kind": "timeout" if "timeout" in str(body.get("error") or body.get("summary") or "").lower() else "executor_failure",
                "reason_code": str(body.get("reason_code") or body.get("error") or "")[:100],
                "attribution_class": str(body.get("attribution_class") or "")[:1],
                "layer": str(body.get("layer") or "")[:40],
            }
        elif evt_type in ("assert/vision", "assert/dom", "check/blocked") and (body.get("passed") is False or evt_type == "check/blocked"):
            self._latest_error = {
                "event_type": evt_type,
                "turn": self._turn,
                "error_kind": str(body.get("error_kind") or ("evidence_insufficient" if body.get("reason_code") == "observation_missing" or body.get("parse_warnings") else "assertion_failed")),
            }
        elif evt_type == "contract/blocked" and body.get("error_kind"):
            self._latest_error = {
                "event_type": evt_type,
                "turn": event_turn,
                "error_kind": str(body["error_kind"]),
                "reason_code": body.get("reason_code") or "",
            }
        with self._lock:
            try:
                seq = session_store.append_event(
                    session_id=self.session_id,
                    type=evt_type,
                    payload=body,
                    turn=event_turn,
                    phase=event_phase,
                )
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                    self._store_failure = self._store_failure or f"append:{evt_type}"
                    return 0
                self._last_persisted_seq = seq
                return seq
            except Exception as exc:
                self._store_failure = self._store_failure or f"append:{evt_type}"
                SLog.w(TAG, f"append {evt_type} failed session={self.session_id}: {exc!r}")
                return 0

    def close(self, *, status: str, summary: str = "", step_count: int = 0, reason_code: str = "",
              failure_stage: str = "", case_step: int = 0) -> None:
        with self._lock:
            if self._closed:
                return
            self.require_persisted()
            try:
                meta = get_meta(self.session_id)
            except Exception as exc:
                self._store_failure = "close_meta_read"
                raise SessionPersistenceError(self._store_failure) from exc
            if meta and str(meta.get("finished_at") or "").strip():
                self._closed = True
                return
            wall_ms = max(0, int((time.perf_counter() - self._t0) * 1000))
            if status != "pass":
                stop_kind = (
                    "cancelled" if status in ("cancel", "cancelled")
                    else "timeout" if status == "timeout" or "timeout" in summary.lower()
                    else "llm_output_contract_error" if (self._latest_error or {}).get("error_kind") == "llm_output_contract_error"
                    else "dependency_blocked" if status == "blocked"
                    else (self._latest_error or {}).get("error_kind") or "unclassified"
                )
                resolved = reason_code or str((self._first_blocker or {}).get("reason_code") or "")
                attribution = _classify_reason(
                    resolved or stop_kind,
                    layer=failure_stage or str((self._first_blocker or {}).get("layer") or ""),
                )
                stop_reason = {
                    "status": status, "kind": resolved or stop_kind,
                    "reason_code": resolved or stop_kind,
                    "failure_stage": failure_stage, "case_step": case_step,
                    **{k: attribution[k] for k in ("attribution_class", "attribution_label", "layer")},
                }
                self.append("session/attribution", {
                    "first_blocker": self._first_blocker,
                    "latest_error": self._latest_error,
                    "stop_reason": stop_reason,
                    "attribution": attribution,
                })
            self.append(
                "perf/summary",
                {
                    "wall_ms": wall_ms,
                    "llm_ms": self._llm_ms,
                    "tool_ms": self._tool_ms,
                    "other_ms": max(0, wall_ms - self._llm_ms - self._tool_ms),
                    "guard_blocks": self._guard_blocks,
                    "turns": self._turn,
                },
            )
            self.require_persisted()
            if meta and (meta.get("runtime_context") or {}).get("execution_binding") is not None:
                try:
                    failed_status = str(status or "").lower() in (
                        "failed", "fail", "blocked", "gave_up", "error", "timeout", "cancelled")
                    final_attr = (_classify_reason(reason_code or "ordinary_execution_result", layer=failure_stage)
                                  if failed_status else {"attribution_class": "", "attribution_label": "", "layer": ""})
                    session_store.finalize_bound(session_id=self.session_id, body={
                        "status": status, "summary": summary, "step_count": step_count, "wall_ms": wall_ms, "reason_code": reason_code,
                        "failure_stage": failure_stage, "case_step": case_step,
                        "attribution_class": final_attr["attribution_class"],
                        "attribution_label": final_attr["attribution_label"],
                        "layer": final_attr["layer"],
                    }, turn=self._turn, phase="done")
                    if (meta.get("runtime_context") or {}).get("resolved_case"):
                        finalized = get_meta(self.session_id)
                        verdict = finalized.get("verdict") or {}
                        self.final_outcome = {"status": finalized["status"], "summary": finalized["summary"],
                            "reason_code": verdict.get("reason_code", reason_code),
                            "failure_stage": verdict.get("failure_stage", failure_stage),
                            "case_step": verdict.get("case_step", case_step),
                            "attribution_class": verdict.get("attribution_class", final_attr["attribution_class"]),
                            "attribution_label": verdict.get("attribution_label", final_attr["attribution_label"]),
                            "layer": verdict.get("layer", final_attr["layer"])}
                except Exception as exc:
                    self._store_failure = "atomic_finalization"
                    raise SessionPersistenceError(self._store_failure) from exc
                self._closed = True
                return
            self.append(
                "session/end",
                {
                    "status": status,
                    "summary": summary,
                    "step_count": step_count,
                    "wall_ms": wall_ms,
                    "reason_code": reason_code, "failure_stage": failure_stage, "case_step": case_step,
                },
            )
            self.require_persisted()
            try:
                session_store.close_meta(
                    session_id=self.session_id,
                    status=status,
                    summary=summary,
                )
            except Exception as exc:
                self._store_failure = "close_meta"
                SLog.w(TAG, f"close_meta failed session={self.session_id}: {exc!r}")
                raise SessionPersistenceError(self._store_failure) from exc
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
        raise SessionPersistenceError("open_meta") from exc
    writer._meta_persisted = True
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
    writer.require_persisted()
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
    phase = str(slim.get("loop_phase") or writer.phase or "")
    step = slim.get("step")
    turn = step if isinstance(step, int) and step > 0 else None
    if slim.get("loop_phase") and (turn is None or turn >= writer.turn):
        writer.set_phase(phase)
    return writer.append("stream/emit", slim, turn=turn, phase=phase)


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
            "raw_response_available": bool(dispatch_row.get("raw_response_available")),
            "raw_response_redacted": bool(dispatch_row.get("raw_response_redacted")),
            "error": str(dispatch_row.get("error") or "")[:400],
            "tool_name": str(dispatch_row.get("tool_name") or ""),
            "prompt_version": int(dispatch_row.get("prompt_version") or 0) or None,
            **{k: dispatch_row.get(k) for k in ("provider_id", "sdk_provider", "http_status", "provider_error_code", "provider_error_param", "provider_request_id",
                                               "fail_kind", "finish_reason", "prompt_sha", "call_parameters", "stream_structure", "response_trace", "raw_response_source", "usage_status", "estimated_cost_usd", "cost_status", "cost_source", "pricing_version", "usage_attempt_ids", "cost_tracking_error")},
        },
    )


def summarize_messages(messages: list | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content")
        image_hash = ""
        if isinstance(content, list):
            text_bits = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_bits.append(str(part.get("text") or ""))
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    url = ""
                    raw = part.get("image_url")
                    if isinstance(raw, dict):
                        url = str(raw.get("url") or "")
                    elif isinstance(raw, str):
                        url = raw
                    image_hash = _hash_text(url)
                    text_bits.append("[image]")
            content = "\n".join(text_bits)
        text = str(content or "")
        row = {"role": role, "len": len(text), "hash": _hash_text(text)}
        if image_hash:
            row["image_hash"] = image_hash
        rows.append(row)
    return rows


def append_llm_request(*, job_id: str, messages: list | None,
                       prompt_version: int | None = None,
                       prompt_sha: str | None = None) -> int:
    """事件只记调用方已知的事实；不反查 prompt 仓库（records 不依赖 ai）。"""
    writer = active_writer()
    if writer is None:
        return 0
    payload: dict[str, Any] = {
        "job_id": job_id,
        "messages": summarize_messages(messages),
    }
    if _screenshot_hash.get():
        payload["screenshot_hash"] = _screenshot_hash.get()
    if job_id and (prompt_version or prompt_sha):
        payload["prompt_version"] = int(prompt_version) if prompt_version else None
        payload["prompt_sha"] = str(prompt_sha or "") or None
    return writer.append("llm/request", payload)


def read_events(session_id: str, *, from_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    return session_store.read_events(session_id, from_seq=from_seq, limit=limit)


def get_meta(session_id: str) -> dict[str, Any] | None:
    return session_store.get_meta(session_id)
