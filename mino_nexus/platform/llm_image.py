"""喂给大模型的图片编码：JPEG 压缩，纯本地处理。"""
from __future__ import annotations
TAG = "LLMImage"

import base64
import io

from mino_nexus.core.log import SLog
from io import BytesIO

_LLM_JPEG_QUALITY = 82

def make_llm_jpeg(
    raw_b64: str,
    *,
    compress_ratio: float | None = None,
    quality: int = _LLM_JPEG_QUALITY,
) -> tuple[str, str]:
    """喂给大模型的图：转 JPEG。采集端已经按渠道压缩比缩过的图不再套 plan_compress_ratio。

    1.0 不缩小。点按仍用千分比，不依赖像素。
    返回 ``(jpeg_b64, image/jpeg)``；失败则 ``("", "")``，调用方回落原图。
    """
    if not raw_b64:
        return "", ""
    try:
        from PIL import Image

        blob = raw_b64.strip()
        if blob.startswith("data:") and "," in blob:
            blob = blob.split(",", 1)[1]
        img = Image.open(BytesIO(base64.b64decode(blob))).convert("RGB")
        w, h = img.size
        ratio = compress_ratio
        if ratio is None:
            from mino_nexus.platform.settings import llm_shrink_after_capture

            ratio = llm_shrink_after_capture()
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            ratio = 3.0
        if ratio < 1.0:
            ratio = 1.0
        if ratio > 1.0 and w > 0 and h > 0:
            img = img.resize(
                (max(1, int(round(w / ratio))), max(1, int(round(h / ratio)))),
                Image.Resampling.LANCZOS,
            )
        q = int(quality or _LLM_JPEG_QUALITY)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=max(40, min(90, q)))
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception as e:
        SLog.d(TAG, f"make_llm_jpeg failed: {e}")
        return "", ""
