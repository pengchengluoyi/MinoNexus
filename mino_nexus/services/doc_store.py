"""文档库：上传、分片、FTS 全文检索。"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text

from mino_nexus.core.database import engine, session_scope
from mino_nexus.core.paths import data_dir
from mino_nexus.models.doc_library import DocChunk, DocSource
from mino_nexus.services import doc_ingest as ingest
from mino_nexus.services import doc_embed as dembed
from mino_nexus.services.doc_text_norm import normalize_doc_text

MAX_UPLOAD_BYTES = 32 * 1024 * 1024


def _safe_seg(raw: str) -> str:
    seg = str(raw or "").strip().replace("\\", "/").split("/")[-1]
    seg = "".join(ch for ch in seg if ch.isalnum() or ch in "-_.")
    return "_" if seg in ("", ".", "..") else seg


def docs_dir(app_id: str, source_id: str = "") -> Path:
    base = data_dir() / "docs" / _safe_seg(app_id)
    return base / _safe_seg(source_id) if source_id else base


def ensure_fts() -> None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='doc_chunks_fts'"),
        ).fetchone()
        if row:
            return
        conn.execute(text(
            """
            CREATE VIRTUAL TABLE doc_chunks_fts USING fts5(
                chunk_id UNINDEXED,
                source_id UNINDEXED,
                app_id UNINDEXED,
                title,
                heading,
                body,
                tokenize='unicode61'
            )
            """
        ))
        conn.commit()


def _to_public_source(row: DocSource) -> dict[str, Any]:
    return {
        "id": row.id,
        "app_id": row.app_id or "",
        "project_id": row.project_id or "",
        "title": row.title or "",
        "file_type": row.file_type or "",
        "original_filename": row.original_filename or "",
        "content_hash": row.content_hash or "",
        "chunk_count": int(row.chunk_count or 0),
        "status": row.status or "ok",
        "error_msg": row.error_msg or "",
        "file_size": int(row.file_size or 0),
        "source_kind": row.source_kind or "upload",
        "source_url": row.source_url or "",
        "feishu_bot_id": row.feishu_bot_id or "",
        "auto_sync": int(row.auto_sync or 0),
        "sync_interval_sec": int(row.sync_interval_sec or 3600),
        "created_at": int(row.created_at or 0),
        "updated_at": int(row.updated_at or 0),
    }


def _to_public_chunk(row: DocChunk) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_id": row.source_id,
        "chunk_index": int(row.chunk_index or 0),
        "heading": row.heading or "",
        "text": row.text or "",
        "char_start": int(row.char_start or 0),
        "char_end": int(row.char_end or 0),
    }


def list_sources(*, app_id: str, project_id: str = "") -> list[dict[str, Any]]:
    aid = str(app_id or "").strip()
    if not aid:
        return []
    ensure_fts()
    with session_scope() as db:
        q = db.query(DocSource).filter(DocSource.app_id == aid)
        pid = str(project_id or "").strip()
        if pid:
            q = q.filter(DocSource.project_id == pid)
        rows = q.order_by(DocSource.updated_at.desc()).all()
        return [_to_public_source(r) for r in rows]


def find_source_by_url(*, app_id: str, source_url: str) -> dict[str, Any] | None:
    aid = str(app_id or "").strip()
    url = str(source_url or "").strip()
    if not aid or not url:
        return None
    with session_scope() as db:
        row = (
            db.query(DocSource)
            .filter(DocSource.app_id == aid, DocSource.source_url == url)
            .order_by(DocSource.updated_at.desc())
            .first()
        )
        return _to_public_source(row) if row else None


def touch_source(source_id: str) -> None:
    sid = str(source_id or "").strip()
    if not sid:
        return
    with session_scope() as db:
        row = db.get(DocSource, sid)
        if row:
            row.updated_at = int(time.time())


def set_sync_settings(
    source_id: str,
    *,
    auto_sync: int | bool | None = None,
    sync_interval_sec: int | None = None,
) -> dict[str, Any] | None:
    sid = str(source_id or "").strip()
    if not sid:
        return None
    with session_scope() as db:
        row = db.get(DocSource, sid)
        if not row:
            return None
        if auto_sync is not None:
            row.auto_sync = 1 if auto_sync else 0
        if sync_interval_sec is not None:
            row.sync_interval_sec = max(300, int(sync_interval_sec or 3600))
        row.updated_at = int(time.time())
    return get_source(sid)


def list_feishu_sync_candidates() -> list[dict[str, Any]]:
    with session_scope() as db:
        rows = (
            db.query(DocSource)
            .filter(DocSource.source_kind == "feishu", DocSource.status == "ok")
            .order_by(DocSource.updated_at.asc())
            .all()
        )
        return [_to_public_source(r) for r in rows]


def get_source(source_id: str) -> dict[str, Any] | None:
    sid = str(source_id or "").strip()
    if not sid:
        return None
    with session_scope() as db:
        row = db.get(DocSource, sid)
        return _to_public_source(row) if row else None


def list_chunks(source_id: str, *, offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    sid = str(source_id or "").strip()
    if not sid:
        return []
    off = max(0, int(offset or 0))
    lim = max(1, min(200, int(limit or 50)))
    with session_scope() as db:
        rows = (
            db.query(DocChunk)
            .filter(DocChunk.source_id == sid)
            .order_by(DocChunk.chunk_index.asc())
            .offset(off)
            .limit(lim)
            .all()
        )
        return [_to_public_chunk(r) for r in rows]


def reindex_source(source_id: str) -> dict[str, Any]:
    """按最新归一化规则重写分片正文与 FTS（PDF 部首/控制符修复后调用）。"""
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError("source_id 不能为空")
    ensure_fts()
    with session_scope() as db:
        source = db.get(DocSource, sid)
        if not source:
            raise ValueError("文档不存在")
        chunks = (
            db.query(DocChunk)
            .filter(DocChunk.source_id == sid)
            .order_by(DocChunk.chunk_index.asc())
            .all()
        )
        aid = str(source.app_id or "")
        title = str(source.title or "")
        fts_rows: list[dict[str, str]] = []
        chunk_ids: list[str] = []
        for ch in chunks:
            norm = normalize_doc_text(str(ch.text or ""))
            ch.text = norm
            chunk_ids.append(ch.id)
            fts_rows.append({
                "chunk_id": ch.id,
                "source_id": sid,
                "app_id": aid,
                "title": title,
                "heading": str(ch.heading or ""),
                "body": norm,
            })
        source.updated_at = int(time.time())
    _delete_fts_chunks(chunk_ids)
    _insert_fts_rows(fts_rows)
    _embed_source_chunks(sid)
    out = get_source(sid)
    return out or {"id": sid}


def delete_source(source_id: str) -> bool:
    sid = str(source_id or "").strip()
    if not sid:
        return False
    ensure_fts()
    with session_scope() as db:
        row = db.get(DocSource, sid)
        if not row:
            return False
        app_id = row.app_id or ""
        chunk_ids = [
            c.id for c in db.query(DocChunk).filter(DocChunk.source_id == sid).all()
        ]
        db.query(DocChunk).filter(DocChunk.source_id == sid).delete()
        db.delete(row)
    _delete_fts_chunks(chunk_ids)
    folder = docs_dir(app_id, sid)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    return True


def _delete_fts_chunks(chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    with engine.connect() as conn:
        for cid in chunk_ids:
            conn.execute(text("DELETE FROM doc_chunks_fts WHERE chunk_id = :cid"), {"cid": cid})
        conn.commit()


def _purge_source_id(source_id: str, app_id: str = "") -> None:
    sid = str(source_id or "").strip()
    if not sid:
        return
    aid = str(app_id or "").strip()
    with session_scope() as db:
        chunk_ids = [c.id for c in db.query(DocChunk).filter(DocChunk.source_id == sid).all()]
        db.query(DocChunk).filter(DocChunk.source_id == sid).delete()
        db_row = db.get(DocSource, sid)
        if db_row:
            aid = aid or str(db_row.app_id or "")
            db.delete(db_row)
    _delete_fts_chunks(chunk_ids)
    folder = docs_dir(aid, sid)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def ingest_upload(
    *,
    data: bytes,
    filename: str,
    app_id: str,
    project_id: str = "",
    title: str = "",
    source_kind: str = "upload",
    source_url: str = "",
    feishu_bot_id: str = "",
) -> dict[str, Any]:
    aid = str(app_id or "").strip()
    if not aid:
        raise ValueError("app_id 不能为空")
    if not data:
        raise ValueError("文件为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")

    file_type = ingest.detect_file_type(filename)
    if not file_type:
        raise ValueError("仅支持 .md / .markdown / .pdf")

    content_hash = hashlib.sha256(data).hexdigest()
    source_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    display_title = str(title or "").strip() or Path(filename).stem or "未命名文档"

    ensure_fts()
    src_url = str(source_url or "").strip()

    with session_scope() as db:
        to_drop: list[DocSource] = []
        if src_url:
            to_drop.extend(
                db.query(DocSource).filter(DocSource.app_id == aid, DocSource.source_url == src_url).all()
            )
        to_drop.extend(
            db.query(DocSource).filter(DocSource.app_id == aid, DocSource.content_hash == content_hash).all()
        )
        seen: set[str] = set()
        for row in to_drop:
            if row.id in seen:
                continue
            seen.add(row.id)
            _purge_source_id(row.id, row.app_id or aid)

    folder = docs_dir(aid, source_id)
    folder.mkdir(parents=True, exist_ok=True)
    ext = "pdf" if file_type == "pdf" else "md"
    original_path = folder / f"original.{ext}"
    original_path.write_bytes(data)

    source = DocSource(
        id=source_id,
        app_id=aid,
        project_id=str(project_id or "").strip(),
        title=display_title,
        file_type=file_type,
        original_filename=str(filename or ""),
        content_hash=content_hash,
        chunk_count=0,
        status="parsing",
        error_msg="",
        file_size=len(data),
        source_kind=str(source_kind or "upload").strip() or "upload",
        source_url=src_url,
        feishu_bot_id=str(feishu_bot_id or "").strip(),
        created_at=now,
        updated_at=now,
    )

    try:
        body = ingest.extract_text(file_type, data)
        chunks = ingest.chunk_document(file_type, body)
        if not chunks:
            if file_type == "pdf":
                raise ValueError(
                    "未能从 PDF 提取正文：多为扫描件/纯图片 PDF（无文字层）。"
                    "请导出带可选文字的 PDF，或转为 Markdown 上传。"
                )
            raise ValueError("未能从文件中提取正文")
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        with session_scope() as db:
            source.status = "error"
            source.error_msg = str(exc)
            source.updated_at = int(time.time())
            db.add(source)
        raise ValueError(str(exc)) from exc

    fts_rows: list[dict[str, str]] = []
    with session_scope() as db:
        db.add(source)
        for i, ch in enumerate(chunks):
            cid = uuid.uuid4().hex[:12]
            row = DocChunk(
                id=cid,
                source_id=source_id,
                chunk_index=i,
                heading=str(ch.get("heading") or ""),
                text=str(ch.get("text") or ""),
                char_start=int(ch.get("char_start") or 0),
                char_end=int(ch.get("char_end") or 0),
            )
            db.add(row)
            fts_rows.append({
                "chunk_id": cid,
                "source_id": source_id,
                "app_id": aid,
                "title": display_title,
                "heading": row.heading or "",
                "body": row.text or "",
            })
        source.chunk_count = len(chunks)
        source.status = "ok"
        source.error_msg = ""
        source.updated_at = int(time.time())

    _insert_fts_rows(fts_rows)
    _embed_source_chunks(source_id)
    out = get_source(source_id)
    return out or _to_public_source(source)


def _embed_source_chunks(source_id: str) -> None:
    sid = str(source_id or "").strip()
    if not sid:
        return
    with session_scope() as db:
        rows = (
            db.query(DocChunk)
            .filter(DocChunk.source_id == sid)
            .order_by(DocChunk.chunk_index.asc())
            .all()
        )
        if not rows:
            return
        texts = [str(r.text or "") for r in rows]
        ids = [r.id for r in rows]
    vecs = dembed.embed_texts(texts)
    if not vecs:
        return
    with session_scope() as db:
        for cid, vec in zip(ids, vecs):
            if not vec:
                continue
            row = db.get(DocChunk, cid)
            if row:
                row.embedding_json = vec


def _insert_fts_rows(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    ensure_fts()
    with engine.connect() as conn:
        for row in rows:
            conn.execute(
                text(
                    """
                    INSERT INTO doc_chunks_fts(chunk_id, source_id, app_id, title, heading, body)
                    VALUES (:chunk_id, :source_id, :app_id, :title, :heading, :body)
                    """
                ),
                row,
            )
        conn.commit()


def _query_tokens(raw: str) -> set[str]:
    tokens = re.findall(r"[\w\u4e00-\u9fff]{2,}", str(raw or "").lower())
    stop = {
        "点击", "输入", "页面", "步骤", "进行", "成功", "失败", "登录", "打开", "关闭",
        "测试", "用例", "操作", "验证", "检查", "当前", "屏幕", "应用",
    }
    return {t for t in tokens if t not in stop}


def _normalize_match_text(text: str) -> str:
    body = normalize_doc_text(str(text or "")).lower()
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", body)


def _overlap_score(query: str, text: str) -> float:
    qtoks = _query_tokens(query)
    if not qtoks:
        return 0.0
    blob = _normalize_match_text(text)
    return sum(1 for t in qtoks if t in blob) / len(qtoks)


def _fts_query(raw: str) -> str:
    tokens = [t for t in re.split(r"\s+", str(raw or "").strip()) if t]
    if not tokens:
        return ""
    parts: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        t = str(term or "").strip()
        if not t or t in seen:
            return
        seen.add(t)
        safe = t.replace('"', '""')
        parts.append(f'"{safe}"*')

    for tok in tokens:
        _add(tok)
        # PDF 正文可能是「验 证 码」
        if re.fullmatch(r"[\u4e00-\u9fff]+", tok):
            _add(" ".join(tok))
            if len(tok) >= 2:
                for i in range(len(tok) - 1):
                    _add(tok[i : i + 2])
    return " OR ".join(parts)


def search_documents(
    *,
    q: str,
    app_id: str,
    limit: int = 20,
    query_vec: list[float] | None = None,
    use_query_embed: bool = False,
) -> list[dict[str, Any]]:
    aid = str(app_id or "").strip()
    query = _fts_query(q)
    if not aid or not query:
        return []
    ensure_fts()
    lim = max(1, min(100, int(limit or 20)))
    fetch_lim = min(100, max(lim * 3, lim))
    sql = text(
        """
        SELECT
            chunk_id,
            source_id,
            app_id,
            title,
            heading,
            snippet(doc_chunks_fts, 4, '', '', '…', 48) AS snippet,
            bm25(doc_chunks_fts) AS rank
        FROM doc_chunks_fts
        WHERE doc_chunks_fts MATCH :q AND app_id = :app_id
        ORDER BY rank
        LIMIT :lim
        """
    )
    with engine.connect() as conn:
        fts_rows = conn.execute(sql, {"q": query, "app_id": aid, "lim": fetch_lim}).mappings().all()
    rows = list(fts_rows)
    seen_chunks = {str(r.get("chunk_id") or "") for r in rows}
    for row in _overlap_search_rows(q=q, app_id=aid, limit=fetch_lim):
        cid = str(row.get("chunk_id") or "")
        if cid and cid not in seen_chunks:
            rows.append(row)
            seen_chunks.add(cid)

    qvec = query_vec
    if use_query_embed and not qvec:
        qvec = dembed.embed_query(q)

    chunk_ids = [str(row.get("chunk_id") or "") for row in rows]
    embed_map = _chunk_embeddings(chunk_ids)

    out: list[dict[str, Any]] = []
    for row in rows:
        chunk_id = str(row.get("chunk_id") or "")
        detail = _chunk_detail(chunk_id)
        body = detail.get("text", "") if detail else ""
        bm25 = float(row.get("rank") or 0)
        overlap = _overlap_score(q, f"{row.get('title') or ''} {row.get('heading') or ''} {body}")
        vec_score = 0.0
        if qvec:
            emb = embed_map.get(chunk_id) or []
            if emb:
                vec_score = dembed.cosine(qvec, emb)
        hybrid = overlap * 10.0 - bm25 * 0.001 + vec_score * 5.0
        out.append({
            "chunk_id": chunk_id,
            "source_id": str(row.get("source_id") or ""),
            "app_id": str(row.get("app_id") or ""),
            "title": str(row.get("title") or ""),
            "heading": str(row.get("heading") or ""),
            "snippet": str(row.get("snippet") or ""),
            "rank": bm25,
            "vector_score": vec_score,
            "hybrid_score": hybrid,
            "text": body,
        })
    out.sort(key=lambda x: float(x.get("hybrid_score") or 0), reverse=True)
    return out[:lim]


def _overlap_search_rows(
    *,
    q: str,
    app_id: str,
    limit: int,
) -> list[Any]:
    """FTS 无命中时按字面重叠扫分片（弥补 PDF 抽字异常 / 旧索引未归一化）。"""
    aid = str(app_id or "").strip()
    if not aid or not str(q or "").strip():
        return []
    lim = max(1, min(100, int(limit or 20)))
    pairs: list[tuple[str, str, str, str, str]] = []
    with session_scope() as db:
        rows = (
            db.query(DocChunk, DocSource)
            .join(DocSource, DocSource.id == DocChunk.source_id)
            .filter(DocSource.app_id == aid, DocSource.status == "ok")
            .order_by(DocSource.updated_at.desc(), DocChunk.chunk_index.asc())
            .limit(500)
            .all()
        )
        for chunk, source in rows:
            pairs.append((
                str(chunk.id or ""),
                str(chunk.source_id or ""),
                str(source.title or ""),
                str(chunk.heading or ""),
                str(chunk.text or ""),
            ))
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk_id, source_id, title, heading, body in pairs:
        blob = f"{title} {heading} {body}"
        overlap = _overlap_score(q, blob)
        if overlap <= 0:
            continue
        scored.append((overlap, {
            "chunk_id": chunk_id,
            "source_id": source_id,
            "app_id": aid,
            "title": title,
            "heading": heading,
            "snippet": body[:120],
            "rank": 0.0,
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in scored[:lim]]


def _chunk_embeddings(chunk_ids: list[str]) -> dict[str, list[float]]:
    ids = [str(x or "").strip() for x in chunk_ids if str(x or "").strip()]
    if not ids:
        return {}
    out: dict[str, list[float]] = {}
    with session_scope() as db:
        rows = db.query(DocChunk).filter(DocChunk.id.in_(ids)).all()
        for row in rows:
            vec = row.embedding_json
            if isinstance(vec, list) and vec:
                out[str(row.id)] = [float(x) for x in vec]
    return out


def _chunk_detail(chunk_id: str) -> dict[str, Any] | None:
    cid = str(chunk_id or "").strip()
    if not cid:
        return None
    with session_scope() as db:
        row = db.get(DocChunk, cid)
        return _to_public_chunk(row) if row else None
