"""文档向量：ingest 离线算 embedding；跑批用用例级 query 向量做点积，不调 embedding API。"""
from __future__ import annotations

import math
import os
from typing import Any

import requests

from mino_nexus.core.log import SLog

TAG = "DocEmbed"

# provider id → 默认 embedding 模型（可被 settings.embedding_model / MINO_EMBEDDING_MODEL 覆盖）
_EMBED_MODEL: dict[str, str] = {
    "openai": "text-embedding-3-small",
    "deepseek": "text-embedding-3-small",
    "qwen": "text-embedding-v3",
    "volcengine": "doubao-embedding",
}
# 火山方舟账号差异大，按序尝试；需在控制台开通对应模型或填接入点 ep-…
_VOLCENGINE_EMBED_FALLBACKS = (
    "doubao-embedding",
    "doubao-embedding-large-text-240915",
    "doubao-embedding-text-240715",
)
_MAX_CHARS = 2000
_BATCH = 16
_disabled_providers: set[str] = set()
_warned_providers: set[str] = set()


def _resolve_provider() -> dict[str, Any] | None:
    from mino_nexus.services.settings import get_ai_provider_credentials
    from mino_nexus.services.settings_store import find_case_execution_provider_id

    pid = find_case_execution_provider_id()
    if not pid:
        return None
    cred = get_ai_provider_credentials(pid)
    if not cred.get("configured") or not cred.get("case_execution_use"):
        return None
    api_type = str(cred.get("api_type") or "").lower()
    if api_type in {"anthropic", "gemini"}:
        return None
    if not str(cred.get("base_url") or "").strip() or not str(cred.get("api_key") or "").strip():
        return None
    return cred


def _embedding_candidates(provider: dict[str, Any]) -> list[str]:
    pid = str(provider.get("id") or "openai").lower()
    out: list[str] = []
    for raw in (
        str(provider.get("embedding_model") or "").strip(),
        os.environ.get("MINO_EMBEDDING_MODEL", "").strip(),
        _EMBED_MODEL.get(pid) or "",
    ):
        if raw and raw not in out:
            out.append(raw)
    if pid == "volcengine":
        for model in _VOLCENGINE_EMBED_FALLBACKS:
            if model not in out:
                out.append(model)
    if not out:
        out.append("text-embedding-3-small")
    return out


def _disable_provider(provider: dict[str, Any], models: list[str], detail: str) -> None:
    pid = str(provider.get("id") or "").strip() or "unknown"
    _disabled_providers.add(pid)
    if pid in _warned_providers:
        return
    _warned_providers.add(pid)
    SLog.w(
        TAG,
        f"embedding 已停用 provider={pid}（{detail}），文档库退化为 FTS。"
        f" 可在 provider 配置 embedding_model 或设 MINO_EMBEDDING_MODEL"
        f"（火山方舟常用接入点 ep-… 或 doubao-embedding）。已尝试: {', '.join(models[:4])}",
    )


def _post_embeddings(
    *,
    base: str,
    key: str,
    model: str,
    batch: list[str],
) -> tuple[bool, list[list[float]], str]:
    try:
        resp = requests.post(
            f"{base}/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "input": batch},
            timeout=60,
        )
        data = resp.json()
        if resp.status_code >= 400 or "data" not in data:
            err = data.get("error") if isinstance(data, dict) else data
            msg = ""
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("code") or err)
            return False, [], msg or str(data)[:200]
        items = sorted(data.get("data") or [], key=lambda x: int(x.get("index") or 0))
        vecs: list[list[float]] = []
        for item in items:
            vec = item.get("embedding")
            vecs.append([float(x) for x in vec] if isinstance(vec, list) else [])
        while len(vecs) < len(batch):
            vecs.append([])
        return True, vecs, ""
    except Exception as exc:
        return False, [], str(exc)


def embed_texts(texts: list[str], *, provider: dict[str, Any] | None = None) -> list[list[float]]:
    """调用 OpenAI-compatible /embeddings。失败返回空向量（与 texts 等长）。"""
    rows = [str(t or "").strip()[:_MAX_CHARS] for t in (texts or [])]
    if not rows:
        return []
    prov = provider or _resolve_provider()
    if prov is None:
        return [[] for _ in rows]
    pid = str(prov.get("id") or "").strip()
    if pid in _disabled_providers:
        return [[] for _ in rows]

    base = str(prov.get("base_url") or "").rstrip("/")
    key = str(prov.get("api_key") or "")
    models = _embedding_candidates(prov)
    out: list[list[float]] = []
    active_model = models[0]

    for i in range(0, len(rows), _BATCH):
        batch = rows[i : i + _BATCH]
        vecs: list[list[float]] = []
        last_err = ""
        for model in models:
            ok, vecs, last_err = _post_embeddings(base=base, key=key, model=model, batch=batch)
            if ok and any(vecs):
                active_model = model
                models = [model] + [m for m in models if m != model]
                break
        if not vecs or not any(vecs):
            _disable_provider(prov, models, last_err or "no embedding returned")
            out.extend([[] for _ in batch])
            if pid in _disabled_providers:
                out.extend([[] for _ in rows[i + len(batch) :]])
                return out[: len(rows)]
            continue
        out.extend(vecs)
        if active_model != models[0] and pid not in _warned_providers:
            _warned_providers.add(pid)
            SLog.i(TAG, f"embedding 使用模型 {active_model}（provider={pid}）")

    return out[: len(rows)]


def embed_query(text: str) -> list[float]:
    rows = embed_texts([str(text or "").strip()[:_MAX_CHARS]])
    return rows[0] if rows else []


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def bootstrap_case_doc_query(*, case: dict[str, Any], app_id: str = "") -> list[float]:
    """每用例 1 次 embedding，供跑批向量 rerank（不再逐步调 API）。"""
    if not str(app_id or "").strip():
        return []
    parts = [
        str(case.get("name") or "").strip(),
        str(case.get("precondition") or "").strip(),
        str(case.get("steps_raw") or "").strip()[:1200],
        str(case.get("success_criteria") or "").strip(),
    ]
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        return []
    return embed_query(text)
