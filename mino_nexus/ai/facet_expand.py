"""按项目需求让模型扩展账号维度字段（仅生成建议，落库仍走资源域）。"""
from __future__ import annotations

from typing import Any

import json

from mino_nexus.core.log import SLog
from mino_nexus.resources.account_facet_schema import (
    _sample_preconditions,
    merge_extensions,
    normalize_extension,
    normalize_extensions,
)
from mino_nexus.resources.resource_pool import DEFAULT_FACETS, FACET_LABELS, normalize_facets

TAG = "FacetExpand"
CORE_KEYS = frozenset(DEFAULT_FACETS.keys())


def ai_expand_facet_extensions(
    project_id: str,
    *,
    hint: str = "",
    existing: list[dict] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """根据用例前置/备注建议项目级扩展字段；不改动五维默认字段。"""
    pre = _sample_preconditions(project_id)
    if not pre and not str(hint or "").strip():
        return [], "没有用例前置可参考，请填写补充说明后再试"

    payload = {
        "project_id": project_id,
        "hint": str(hint or "").strip(),
        "existing_extension_keys": [str(x.get("key")) for x in normalize_extensions(existing or [])],
        "reserved_core_keys": sorted(CORE_KEYS),
        "sample_preconditions": pre[:25],
    }
    try:
        from mino_nexus.platform.llm.dispatch_log import bind, reset
        from mino_nexus.platform.llm.client import call_chat_text, resolve_regression_provider
        from mino_nexus.platform.llm.render import render
    except Exception as exc:
        return [], f"LLM 不可用: {exc}"

    provider, gate = resolve_regression_provider()
    if provider is None:
        return [], str((gate or {}).get("reason") or "未配置大模型")

    tok = bind(
        trigger="account_facet_expand",
        source="account_facet_schema",
        role="test-engineer",
        job="account-facet-expand",
        skill="run-case",
    )
    try:
        messages, prompt_meta = render(
            "account-facet-schema",
            {"user_payload": json.dumps(payload, ensure_ascii=False)},
        )
        call = prompt_meta.get("call") or {}
        raw, meta = call_chat_text(
            provider=provider,
            messages=messages,
            temperature=call.get("temperature", 0.1),
            max_tokens=int(call.get("max_tokens") or 900),
            timeout_sec=int(call.get("timeout_sec") or 45),
            json_mode=bool(call.get("json_mode")),
        )
    finally:
        reset(tok)

    if raw is None:
        return [], str((meta or {}).get("error") or "模型调用失败")

    data = raw if isinstance(raw, dict) else {}
    items = data.get("extensions") if isinstance(data.get("extensions"), list) else []
    seen = set(CORE_KEYS) | {str(x.get("key")) for x in normalize_extensions(existing or [])}
    normalized = []
    for item in items:
        row = normalize_extension({**item, "source": "ai"}, seen)
        if row:
            normalized.append(row)
    if not normalized:
        return [], "模型未建议新字段（可能现有五维已够用）"
    merged = merge_extensions(existing or [], normalized)
    SLog.i(TAG, f"ai expand project={project_id} +{len(normalized)} fields")
    return merged, ""
