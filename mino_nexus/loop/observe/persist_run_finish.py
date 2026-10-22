"""把一次 run 的终态落到 JSON。独立模块，不拖 copilot / CLIP（MIGRATION E1）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.casework import run_store
from mino_nexus.core.log import SLog

TAG = "PersistRunFinish"


def persist_run_finish(run_doc: dict[str, Any]) -> None:
    """对齐上游 persist_run_finish：写完即权威。无 SQLAlchemy。"""
    if not isinstance(run_doc, dict) or not run_doc.get("run_id"):
        return
    try:
        run_store.put(run_doc)
        try:
            from mino_nexus.assistant.assistant_notify import on_run_finished

            on_run_finished(run_doc)
        except Exception as exc:
            SLog.w(TAG, f"assistant notify failed run={run_doc.get('run_id')}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        SLog.w(TAG, f"persist_run_finish failed run={run_doc.get('run_id')}: {exc}")
