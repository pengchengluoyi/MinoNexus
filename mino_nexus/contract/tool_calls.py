"""模型 tool-call 增量与决策字段的归一（协议形状，纯函数）。"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.S)
_VISUAL_ENVELOPE_KEYS = ("screen_layout", "vlm_hierarchy")
SIGNAL_DONE = "signal_done"
SIGNAL_GIVE_UP = "signal_give_up"
SIGNAL_SKIP = "signal_skip"
SIGNAL_ASK_HUMAN = "signal_ask_human"


def merge_decision_visual_fields(
    decision: dict[str, Any],
    *,
    content: str = "",
) -> dict[str, Any]:
    """tool call 决策与正文 JSON 中的 screen_layout / vlm_hierarchy 合并（正文补全 tool 缺项）。"""
    if not isinstance(decision, dict):
        return decision
    extra: Optional[dict[str, Any]] = None
    text = str(content or "").strip()
    if text:
        try:


            extra = _extract_first_json_object(text)
        except Exception:
            extra = None
    if isinstance(extra, dict):
        for key in _VISUAL_ENVELOPE_KEYS:
            val = extra.get(key)
            if not isinstance(val, dict) or not val:
                continue
            if not decision.get(key):
                decision[key] = val
        for key in ("milestones", "step_outcome"):
            if key in extra and extra.get(key) and not decision.get(key):
                decision[key] = extra.get(key)
    return decision


def decision_from_tool_calls(
    tool_calls: Any,
    *,
    content: str = "",
) -> Optional[dict[str, Any]]:
    """把 OpenAI tool_calls 收成 decide JSON。只取第一个 call。"""
    call = _first_tool_call(tool_calls)
    if call is None:
        return None
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(fn.get("name") or "").strip()
    args = _args_of(call)
    envelope = _pop_visual_envelope(args)
    thought = str(args.pop("thought", "") or content or "").strip()
    remember = args.pop("remember", None)
    knowledge_ids = args.pop("knowledge_ids", None)
    args.pop("milestone_updates", None)
    milestones = args.pop("milestones", None)
    step_outcome = str(args.pop("step_outcome", "") or "").strip()
    prior_status = str(args.pop("prior_status", "") or "").strip().lower()
    out: dict[str, Any] = {
        "thought": thought,
        "status": "continue",
        "action": None,
        "expected_after": str(args.pop("expected_after", "") or ""),
        "remember": remember if isinstance(remember, list) else [],
        "knowledge_ids": knowledge_ids if isinstance(knowledge_ids, list) else [],
        "_tool_name": name,
    }
    if isinstance(milestones, list) and milestones:
        out["milestones"] = milestones
    if step_outcome:
        out["step_outcome"] = step_outcome
    if prior_status:
        out["prior_status"] = prior_status
    out.update(envelope)
    if name == SIGNAL_DONE:
        out["status"] = "done"
        return out
    if name == SIGNAL_GIVE_UP:
        out["status"] = "give_up"
        return out
    if name == SIGNAL_ASK_HUMAN:
        out["status"] = "give_up"
        out["thought"] = "V5 不支持人工输入能力"
        return out
    if name == SIGNAL_SKIP:
        reason = str(args.get("reason") or thought or "当前渠道无法执行本条用例").strip()
        out["status"] = "skip"
        out["thought"] = reason
        return out
    out["action"] = {"capability_id": name, "params": args}
    return out


def merge_tool_call_deltas(acc: list[dict[str, Any]], deltas: Any) -> None:
    """把流式 delta.tool_calls 拼进 acc（按 index）。"""
    rows = deltas if isinstance(deltas, list) else []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        try:
            idx = int(raw.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx < 0:
            idx = 0
        while len(acc) <= idx:
            acc.append({
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
        row = acc[idx]
        if raw.get("id"):
            row["id"] = str(raw.get("id") or "")
        if raw.get("type"):
            row["type"] = str(raw.get("type") or "function")
        fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        dest = row.setdefault("function", {"name": "", "arguments": ""})
        if fn.get("name"):
            name, existing = str(fn.get("name") or ""), str(dest.get("name") or "")
            dest["name"] = name if not existing or name.startswith(existing) else existing if existing.startswith(name) else existing + name
        if fn.get("arguments"):
            dest["arguments"] = str(dest.get("arguments") or "") + str(fn.get("arguments") or "")


def _extract_first_json_object(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 文本里抠出第一个完整 JSON 对象（容错 Markdown / 思考链外泄）。"""
    if not text:
        return None
    text = text.strip()
    # 直接尝试解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 剥掉 ```json ... ``` 包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except Exception:
            pass
    # 找第一个 { ... } 段
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except Exception:
        # 进一步用栈匹配抠出第一个平衡 JSON
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    snippet = text[start : i + 1]
                    try:
                        return json.loads(snippet)
                    except Exception:
                        start = -1
                        continue
        return None


def _pop_visual_envelope(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _VISUAL_ENVELOPE_KEYS:
        if key not in args:
            continue
        val = args.pop(key)
        if isinstance(val, dict) and val:
            out[key] = val
    return out


def _args_of(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_tool_call(tool_calls: Any) -> Optional[dict[str, Any]]:
    rows = tool_calls if isinstance(tool_calls, list) else []
    for row in rows:
        if isinstance(row, dict) and str((row.get("function") or {}).get("name") or "").strip():
            return row
    return None


from mino_nexus.contract.tool_calls import (  # noqa: F401,E402
    decision_from_tool_calls,
    merge_decision_visual_fields,
    merge_tool_call_deltas,
)
