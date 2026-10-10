"""MinoCard：渠道无关结构，渲染成微信文本或 Workbench 用的 JSON。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.ai.prompt_repo import get_card_template

_TOKEN = re.compile(r"\{([^{}]+)\}")


def _lookup(bag: dict[str, Any], path: str) -> str:
    cur: Any = bag
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    if cur is None:
        return ""
    return str(cur)


def _fill(text: str, bag: dict[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        return _lookup(bag, match.group(1)) or match.group(0)

    return _TOKEN.sub(repl, str(text or ""))


def build_card(
    *,
    template_id: str,
    title: str,
    summary: str,
    data: Any = None,
    intent: str = "",
    steps: list[dict[str, Any]] | None = None,
    status: str = "done",
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    spec = get_card_template(template_id) or {}
    bag = {"summary": summary, "intent": intent, "title": title}
    if isinstance(data, (str, int)) and str(data).strip():
        # 工具只挑出一个值时（如任务 id）data 是标量
        bag["task_id"] = str(data).strip()
    if isinstance(data, dict):
        bag.update({str(k): v for k, v in data.items()})
        task_id = data.get("task_id") or data.get("run_id") or data.get("id")
        if task_id:
            bag["task_id"] = task_id
    tone = str(spec.get("tone") or "info")
    blocks: list[dict[str, Any]] = []
    if intent:
        blocks.append({"type": "intent", "text": intent})
    if steps:
        blocks.append({"type": "trace", "steps": steps})
    kv = spec.get("kv") if isinstance(spec.get("kv"), list) else []
    items = []
    for pair in kv:
        if isinstance(pair, list) and len(pair) == 2:
            value = _fill(str(pair[1]), bag)
            if _TOKEN.search(value) or not value.strip():
                continue  # 占位符没有值：整行不显示，不要把 {task_id} 原样发给用户
            items.append([_fill(str(pair[0]), bag), value])
    if items:
        blocks.append({"type": "kv", "items": items})
    blocks.append({"type": "summary", "text": _fill(str(spec.get("summary") or "{summary}"), bag) or summary})
    return {
        "template": template_id,
        "status": status,
        "header": {"title": _fill(str(spec.get("title") or title), bag) or title, "tone": tone},
        "blocks": blocks,
        "actions": actions or [],
    }


_MARK = {"ok": "✅", "fail": "❌", "error": "❌", "ask": "❓"}


def render_wechat_text(card: dict[str, Any]) -> str:
    """微信消息框按 Markdown 样式显示：单个换行会被当成空格，空行才分段。
    所以段与段之间用空行，列表项用「- 」开头，不要靠单个换行分行。"""
    sections: list[str] = []
    title = str((card.get("header") or {}).get("title") or "").strip()
    if title and card.get("template") != "answer":
        sections.append(f"**{title}**")
    for block in card.get("blocks") or []:
        kind = block.get("type")
        if kind in ("intent", "summary") and block.get("text"):
            sections.append(str(block["text"]).strip())
        elif kind == "kv":
            rows = [f"- {p[0]}：{p[1]}" for p in block.get("items") or []
                    if isinstance(p, list) and len(p) == 2 and p[1]]
            if rows:
                sections.append("\n".join(rows))
        elif kind == "trace":
            rows = [f"- {_MARK.get(str(s.get('status')), '•')} {s.get('label') or s.get('tool')}"
                    for s in block.get("steps") or []]
            if rows:
                sections.append("\n".join(rows))
    labels = [str(a.get("label") or "") for a in (card.get("actions") or []) if a.get("label")]
    if labels:
        sections.append("可以回复：" + " / ".join(labels))
    return "\n\n".join(x for x in sections if x)[:1800]
