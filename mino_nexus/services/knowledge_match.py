"""知识检索：按 app_id / project_id 打分取前 N。port(nexus) 自上游 system_settings_service。

只做检索与渲染素材，不拼 prompt 文案（那是 ai/knowledge_hint 的事）。
归属 scope 全空一律返回空 —— 同表多项目，放行等于串味。
条目 app_ids 可写应用 id 或项目 id（项目级知识对该项目下各应用可见）。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 原始分 25 视为 100%；低于 40% 不注入 prompt，避免低相关知识误导模型。
SCORE_FULL = 25
USE_MIN_PCT = 40
SKIP_REASON = "匹配度过低，未注入本步，避免误导模型"

# 正文之外还想带进 prompt 的，只有真业务字段；审核/溯源元数据一律排除。
_CORE_KEYS = frozenset({
    "id", "title", "content", "category", "tags", "app_ids", "enabled",
    "source", "review_status", "account_id", "account_ident",
    "score", "match_pct", "used", "skip_reason",
    "origin_task_id", "origin_case_id", "playbook_slot", "question",
    "expert_note", "expert_aligned", "expert_conflicts", "aligned_count",
    "review_method", "review_decision", "reviewed_by", "review_reason",
    "review_confidence",
    "facet", "situation", "situation_fp", "bind",
    "proposal_kind", "conflicts_with", "superseded_by",
    "valid_from", "invalid_from", "source_ref",
    "updated_at", "extra", "extra_json", "tags_json", "app_ids_json",
})

_QUERY_STOP = frozenset({
    "点击", "输入", "勾选", "页面", "步骤", "进行", "成功", "失败",
    "登录", "打开", "关闭", "测试", "用例", "操作", "验证", "检查",
})


def match_pct(score: int) -> int:
    if score <= 0:
        return 0
    return max(1, min(100, round(score * 100 / SCORE_FULL)))


def body_text(item: dict[str, Any]) -> str:
    """正文 + 扩展字段，供匹配与注入，避免只截 content。"""
    parts: list[str] = []
    content = str(item.get("content") or "").strip()
    if content:
        parts.append(content)
    for key, val in item.items():
        if key in _CORE_KEYS or val in (None, "", [], {}):
            continue
        if isinstance(val, (dict, list)):
            try:
                rendered = json.dumps(val, ensure_ascii=False)
            except (TypeError, ValueError):
                rendered = str(val)
        else:
            rendered = str(val).strip()
        if rendered:
            parts.append(f"{key}: {rendered}")
    return "\n".join(parts)


def prompt_snippet(item: dict[str, Any], *, max_chars: int = 800) -> str:
    title = str(item.get("title") or "").strip() or str(item.get("id") or "")
    body = body_text(item).strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return f"「{title}」: {body}" if body else f"「{title}」"


def dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 id、同一标题、或标题+正文相同，只保留先出现的一条。"""
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    seen_fp: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        kid = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip()
        title_key = re.sub(r"\s+", "", title).casefold()
        body = re.sub(r"\s+", " ", str(row.get("content") or "")).strip()
        fp = f"{title}\n{body}"
        if kid and kid in seen_ids:
            continue
        if title_key and title_key in seen_titles:
            continue
        if title and fp in seen_fp:
            continue
        if kid:
            seen_ids.add(kid)
        if title_key:
            seen_titles.add(title_key)
        if title:
            seen_fp.add(fp)
        out.append(row)
    return out


def score_item(item: dict[str, Any], query: str) -> int:
    q = (query or "").strip().lower()
    if not q:
        return 0
    title = str(item.get("title") or "").lower()
    body = body_text(item).lower()
    category = str(item.get("category") or "").lower()
    tags = [str(t).lower() for t in (item.get("tags") or [])]
    score = 0
    if title and title in q:
        score += 14
    elif title:
        n = 4 if len(title) >= 4 else max(2, len(title))
        hits = 0
        step = max(1, n // 2)
        for i in range(0, max(0, len(title) - n + 1), step):
            gram = title[i:i + n]
            if gram and gram in q:
                hits += 1
        score += min(12, hits * 2)
    for tag in tags:
        if len(tag) >= 2 and tag in q:
            score += 6
    if category and len(category) >= 2 and category in q:
        score += 3
    for token in re.split(r"[\s,，、/]+", q):
        if len(token) < 2 or token in _QUERY_STOP:
            continue
        if len(token) < 3 and not re.search(r"[a-zA-Z]{3,}", token):
            continue
        local = 0
        if token in title:
            local += 8
        if token in body:
            local += 4
        if category and token in category:
            local += 5
        for tag in tags:
            if token in tag or tag in token:
                local += 6
        if local > 0:
            score += local
    return score


def _eligible(
    app_id: str = "",
    project_id: str = "",
    account_id: str = "",
    account_ident: str = "",
) -> list[dict[str, Any]]:
    """已审核 + 启用 + 有正文 + 属于本应用或本项目。"""
    from mino_nexus.services.knowledge_store import list_testing_knowledge

    aid = str(app_id or "").strip()
    pid = str(project_id or "").strip()
    if not aid and not pid:
        return []
    out: list[dict[str, Any]] = []
    for item in list_testing_knowledge(
        app_id=aid,
        project_id=pid,
        account_id=account_id,
        account_ident=account_ident,
    ):
        if item.get("enabled") is False:
            continue
        if str(item.get("review_status") or "") != "approved":
            continue
        # app_ids 为空视为未归属，不放行 —— 避免跨应用串味。
        if not [str(x).strip() for x in (item.get("app_ids") or []) if str(x).strip()]:
            continue
        if not body_text(item).strip():
            continue
        out.append(item)
    return out


def match_knowledge(
    query: str,
    *,
    app_id: str = "",
    project_id: str = "",
    scene: dict[str, Any] | None = None,
    limit: int = 3,
    categories: list[str] | None = None,
    exclude_categories: list[str] | None = None,
    account_id: str = "",
    account_ident: str = "",
) -> list[dict[str, Any]]:
    """混合检索：情境 + 绑定 + 字面。used=False 仍返回给日志，但不写入 prompt。"""
    from mino_nexus.services.knowledge_situation import route_knowledge

    return route_knowledge(
        query,
        app_id=app_id,
        project_id=project_id,
        scene=scene,
        limit=limit,
        categories=categories,
        exclude_categories=exclude_categories,
        account_id=account_id,
        account_ident=account_ident,
    )


def items_by_ids(
    ids: list[str],
    *,
    app_id: str = "",
    project_id: str = "",
    account_id: str = "",
    account_ident: str = "",
) -> list[dict[str, Any]]:
    """按 id 取已审核正文。未审 / 禁用 / 空正文 / 非本 scope 不返回。"""
    want = [str(x).strip() for x in (ids or []) if str(x).strip()]
    if not want:
        return []
    by_id = {
        str(item.get("id") or ""): item
        for item in _eligible(app_id, project_id, account_id, account_ident)
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kid in want:
        if kid in seen:
            continue
        item = by_id.get(kid)
        if item is None:
            continue
        seen.add(kid)
        out.append({**item, "used": True})
    return out


__all__ = [
    "SCORE_FULL",
    "USE_MIN_PCT",
    "SKIP_REASON",
    "match_pct",
    "body_text",
    "prompt_snippet",
    "dedupe",
    "score_item",
    "match_knowledge",
    "items_by_ids",
]
