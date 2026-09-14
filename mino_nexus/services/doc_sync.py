"""飞书文档定时增量同步（按 content_hash 跳过无变化）。"""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services import doc_store as ds
from mino_nexus.services import feishu_doc_service as fds

TAG = "DocSync"
_MIN_INTERVAL = 300
_TICK_LIMIT = 5


def list_due_feishu_sources(*, limit: int = _TICK_LIMIT) -> list[dict[str, Any]]:
    now = int(time.time())
    rows = ds.list_feishu_sync_candidates()
    due: list[dict[str, Any]] = []
    for row in rows:
        if int(row.get("auto_sync") or 0) != 1:
            continue
        interval = max(_MIN_INTERVAL, int(row.get("sync_interval_sec") or 3600))
        if int(row.get("updated_at") or 0) + interval > now:
            continue
        due.append(row)
    due.sort(key=lambda x: int(x.get("updated_at") or 0))
    return due[: max(1, min(20, int(limit or _TICK_LIMIT)))]


def sync_source_now(source_id: str) -> dict[str, Any]:
    row = ds.get_source(source_id)
    if not row:
        raise ValueError("文档不存在")
    if str(row.get("source_kind") or "") != "feishu":
        raise ValueError("仅飞书来源支持同步")
    url = str(row.get("source_url") or "").strip()
    if not url:
        raise ValueError("缺少 source_url")
    return fds.sync_feishu_document(
        url=url,
        app_id=str(row.get("app_id") or ""),
        project_id=str(row.get("project_id") or ""),
        bot_id=str(row.get("feishu_bot_id") or ""),
        title=str(row.get("title") or ""),
        enable_auto_sync=True,
    )


def doc_sync_tick(*, limit: int = _TICK_LIMIT) -> int:
    """后台轮询：返回本轮成功同步条数。"""
    done = 0
    for row in list_due_feishu_sources(limit=limit):
        sid = str(row.get("id") or "")
        try:
            sync_source_now(sid)
            done += 1
            SLog.i(TAG, f"synced {sid} ({row.get('title') or ''})")
        except Exception as exc:
            SLog.w(TAG, f"sync {sid} failed: {exc}")
    return done
