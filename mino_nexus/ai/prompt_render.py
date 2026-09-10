"""从 llm_jobs 行渲染 OpenAI messages。"""
from __future__ import annotations

from typing import Any


class JobRenderError(ValueError):
    pass


def _block_enabled(block: dict[str, Any]) -> bool:
    return block.get("enabled") is not False


def _slot_text(block: dict[str, Any], slots: dict[str, str]) -> str:
    slot = block.get("slot")
    if slot:
        val = str(slots.get(str(slot)) or "").strip()
        if not val:
            val = str(block.get("empty_text") or "").strip()
        if not val and block.get("skip_if_empty"):
            return ""
        heading = str(block.get("heading") or "").strip()
        body = val
        if block.get("max_chars"):
            try:
                cap = int(block.get("max_chars") or 0)
                if cap > 0:
                    body = body[:cap]
            except (TypeError, ValueError):
                pass
        if not body and block.get("skip_if_empty"):
            return ""
        if heading:
            return f"{heading}\n{body}" if body else heading
        return body
    text = str(block.get("text") or "").strip()
    if block.get("max_chars"):
        try:
            cap = int(block.get("max_chars") or 0)
            if cap > 0:
                text = text[:cap]
        except (TypeError, ValueError):
            pass
    return text


def _render_side(blocks: list[dict[str, Any]], slots: dict[str, str]) -> str:
    parts: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if not _block_enabled(block):
            continue
        chunk = _slot_text(block, slots)
        if chunk:
            parts.append(chunk)
    return "\n\n".join(parts).strip()


def render_job(row: dict[str, Any], slots: dict[str, str]) -> tuple[list[dict], dict]:
    if not row:
        raise JobRenderError("job 不存在")
    if not row.get("enabled", True):
        raise JobRenderError(f"job 已禁用：{row.get('id')}")

    system = _render_side(list(row.get("system_blocks") or []), slots)
    user_text = _render_side(list(row.get("user_blocks") or []), slots)
    if not system.strip():
        raise JobRenderError(f"job {row.get('id')} system 为空")

    image_cfg = row.get("image") if isinstance(row.get("image"), dict) else {}
    img_slot = str(image_cfg.get("slot") or "image_base64").strip()
    img_b64 = str(slots.get(img_slot) or "").strip()
    img_mime = str(slots.get("image_mime") or "image/png").strip()

    user_content: list[dict[str, Any]] | str
    if img_b64:
        user_content = [{"type": "text", "text": user_text or "（无）"}]
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{img_mime};base64,{img_b64}"},
        })
    else:
        engine = str(row.get("engine") or "").strip()
        if not user_text.strip():
            if engine in ("text_chat", "json_chat"):
                user_content = "（无）"
            else:
                raise JobRenderError(f"job {row.get('id')} user 文本为空")
        else:
            user_content = user_text

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    meta = {
        "job_id": str(row.get("id") or ""),
        "role_id": str(row.get("role_id") or ""),
        "output_schema": str(row.get("output_schema") or ""),
        "call": dict(row.get("call") or {}),
        "prompt_version": int(row.get("prompt_version") or 1),
    }
    return messages, meta


def render(job_id: str, slots: dict[str, str]) -> tuple[list[dict], dict]:
    from mino_nexus.services.job_store import get_job

    row = get_job(job_id)
    if not row:
        raise JobRenderError(f"llm_jobs 里没有 {job_id}")
    return render_job(row, slots)
