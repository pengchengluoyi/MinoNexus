"""账号隔离的 LiteLLM 向量调用；未配置/失败时显式退化到 FTS。"""
from __future__ import annotations
import math
import time
from typing import Any
from mino_nexus.core.log import SLog

TAG = "DocEmbed"
_MAX_CHARS = 2000
_BATCH = 16
# 确定配置错误短期停用；配置指纹改变立即恢复；429/网络错误不永久停用。
_disabled_until: dict[str, float] = {}


def _resolve_provider() -> dict[str, Any] | None:
    from mino_nexus.services.settings import get_ai_provider_credentials, find_case_execution_provider_id
    pid = find_case_execution_provider_id()
    if not pid:
        return None
    provider = get_ai_provider_credentials(pid)
    return provider if provider.get("configured") and provider.get("case_execution_use") else None


def embed_texts(texts: list[str], *, provider: dict[str, Any] | None = None) -> list[list[float]]:
    from mino_nexus.ai.model_sdk import sdk, connection, config_fingerprint, error_meta, model_info
    from mino_nexus.services.model_usage import begin_attempt, record_attempt
    from mino_nexus.services.settings import validate_provider_owner
    rows = [str(t or "").strip()[:_MAX_CHARS] for t in texts or []]
    if not rows:
        return []
    prov = provider or _resolve_provider()
    if prov is None:
        return [[] for _ in rows]
    validate_provider_owner(prov)
    model = str(prov.get("embedding_model") or "").strip()
    if not model:
        return [[] for _ in rows]
    fingerprint = str(prov.get("owner_user_id") or "") + ":" + config_fingerprint(prov)
    if _disabled_until.get(fingerprint, 0) > time.monotonic():
        return [[] for _ in rows]
    try:
        config = connection(prov, model=model)
        info = model_info(config["custom_llm_provider"], model)
        if info["sdk_registered"] and info["mode"] != "embedding":
            raise ValueError("所选模型不是向量模型")
    except Exception:
        SLog.w(TAG, "向量模型配置无效，文档检索退化为 FTS")
        return [[] for _ in rows]
    out = []
    for i in range(0, len(rows), _BATCH):
        response = None
        meta = {"model": model, "provider_id": prov.get("id"), "sdk_provider": config["custom_llm_provider"], "error": ""}
        started = time.monotonic()
        try:
            call_id = begin_attempt(provider=prov, model=model, kind="embedding", meta=meta)
        except Exception:
            SLog.w(TAG, "向量用量账本暂不可写，文档检索退化为 FTS；未提交请求")
            out.extend([[] for _ in rows[i:]])
            break
        try:
            result = sdk().embedding(**config, input=rows[i:i + _BATCH], timeout=60)
            response = result.model_dump(exclude_none=True)
            items = sorted(response.get("data") or [], key=lambda x: int(x["index"]))
            if len(items) != len(rows[i:i + _BATCH]) or [x.get("index") for x in items] != list(range(len(items))):
                raise ValueError("向量返回数量或索引不匹配")
            vectors = [[float(v) for v in x["embedding"]] for x in items]
            if not all(v and all(math.isfinite(n) for n in v) for v in vectors):
                raise ValueError("向量返回为空或非有限数字")
            out.extend(vectors)
            meta["http_status"] = 200
        except Exception as exc:
            meta.update(error_meta(exc, prov))
            out.extend([[] for _ in rows[i:i + _BATCH]])
            if not meta.get("retryable"):
                _disabled_until[fingerprint] = time.monotonic() + 300
            SLog.w(TAG, f"向量调用失败，文档检索退化为 FTS：{meta.get('error')}")
        meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        try:
            record_attempt(provider=prov, model=model, kind="embedding", meta=meta, response=response, call_id=call_id)
        except Exception as exc:
            SLog.w(TAG, f"向量用量账本写入失败：{type(exc).__name__}")
        if meta.get("error"):
            out.extend([[] for _ in rows[i + _BATCH:]])
            break
    return out

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
