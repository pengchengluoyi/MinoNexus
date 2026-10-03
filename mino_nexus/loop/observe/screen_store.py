"""把步骤截图落到数据目录，会话里只存 /static 引用。

base64 再被 session_log 裁成 `...(+N)` 后，界面会画出坏 JPEG。
"""
from __future__ import annotations

import base64
import hashlib

from mino_nexus.core.paths import data_dir


def _safe(raw: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(raw or ""))
    return text[:180] or "_"


def publish_screen(session_id: str, name: str, jpeg_b64: str) -> tuple[str, str]:
    """写入 uploads/session-thumbs。返回路径和 JPEG 字节的哈希。失败时两者都是空串。"""
    raw = str(jpeg_b64 or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    if not raw or "(+" in raw:
        return "", ""
    pad = (-len(raw)) % 4
    try:
        data = base64.b64decode(raw + ("=" * pad), validate=False)
    except Exception:
        return "", ""
    if not data:
        return "", ""
    folder = data_dir() / "uploads" / "session-thumbs" / _safe(session_id)
    folder.mkdir(parents=True, exist_ok=True)
    fname = f"{_safe(name)}.jpg"
    (folder / fname).write_bytes(data)
    digest = hashlib.sha1(data).hexdigest()[:16]
    return f"/static/session-thumbs/{_safe(session_id)}/{fname}", digest
