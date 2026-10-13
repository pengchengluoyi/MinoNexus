"""一句话 → assistant-route → 工具 → 卡片。结果先行，追问可续接。"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from mino_nexus.ai.prompt_render import render
from mino_nexus.services import assistant_store as store
from mino_nexus.services.assistant_cards import build_card, render_wechat_text
from mino_nexus.services.assistant_tools import ToolOutcome, openai_tools, run_tool, tool_by_function_name


def _persona(profile: dict[str, Any]) -> str:
    custom = str(profile.get("persona") or "").strip()
    if custom:
        return custom
    from mino_nexus.ai.prompt_repo import prompts_dir

    path = prompts_dir() / "assistant" / "persona" / "default.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return "你是 Mino 的测试助手。"


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


def _started_summary(answer: str, task_id: str, steps: list[dict[str, Any]]) -> str:
    labels = {str(step.get("label") or step.get("tool") or "").strip() for step in steps}
    lines = []
    for raw_line in answer.splitlines():
        line = raw_line.strip()
        plain = line.strip("* ").strip()
        if plain == "已开始":
            continue
        trace = re.fullmatch(r"[-•]\s*[✅❌❓•]\s*(.+)", line)
        if trace and trace.group(1).strip() in labels:
            continue
        if task_id and re.fullmatch(rf"[-•]?\s*任务\s*[：:]\s*`?{re.escape(task_id)}`?", line):
            continue
        lines.append(raw_line)
    return "\n".join(lines).strip()


def _call_model(messages: list[dict[str, Any]], job_meta: dict | None = None) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    from mino_nexus.ai import dispatch_log as dispatch
    from mino_nexus.ai.llm_client import call_chat_plain
    from mino_nexus.ai.llm_client import resolve_regression_provider

    provider, gate = resolve_regression_provider()
    if not provider:
        reason = (gate or {}).get("reason") or "没有可用的大模型"
        return "", [], {"error": reason}
    job_meta = job_meta or {}
    call = job_meta.get("call") or {}
    token = dispatch.bind(job="assistant-route", trigger="im", source="assistant", role="assistant",
                          prompt_version=job_meta.get("prompt_version"), prompt_sha=job_meta.get("prompt_sha"), call_parameters=call)
    try:
        text, meta = call_chat_plain(
            provider=provider,
            messages=messages,
            temperature=float(call.get("temperature", 0.1)),
            max_tokens=int(call.get("max_tokens", 800)),
            timeout_sec=float(call.get("timeout_sec", 60)),
            max_attempts=1,
            on_text=dispatch.ctx().get("assistant_stream_callback"),
            extra_payload={"tools": openai_tools(), "tool_choice": "auto", "stream": bool(dispatch.ctx().get("assistant_stream_callback"))},
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
    inbound: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from mino_nexus.core.execution_user import require_user_id
    require_user_id(user_id)
    def check_binding() -> None:
        if channel:
            store.require_im_binding(user_id, channel, node_id, reply_ctx or {})
    check_binding()
    from mino_nexus.services import assistant_conversation as conversation
    from mino_nexus.services.assistant_runtime import route_control, continue_request, result_for
    origin = conversation.origin_for(channel, chat_id, node_id, reply_ctx or {})
    control = route_control(text, origin, inbound or {})
    if control is not None:
        return control
    pending = conversation.pending_for(origin, reply_to=str((inbound or {}).get("reply_to") or ""))
    if pending:
        question = pending.get("question") or {}
        candidates = question.get("candidates") or []
        chosen = None
        raw = text.strip()
        if raw.isdigit() and 0 < int(raw) <= len(candidates):
            chosen = candidates[int(raw) - 1]["value"]
        else:
            matches = [row for row in candidates if raw in (str(row["value"]), str(row["label"]), str(row["label"]).split("（", 1)[0])]
            if len(matches) == 1:
                chosen = matches[0]["value"]
        if chosen is not None:
            try:
                return continue_request(pending["id"], pending["revision"], {question["field"]: chosen}, origin=origin)
            except ValueError:
                return result_for(answer="这条追问已被处理或内容已变化，请查看最新请求。", origin=origin)
    profile = store.get_profile(user_id)
    messages, _meta = render("assistant-route", {
        "persona_block": _persona(profile),
        "recent_turns": _history_text(recent_turns),
        "utterance": text,
    })
    preferences = conversation.preferences(user_id)
    if preferences:
        messages.append({"role": "system", "content": json.dumps({"display_preferences": preferences}, ensure_ascii=False)})
    steps: list[dict[str, Any]] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    model_error = ""
    last_outcome: ToolOutcome | None = None
    answer = ""
    from mino_nexus.services.assistant_targets import named_target

    call_config = _meta.get("call") or {}
    deadline = time.monotonic() + max(1.0, float(call_config.get("total_timeout_sec", 300)))
    target = named_target(text)
    if target:
        messages.append({"role": "system", "content": json.dumps({"execution_target": target}, ensure_ascii=False)})
    # 查询轮数不代表任务复杂度；按墙钟预算和相同查询无新结果判断。
    seen: dict[str, str] = {}
    runtime_stop = ""

    def submit(spec_id: str, args: dict[str, Any]) -> dict[str, Any]:
        from mino_nexus.services.assistant_runtime import execute_request
        check_binding()
        request = conversation.create(spec_id, {**target, **args}, target, {}, origin, state="resolving")
        result = execute_request(request)
        result["usage"] = usage
        result["steps"] = steps + result.get("steps", [])
        return result

    while not answer:
        check_binding()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            runtime_stop = "deadline"
            break
        # 模型单次超时服从剩余预算；禁用调用层的隐藏重试。
        model_meta = {**_meta, "call": {**call_config, "timeout_sec": min(float(call_config.get("timeout_sec", 60)), remaining)}}
        reply, calls, meta = _call_model(messages, model_meta)
        usage["available"] = usage.get("available", True) and bool(meta.get("usage_available", not meta.get("stream")))
        usage["prompt_tokens"] += int(meta.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(meta.get("completion_tokens") or 0)
        if meta.get("error") and not reply and not calls:
            if time.monotonic() >= deadline:
                runtime_stop = "deadline"
            else:
                answer = "大模型暂时不可用。"
                model_error = str(meta["error"])
            break
        if not calls:
            answer = reply or "我没能理解这句话。"
            break
        if time.monotonic() >= deadline:
            runtime_stop = "deadline"
            break
        messages.append({"role": "assistant", "content": reply or "", "tool_calls": calls})
        done = False
        for call in calls:
            if time.monotonic() >= deadline:
                runtime_stop = "deadline"
                done = True
                break
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            name = str(fn.get("name") or "")
            args = _parse_args(fn.get("arguments"))
            if name == "reply":
                answer = str(args.get("text") or reply or "我没能理解这句话。")
                done = True
                break
            spec = tool_by_function_name(name)
            signature = json.dumps([spec.get("id") if spec else name, {**target, **args}], ensure_ascii=False, sort_keys=True)
            if spec is None:
                if signature in seen:
                    runtime_stop = "no_progress"
                    done = True
                    break
                seen[signature] = "unknown_tool"
                messages.append({"role": "tool", "tool_call_id": call.get("id") or name, "content": "没有这个工具"})
                continue
            check_binding()
            if spec.get("risk") == "write":
                return submit(spec["id"], args)
            outcome = run_tool(spec, args, user_id=user_id, target=target)
            last_outcome = outcome
            steps.append({
                "tool": spec.get("id"),
                "label": spec.get("title") or spec.get("id"),
                "status": "ok" if outcome.ok else ("ask" if outcome.ask else "fail"),
                "ms": outcome.elapsed_ms,
                "args_brief": json.dumps(args, ensure_ascii=False)[:180],
            })
            # 先保留真实失败/追问；不得用运行预算覆盖它们。
            if outcome.ask or not outcome.ok:
                answer = outcome.ask or outcome.summary
                done = True
                break
            if time.monotonic() >= deadline:
                runtime_stop = "deadline"
                done = True
                break
            if outcome.args.get("intent") == "execute_test":
                return submit("case.run", outcome.args)
            observed = json.dumps({"summary": outcome.summary, "data": outcome.data, "args": outcome.args},
                                  ensure_ascii=False, sort_keys=True, default=str)
            if seen.get(signature) == observed:
                runtime_stop = "no_progress"
                done = True
                break
            seen[signature] = observed
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or name,
                "content": json.dumps({"ok": outcome.ok, "summary": outcome.summary, "data": outcome.data}, ensure_ascii=False)[:4000],
            })
        if done:
            break
    if not answer and last_outcome is not None:
        answer = last_outcome.summary
    return result_for(answer=answer, outcome=last_outcome, origin=origin, steps=steps,
                      target=target, usage=usage, model_error=model_error, runtime_stop=runtime_stop)
