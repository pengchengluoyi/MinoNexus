"""文档库执行期检索。离线索引，在线只做 FTS，失败降级为空。"""
from __future__ import annotations

from typing import Any

from mino_nexus.ai.doc_hint import build_doc_context
from mino_nexus.services import doc_store as ds

DOC_USE_MIN_HITS = 1
DOC_STEP_LIMIT = 3
DOC_STUCK_LIMIT = 2
DOC_STUCK_MAX_CHARS = 800


def match_step_docs(
    query: str,
    *,
    app_id: str,
    limit: int = DOC_STEP_LIMIT,
    max_chars: int = 1200,
    query_vec: list[float] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """按查询串检索文档分片。返回 (hits, doc_context 块)。"""
    aid = str(app_id or "").strip()
    q = str(query or "").strip()
    if not aid or not q:
        return [], ""
    try:
        hits = ds.search_documents(
            q=q,
            app_id=aid,
            limit=max(1, min(10, int(limit or DOC_STEP_LIMIT))),
            query_vec=query_vec,
        )
    except Exception:
        return [], ""
    if len(hits) < DOC_USE_MIN_HITS:
        return [], ""
    block = build_doc_context(hits, max_chars=max_chars)
    return hits, block


def match_stuck_docs(
    query: str,
    *,
    app_id: str,
    screen_text: str = "",
    limit: int = DOC_STUCK_LIMIT,
    query_vec: list[float] | None = None,
) -> str:
    """遇阻时以屏文案为主的二次检索（T3）。"""
    aid = str(app_id or "").strip()
    screen = str(screen_text or "").strip()[:2000]
    q = str(query or "").strip()
    merged = "\n".join(x for x in (screen, q) if x).strip()
    if not aid or not merged:
        return ""
    _, block = match_step_docs(
        merged,
        app_id=aid,
        limit=limit,
        max_chars=DOC_STUCK_MAX_CHARS,
        query_vec=query_vec,
    )
    return block
