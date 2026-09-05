"""Agent 步骤缓冲：进程内存，供 Studio AgentRun 回填。重启即丢，批次结论在 app_regression_runs。"""
from __future__ import annotations

import base64
import copy
from collections import OrderedDict
from io import BytesIO
from threading import Lock
from typing import Any

from mino_nexus.core.log import SLog

TAG = "AgentStream"
_MAX_RUNS = 40
_MAX_EVENTS_PER_RUN = 200
_RUNS: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = Lock()


def _buffer(data: dict[str, Any]) -> None:
    run_id = data.get("run_id")
    if not run_id:
        return
    with _LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            run = {
                "run_id": run_id,
                "case_id": data.get("case_id", ""),
                "goal": data.get("goal", ""),
                "events": [],
                "overall": "",
                "finished": False,
            }
            _RUNS[run_id] = run
            while len(_RUNS) > _MAX_RUNS:
                _RUNS.popitem(last=False)
        _RUNS.move_to_end(run_id)
        if data.get("goal"):
            run["goal"] = data["goal"]
        if data.get("case_id"):
            run["case_id"] = data["case_id"]
        if data.get("skill_id"):
            run["skill_id"] = data["skill_id"]
        if data.get("view_id"):
            run["view_id"] = data["view_id"]
        if isinstance(data.get("slots"), dict):
            run["slots"] = data["slots"]
        if data.get("phase") == "done":
            run["overall"] = data.get("status") or data.get("overall", "")
            run["summary"] = data.get("summary") or data.get("overall", "")
            run["finished"] = True
        slim = dict(data)
        if slim.get("image_base64") and not slim.get("thumb"):
            slim["thumb"] = make_thumb(str(slim.get("image_base64") or ""))
        slim.pop("image_base64", None)
        run["events"].append(slim)
        if len(run["events"]) > _MAX_EVENTS_PER_RUN:
            run["events"] = run["events"][-_MAX_EVENTS_PER_RUN:]


def list_recent_runs() -> list[dict[str, Any]]:
    with _LOCK:
        return [
            {
                "run_id": r["run_id"],
                "case_id": r.get("case_id") or "",
                "goal": r.get("goal") or "",
                "overall": r.get("overall") or "",
                "finished": bool(r.get("finished")),
                "steps": len(r.get("events") or []),
                "skill_id": r.get("skill_id") or "",
                "view_id": r.get("view_id") or "",
            }
            for r in reversed(_RUNS.values())
        ]


def get_run_events(run_id: str) -> dict[str, Any] | None:
    rid = str(run_id or "").strip()
    if not rid:
        return None
    with _LOCK:
        hit = _RUNS.get(rid)
        if hit is None and "::" in rid:
            batch, _, case_id = rid.partition("::")
            batch_hit = _RUNS.get(batch)
            if batch_hit is not None:
                stored_cid = str(batch_hit.get("case_id") or "")
                if not case_id or not stored_cid or stored_cid == case_id.split("::", 1)[0]:
                    hit = batch_hit
        if hit is None and "::" not in rid:
            prefix = f"{rid}::"
            for key in reversed(list(_RUNS.keys())):
                if str(key).startswith(prefix):
                    hit = _RUNS[key]
                    break
        return copy.deepcopy(hit) if hit else None


def make_thumb(png_b64: str, *, width: int = 360, quality: int = 70) -> str:
    if not png_b64:
        return ""
    try:
        from PIL import Image

        raw_b64 = png_b64.strip()
        if raw_b64.startswith("data:") and "," in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]
        raw = base64.b64decode(raw_b64)
        img = Image.open(BytesIO(raw)).convert("RGB")
        w, h = img.size
        max_w = width
        q = quality
        if w >= h and w <= 960:
            max_w = w
            q = max(quality, 82)
        if max_w and w > max_w:
            img = img.resize((max_w, max(1, int(h * max_w / w))))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=q)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        SLog.d(TAG, f"make_thumb failed: {e}")
        return ""


def emit_agent_event(data: dict[str, Any]) -> None:
    _buffer(data)
    try:
        from mino_nexus.websocket import observers as ui_ws

        slim = dict(data)
        slim.pop("image_base64", None)
        ui_ws.broadcast_sync({"type": "agent_step", "data": slim})
    except Exception as exc:
        SLog.d(TAG, f"emit_agent_event broadcast failed: {exc}")


def emit_testing_task(data: dict[str, Any]) -> None:
    SLog.d(TAG, f"testing_task {data.get('event') or ''} run={data.get('run_id') or data.get('task_id') or ''}")
    try:
        from mino_nexus.websocket import observers as ui_ws

        payload = dict(data)
        if payload.get("run_id") and not payload.get("task_id"):
            payload["task_id"] = payload["run_id"]
        ui_ws.broadcast_sync({"type": "testing_task", "data": payload})
    except Exception as exc:
        SLog.d(TAG, f"emit_testing_task broadcast failed: {exc}")
