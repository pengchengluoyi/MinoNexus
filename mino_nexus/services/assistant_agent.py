"""一句话 → assistant-route → 工具 → 卡片。最多 4 轮。"""
from __future__ import annotations

import json
from typing import Any

from mino_nexus.ai.prompt_render import render
from mino_nexus.services import assistant_store as store
from mino_nexus.services.assistant_cards import build_card, render_wechat_text
from mino_nexus.services.assistant_tools import ToolOutcome, openai_tools, run_tool, tool_by_function_name

MAX_STEPS = 4


def _persona(profile: dict[str, Any]) -> str:
    custom = str(profile.get("persona") or "").strip()
    if custom:
        return custom
    from mino_nexus.ai.prompt_repo import prompts_dir

    path = prompts_dir() / "assistant" / "persona" / "default.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return "你是 Mino 的测试助手。"


def _profile_text(profile: dict[str, Any]) -> str:
    parts = []
    if profile.get("default_app_id"):
        parts.append(f"默认应用 {profile.get('default_app_id')}")
    if profile.get("default_sn"):
        parts.append(f"默认设备 {profile.get('default_sn')}")
    return "；".join(parts)


def _history_text(turns: list[dict[str, Any]] | None) -> str:
    lines = []
    for row in turns or []:
        role = "用户" if row.get("role") == "user" else "助手"
        text = str(row.get("text") or "").strip()
        if text:
            lines.append(f"{role}：{text[:500]}")
    return "\n".join(lines)


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _call_model(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    from mino_nexus.ai import dispatch_log as dispatch
    from mino_nexus.ai.llm_client import call_chat_plain
    from mino_nexus.ai.llm_client import resolve_regression_provider

    provider, gate = resolve_regression_provider()
    if not provider:
        reason = (gate or {}).get("reason") or "没有可用的大模型"
        return "", [], {"error": reason}
    token = dispatch.bind(job="assistant-route", trigger="im", source="assistant", role="assistant")
    try:
        text, meta = call_chat_plain(
            provider=provider,
            messages=messages,
            temperature=0.1,
            max_tokens=800,
            timeout_sec=60,
            # 非流式：流式响应里多数服务商不带 usage，会让会话记录的 token 全是 0
            extra_payload={"tools": openai_tools(), "tool_choice": "auto", "stream": False},
        )
    finally:
        dispatch.reset(token)
    calls = meta.get("tool_calls") if isinstance(meta.get("tool_calls"), list) else []
    return text or "", calls, meta


def handle_utterance(
    *,
    user_id: str,
    text: str,
    recent_turns: list[dict[str, Any]] | None = None,
    channel: str = "",
    chat_id: str = "",
    node_id: str = "",
    reply_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = store.get_profile(user_id)
    messages, _meta = render("assistant-route", {
        "persona_block": _persona(profile),
        "user_profile": _profile_text(profile),
        "recent_turns": _history_text(recent_turns),
        "utterance": text,
    })
    steps: list[dict[str, Any]] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    last_outcome: ToolOutcome | None = None
    answer = ""
    for _ in range(MAX_STEPS):
        reply, calls, meta = _call_model(messages)
        usage["prompt_tokens"] += int(meta.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(meta.get("completion_tokens") or 0)
        if meta.get("error") and not reply and not calls:
            answer = "大模型暂时不可用。"
            break
        if not calls:
            answer = reply or "我没能理解这句话。"
            break
        messages.append({"role": "assistant", "content": reply or "", "tool_calls": calls})
        done = False
        for call in calls:
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            name = str(fn.get("name") or "")
            args = _parse_args(fn.get("arguments"))
            if name == "reply":
                answer = str(args.get("text") or reply or "")
                done = True
                break
            spec = tool_by_function_name(name)
            if spec is None:
                messages.append({"role": "tool", "tool_call_id": call.get("id") or name, "content": "没有这个工具"})
                continue
            outcome = run_tool(spec, args, user_id=user_id, profile=profile)
            last_outcome = outcome
            steps.append({
                "tool": spec.get("id"),
                "label": spec.get("title") or spec.get("id"),
                "status": "ok" if outcome.ok else ("ask" if outcome.ask else "fail"),
                "ms": outcome.elapsed_ms,
                "args_brief": json.dumps(args, ensure_ascii=False)[:180],
            })
            if outcome.ask:
                answer = outcome.ask
                done = True
                break
            if spec.get("id") == "case.run" and outcome.ok and isinstance(outcome.data, (str, dict)):
                task_id = outcome.data if isinstance(outcome.data, str) else (
                    outcome.data.get("task_id") or outcome.data.get("run_id") or ""
                )
                if task_id and channel and chat_id:
                    store.add_subscription({
                        "user_id": user_id,
                        "kind": "run_done",
                        "target_id": str(task_id),
                        "channel": channel,
                        "chat_id": chat_id,
                        "node_id": node_id,
                        "reply_ctx": reply_ctx or {},
                    })
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or name,
                "content": json.dumps({"ok": outcome.ok, "summary": outcome.summary, "data": outcome.data}, ensure_ascii=False)[:4000],
            })
        if done:
            break
    else:
        answer = answer or "步骤太多，先停在这里。"
    if not answer and last_outcome is not None:
        answer = last_outcome.summary
    template = "answer"
    if last_outcome and last_outcome.tool_id:
        spec = tool_by_function_name(str(last_outcome.tool_id).replace(".", "_")) or {}
        template = str(spec.get("card") or "answer")
        if last_outcome.ask or not last_outcome.ok:
            template = "answer"  # 没真正执行成功就别套「已开始」之类的卡片
    card = build_card(
        template_id=template,
        title="助手",
        summary=answer,
        data=last_outcome.data if last_outcome else None,
        intent="",  # 意图只进会话记录；卡片里已有调用轨迹，再写一遍会重复
        steps=steps,
        status="done" if not last_outcome or last_outcome.ok or last_outcome.ask else "failed",
    )
    return {
        "answer": answer,
        "intent": " → ".join(str(s.get("label") or s.get("tool")) for s in steps),
        "text": render_wechat_text(card),
        "card": card,
        "steps": steps,
        "usage": usage,
    }
