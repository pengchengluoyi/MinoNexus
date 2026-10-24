"""类型化意图判断：用仓库现有 LLM/VLM 顶替 Jev / Laya 的 choice / noul / score。

规划原意是接 TypeSafe Jev 或自托管 Laya（``POST /v1/systemone``）。本仓不引入新的
模型运行时，也不做本地决策模型（AGENTS.md §1），所以这里只保留同一套线协议语义：

- 输入：问题 + ≤20 个候选 + 界面上下文（可选截图）；
- 输出：``{"choice": 候选 id | "noul", "score": 0..1, "reason": 短句}``；
- 两级分流：先选域（认证/电商/社交/社区/银行/通用），再选意图。

判断归模型，展开与派发归程序（``loop/event_interpreter.py``）。
"""
from __future__ import annotations

from typing import Any

SCHEMA = "mino.intent_choice.v1"
MAX_OPTIONS = 20


def _norm_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


def _parse_choice(payload: dict[str, Any] | None, allowed: set[str]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    choice = str(payload.get("choice") or payload.get("answer") or "").strip()
    if not choice:
        return None
    if choice.lower() in ("noul", "none", "unknown", "null"):
        return {"schema": SCHEMA, "choice": "noul", "score": _norm_score(payload.get("score")),
                "reason": str(payload.get("reason") or "")[:240]}
    if choice not in allowed:
        # 模型返回了不在候选里的 id：不猜，按 noul 处理并保留原值供排查。
        return {"schema": SCHEMA, "choice": "noul", "score": 0.0,
                "reason": "invalid_choice:" + choice[:80]}
    return {"schema": SCHEMA, "choice": choice, "score": _norm_score(payload.get("score")),
            "reason": str(payload.get("reason") or "")[:240]}


def _ask(*, question: str, options: list[dict[str, Any]], context: str = "", domain: str = "",
         image_base64: str = "", image_mime: str = "image/jpeg",
         provider: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not options:
        return None, {"error": "no_options"}
    if len(options) > MAX_OPTIONS:
        options = options[:MAX_OPTIONS]
    if provider is None:
        from mino_nexus.platform.llm.client import resolve_regression_provider

        provider, gate = resolve_regression_provider()
        if not provider:
            return None, {"error": gate.get("reason") or "未配置可用的大模型", "enabled": False}
    lines = "\n".join(
        f"- {row.get('id')}｜{row.get('label') or row.get('id')}｜{row.get('description') or ''}"
        for row in options
    )
    slots = {
        "domain": domain or "（未指定，先按问题判断）",
        "question": question,
        "options": lines,
        "context": str(context or "")[:6000],
        "image_base64": image_base64,
        "image_mime": image_mime or "image/jpeg",
    }
    from mino_nexus.platform.llm.client import call_chat_text
    from mino_nexus.platform.llm.render import JobRenderError, render

    try:
        messages, job_meta = render("intent-choice", slots)
    except JobRenderError as exc:
        return None, {"error": str(exc)}
    call = job_meta.get("call") or {}
    from mino_nexus.platform.llm import dispatch_log

    token = dispatch_log.bind(job="intent-choice", prompt_version=job_meta.get("prompt_version"),
                              prompt_sha=job_meta.get("prompt_sha"))
    try:
        parsed, meta = call_chat_text(
            provider=provider,
            messages=messages,
            temperature=float(call.get("temperature", 0.0)),
            max_tokens=int(call.get("max_tokens", 400)),
            timeout_sec=int(call.get("timeout_sec", 45)),
            json_mode=True,
        )
    finally:
        dispatch_log.reset(token)
    allowed = {str(row.get("id") or "") for row in options}
    choice = _parse_choice(parsed, allowed)
    if choice is None:
        return None, meta or {"error": "unparseable_choice"}
    choice["meta"] = {k: meta.get(k) for k in ("model", "usage", "provider_id", "truncated", "salvaged") if k in (meta or {})}
    return choice, meta or {}


def choose(*, question: str, options: list[dict[str, Any]], context: str = "", domain: str = "",
           image_base64: str = "", image_mime: str = "image/jpeg",
           provider: dict[str, Any] | None = None) -> dict[str, Any]:
    """单层选择：候选 ≤20。没有 provider 时返回结构化失败，不伪造答案。"""
    choice, meta = _ask(question=question, options=options, context=context, domain=domain,
                        image_base64=image_base64, image_mime=image_mime, provider=provider)
    if choice is None:
        return {"schema": SCHEMA, "choice": "", "score": 0.0,
                "reason": str((meta or {}).get("error") or "模型未返回可用选择"),
                "error": str((meta or {}).get("error") or "unavailable"), "options_count": len(options)}
    return choice


def choose_domain(*, question: str, context: str = "", provider: dict[str, Any] | None = None) -> dict[str, Any]:
    from mino_nexus.knowledge.intent_graph import domains

    options = [{"id": row["id"], "label": row["label"],
                "description": f"{row.get('intent_count', 0)} 个意图"} for row in domains()]
    return choose(question=question, options=options, context=context, domain="domain",
                  provider=provider)


def choose_intent(*, question: str, context: str = "", app_id: str = "", domain: str = "",
                  image_base64: str = "", image_mime: str = "image/jpeg",
                  provider: dict[str, Any] | None = None) -> dict[str, Any]:
    """两级分流：先域再意图；调用方已指定 domain 时只做第二级。"""
    from mino_nexus.knowledge.intent_graph import intent_options

    dom = str(domain or "").strip()
    if not dom:
        first = choose_domain(question=question, context=context, provider=provider)
        if first.get("choice") in ("", "noul"):
            return {**first, "stage": "domain"}
        dom = str(first["choice"])
    options = intent_options(domain=dom, app_id=app_id, limit=MAX_OPTIONS)
    if not options:
        return {"schema": SCHEMA, "choice": "noul", "score": 0.0,
                "reason": f"域 {dom} 没有可用意图", "stage": "intent", "domain": dom}
    result = choose(question=question, options=options, context=context, domain=dom,
                    image_base64=image_base64, image_mime=image_mime, provider=provider)
    result["stage"] = "intent"
    result["domain"] = dom
    return result


def score_screen(*, question: str, context: str = "", app_id: str = "", domain: str = "",
                 image_base64: str = "", image_mime: str = "image/jpeg",
                 provider: dict[str, Any] | None = None) -> dict[str, Any]:
    """VLM 版：带截图做同一套类型化选择；没有图时退化为文本选择。"""
    return choose_intent(question=question, context=context, app_id=app_id, domain=domain,
                         image_base64=image_base64, image_mime=image_mime, provider=provider)


def describe() -> dict[str, Any]:
    return {"schema": SCHEMA, "max_options": MAX_OPTIONS,
            "answer_contract": {"choice": "候选 id | noul", "score": "0..1", "reason": "<=60 chars"},
            "stages": ["domain", "intent"]}


__all__ = ["MAX_OPTIONS", "SCHEMA", "choose", "choose_domain", "choose_intent", "describe", "score_screen"]
