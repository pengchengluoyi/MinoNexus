"""DOC_LEARN：从文档分片抽取可验证口径 → knowledge_entries（待审）。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services import doc_store as ds
from mino_nexus.services import knowledge_store as ks

TAG = "DocLearn"

DOC_LEARN_SYSTEM = """You extract test-verifiable facts from product documentation excerpts.
Return one JSON object only:
{"items":[{"title":"short label","content":"verifiable rule for testers","category":"业务逻辑|UI导航|交互规范|其他","tags":[],"evidence":"verbatim quote"}]}

Rules:
- Only include facts verifiable during UI/manual testing (timeouts, counts, copy requirements, flows).
- Do NOT include architecture, implementation, or subjective praise.
- Each item MUST include evidence copied from the excerpt.
- Skip duplicates; max 5 items per batch.
- If nothing testable, return {"items":[]}."""


def _extract_batch(batch: list[dict[str, Any]], *, doc_title: str, source_id: str) -> list[dict[str, Any]]:
    if not batch:
        return []
    payload = {
        "document": doc_title,
        "source_id": source_id,
        "chunks": [
            {
                "chunk_id": ch.get("id"),
                "heading": ch.get("heading") or "",
                "text": str(ch.get("text") or "")[:2000],
            }
            for ch in batch
        ],
    }
    try:
        from mino_nexus.ai.dispatch_log import bind, reset
        from mino_nexus.ai.llm_client import call_chat_text, resolve_regression_provider
    except Exception as exc:
        SLog.w(TAG, f"import failed: {exc}")
        return []
    provider, gate = resolve_regression_provider()
    if provider is None:
        SLog.i(TAG, f"skip extract: {(gate or {}).get('reason')}")
        return []
    tok = bind(
        trigger="doc_learn",
        source="doc_learn",
        role="knowledge-reviewer",
        job="doc-learn",
        skill="doc-learn",
    )
    try:
        raw, meta = call_chat_text(
            provider=provider,
            messages=[
                {"role": "system", "content": DOC_LEARN_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.0,
            max_tokens=1200,
            timeout_sec=60,
            json_mode=True,
        )
    finally:
        reset(tok)
    if raw is None:
        SLog.w(TAG, f"llm failed: {(meta or {}).get('error')}")
        return []
    data = raw if isinstance(raw, dict) else {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        content = str(row.get("content") or "").strip()
        evidence = str(row.get("evidence") or "").strip()
        if not title or not content:
            continue
        if evidence and evidence not in content:
            content = f"{content}\n\n依据：{evidence}"
        out.append({
            "title": title[:120],
            "content": content[:2000],
            "category": str(row.get("category") or "其他").strip() or "其他",
            "tags": [str(t).strip() for t in (row.get("tags") or []) if str(t).strip()],
        })
    return out


def extract_from_source(
    source_id: str,
    *,
    app_id: str,
    project_id: str = "",
    chunk_batch: int = 3,
    max_batches: int = 8,
) -> dict[str, Any]:
    """从已入库文档抽取知识条，写入 knowledge_entries（pending）。"""
    sid = str(source_id or "").strip()
    aid = str(app_id or "").strip()
    if not sid or not aid:
        raise ValueError("source_id 与 app_id 不能为空")
    meta = ds.get_source(sid)
    if not meta:
        raise ValueError("文档不存在")
    if str(meta.get("app_id") or "") != aid:
        raise ValueError("文档不属于该应用")
    if str(meta.get("status") or "") != "ok":
        raise ValueError("文档尚未成功索引")

    chunks = ds.list_chunks(sid, limit=200)
    if not chunks:
        return {"saved": 0, "items": [], "batches": 0}

    doc_title = str(meta.get("title") or meta.get("original_filename") or sid)
    app_ids = [aid]
    if project_id:
        app_ids.append(str(project_id).strip())

    saved: list[dict[str, Any]] = []
    batch_size = max(1, min(5, int(chunk_batch or 3)))
    max_b = max(1, min(20, int(max_batches or 8)))
    batches = 0
    for i in range(0, len(chunks), batch_size):
        if batches >= max_b:
            break
        batch = chunks[i : i + batch_size]
        extracted = _extract_batch(batch, doc_title=doc_title, source_id=sid)
        batches += 1
        for item in extracted:
            kid = uuid.uuid4().hex[:12]
            row = ks.upsert_knowledge_item({
                "id": kid,
                "title": item["title"],
                "content": item["content"],
                "category": item.get("category") or "其他",
                "tags": list(item.get("tags") or []),
                "app_ids": list(dict.fromkeys(app_ids)),
                "enabled": True,
                "source": "doc",
                "review_status": "pending",
                "source_ref": f"doc:{sid}",
            })
            saved.append(row)
    return {"saved": len(saved), "items": saved, "batches": batches}
