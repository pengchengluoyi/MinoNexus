"""文档解析与分片（离线 ingest）。执行期不调用。"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Any

from mino_nexus.services.doc_text_norm import normalize_doc_text

MAX_CHUNK_CHARS = 2000


def detect_file_type(filename: str) -> str:
    name = str(filename or "").strip().lower()
    if name.endswith(".md") or name.endswith(".markdown"):
        return "md"
    if name.endswith(".pdf"):
        return "pdf"
    return ""


def extract_text(file_type: str, data: bytes) -> str:
    ft = str(file_type or "").strip().lower()
    if ft == "md":
        return normalize_doc_text(data.decode("utf-8", errors="replace"))
    if ft == "pdf":
        return _extract_pdf_text(data)
    raise ValueError(f"不支持的文件类型: {file_type or 'unknown'}")


def chunk_document(file_type: str, text: str) -> list[dict[str, Any]]:
    body = str(text or "")
    if not body.strip():
        return []
    ft = str(file_type or "").strip().lower()
    if ft == "md":
        sections = _split_markdown_sections(body)
    else:
        sections = [{"heading": "", "text": body, "char_start": 0, "char_end": len(body)}]
    out: list[dict[str, Any]] = []
    for sec in sections:
        for piece in _split_by_size(sec["text"], MAX_CHUNK_CHARS):
            out.append({
                "heading": sec.get("heading") or "",
                "text": piece["text"],
                "char_start": int(sec.get("char_start", 0)) + int(piece["offset"]),
                "char_end": int(sec.get("char_start", 0)) + int(piece["offset"]) + len(piece["text"]),
            })
    return out


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "PDF 解析需要 pypdf：请在 Nexus 环境执行 pip install -e . 或 pip install 'pypdf>=5.0'"
        ) from exc
    reader = PdfReader(BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    raw = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return _collapse_cjk_spaces(normalize_doc_text(raw))


def _collapse_cjk_spaces(text: str) -> str:
    """pypdf 常在汉字间插入空格。"""
    body = str(text or "")
    if not body:
        return ""
    body = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    return body.strip()


def _split_markdown_sections(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    sections: list[dict[str, Any]] = []
    heading = ""
    buf: list[str] = []
    start = 0
    pos = 0

    def flush() -> None:
        nonlocal start
        body = "".join(buf).strip()
        if body:
            sections.append({
                "heading": heading,
                "text": body,
                "char_start": start,
                "char_end": start + len(body),
            })
        buf.clear()

    for line in lines:
        if re.match(r"^#{1,6}\s+", line):
            flush()
            heading = re.sub(r"^#{1,6}\s+", "", line).strip()
            start = pos + len(line)
            pos += len(line)
            continue
        buf.append(line)
        pos += len(line)
    flush()
    if sections:
        return sections
    return [{"heading": "", "text": text.strip(), "char_start": 0, "char_end": len(text)}]


def _split_by_size(text: str, max_chars: int) -> list[dict[str, Any]]:
    body = str(text or "")
    if len(body) <= max_chars:
        return [{"text": body, "offset": 0}]
    out: list[dict[str, Any]] = []
    start = 0
    while start < len(body):
        end = min(len(body), start + max_chars)
        if end < len(body):
            split_at = body.rfind("\n\n", start, end)
            if split_at <= start:
                split_at = body.rfind("\n", start, end)
            if split_at <= start:
                split_at = end
            end = split_at
        piece = body[start:end].strip()
        if piece:
            out.append({"text": piece, "offset": start})
        start = end if end > start else start + max_chars
    return out
