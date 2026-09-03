"""Agent 步骤缓冲：内存 + JSON，供 Studio AgentRun 回填。"""
from __future__ import annotations

import base64
import copy
from collections import OrderedDict
from io import BytesIO
from threading import Lock
from typing import Any

from mino_nexus.json_store import load_json, save_json
from mino_nexus.log import SLog

TAG = "AgentStream"
_FILE = "agent_steps.json"
_MAX_RUNS = 40
_MAX_EVENTS_PER_RUN = 200
_RUNS: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = Lock()


def _persist() -> None:
    payload = {"runs": list(_RUNS.values())[-_MAX_RUNS:]}
    save_json(_FILE, payload)


def _hydrate() -> None:
    if _RUNS:
        return
    raw = load_json(_FILE, {})
    rows = raw.get("runs") if isinstance(raw, dict) else []
    if not isinstance(rows, list):
        return
    for row in rows[-_MAX_RUNS:]:
        if isinstance(row, dict) and row.get("run_id"):
            _RUNS[str(row["run_id"])] = row


def _buffer(data: dict[str, Any]) -> None:
    run_id = data.get("run_id")
    if not run_id:
        return
    with _LOCK:
        _hydrate()
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
        if data.get("phase") == "done":
            run["overall"] = data.get("overall", "")
            run["finished"] = True
        slim = dict(data)
        if slim.get("image_base64") and not slim.get("thumb"):
            slim["thumb"] = make_thumb(str(slim.get("image_base64") or ""))
        slim.pop("image_base64", None)
        run["events"].append(slim)
        if len(run["events"]) > _MAX_EVENTS_PER_RUN:
            run["events"] = run["events"][-_MAX_EVENTS_PER_RUN:]
        _persist()


def list_recent_runs() -> list[dict[str, Any]]:
    with _LOCK:
        _hydrate()
        return [
            {
                "run_id": r["run_id"],
                "case_id": r.get("case_id") or "",
                "goal": r.get("goal") or "",
                "overall": r.get("overall") or "",
                "finished": bool(r.get("finished")),
                "steps": len(r.get("events") or []),
            }
            for r in reversed(_RUNS.values())
        ]


def get_run_events(run_id: str) -> dict[str, Any] | None:
    with _LOCK:
        _hydrate()
        r = _RUNS.get(run_id)
        return copy.deepcopy(r) if r else None


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


def emit_testing_task(data: dict[str, Any]) -> None:
    SLog.d(TAG, f"testing_task {data.get('event') or ''} run={data.get('run_id') or data.get('task_id') or ''}")
