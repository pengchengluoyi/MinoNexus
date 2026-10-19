"""AgentScope ChatModel 与 Nexus llm_client 回归账号对齐。"""
from __future__ import annotations

from typing import Any


def build_agentscope_chat_model():
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel
    from mino_nexus.ai.llm_client import resolve_regression_provider

    try:
        provider, gate = resolve_regression_provider()
    except (PermissionError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    if not provider:
        raise ValueError(str((gate or {}).get("reason") or "未配置模型"))
    api_key = str(provider.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("模型 API Key 未配置")
    cred = OpenAICredential(
        api_key=api_key,
        base_url=str(provider.get("base_url") or "").strip() or None,
    )
    return OpenAIChatModel(
        credential=cred,
        model=str(provider.get("model") or "gpt-4o-mini"),
        parameters=OpenAIChatModel.Parameters(temperature=0.2, max_tokens=4096),
        stream=False,
    )


def regression_provider_ready() -> tuple[bool, dict[str, Any]]:
    from mino_nexus.ai.llm_client import resolve_regression_provider

    try:
        provider, gate = resolve_regression_provider()
    except (PermissionError, ValueError) as exc:
        return False, {"reason": str(exc)}
    if not provider:
        return False, dict(gate or {})
    if not str(provider.get("api_key") or "").strip():
        return False, {"reason": "模型 API Key 未配置"}
    return True, {}
