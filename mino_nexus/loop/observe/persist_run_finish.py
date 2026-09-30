"""把一次 run 的终态落到 JSON。独立模块，不拖 copilot / CLIP（MIGRATION E1）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services import run_store
from mino_nexus.core.log import SLog

TAG = "PersistRunFinish"


def persist_run_finish(run_doc: dict[str, Any]) -> None:
    """对齐上游 persist_run_finish：写完即权威。无 SQLAlchemy。"""
    if not isinstance(run_doc, dict) or not run_doc.get("run_id"):
        return
    try:
        run_store.put(run_doc)
    except Exception as exc:
        SLog.w(TAG, f"persist_run_finish failed run={run_doc.get('run_id')}: {exc}")
