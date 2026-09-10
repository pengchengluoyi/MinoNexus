"""知识目录 / 点名正文的组装。port(nexus) 自上游 agent_executor 的知识段。

分工：本模块只把检索结果拼成塞进 knowledge_hint 槽的文本；
检索本身在 services/knowledge_match，prompt 正文在 llm_jobs。
"""
from __future__ import annotations

import re
from typing import Any

_TOKEN_STOP = frozenset({
    "点击", "输入", "勾选", "页面", "步骤", "进行", "成功", "失败", "登录", "打开",
    "关闭", "测试", "用例", "操作", "验证", "检查", "进入", "以后", "之后", "然后",
    "可以", "需要", "当前", "屏幕", "应用", "如何", "怎么", "以及", "或者", "一个",
    "任意", "说明", "方式", "正确", "本应用", "请按", "实际", "界面", "补充",
})

_PATH_RE = re.compile(
    r"如何进入|怎么进|如何打开|怎么打开|入口|路径|操作方式|操作说明|导航|定位方式|步骤说明|怎么去",
    re.I,
)

_SPLIT_RE = re.compile(r"[\s,，、/\|;；:：\n\r\t.。！？\(\)（）\[\]【】]+")
_QUOTED_RE = re.compile(r"[「\"'“](.{1,24})[」\"'”]")


def _clip(text: str, max_chars: int) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) > max_chars:
        return t[:max_chars].rstrip() + "…"
    return t


def build_case_intent(
    *,
    case_name: str = "",
    goal: str = "",
    steps_text: str = "",
    precondition: str = "",
    success_criteria: str = "",
    open_checkpoints: list[str] | None = None,
) -> str:
    """稳定的用例意图文本，保证入口类知识能靠步骤/目标命中。"""
    parts: list[str] = []
    for bit in (
        _clip(case_name, 200),
        _clip(goal, 400),
        _clip(steps_text, 1200),
        _clip(precondition, 300),
    ):
        if bit:
            parts.append(bit)
    open_lines = [str(x).strip() for x in (open_checkpoints or []) if str(x).strip()]
    if open_lines:
        parts.append(_clip("\n".join(open_lines), 600))
    sc = _clip(success_criteria, 300)
    if sc:
        parts.append(sc)
    return "\n".join(parts).strip()


def build_step_focus(cursor) -> str:
    """当前步骤焦点：prep / do / check 统一抽出「此刻在干什么」。"""
    if cursor is None:
        return ""
    phase = str(getattr(cursor, "phase", "") or "").strip()
    if phase == "prep":
        pre = _clip(str(getattr(cursor, "precondition", "") or "").strip(), 400)
        return f"当前前置：{pre}" if pre else "当前：前置检查"
    cur = cursor.current() if hasattr(cursor, "current") else None
    if cur is None:
        return ""
    n = int(getattr(cur, "n", 0) or 0)
    instruction = _clip(str(getattr(cur, "instruction", "") or "").strip(), 400)
    expected = _clip(str(getattr(cur, "expected", "") or "").strip(), 400)
    bits: list[str] = []
    if n:
        bits.append(f"步骤 {n}")
    if phase == "check":
        if expected:
            bits.append(f"校验预期：{expected}")
        if instruction:
            bits.append(f"对应用例操作：{instruction}")
    else:
        if instruction:
            bits.append(f"操作：{instruction}")
        if expected:
            bits.append(f"关联预期：{expected}")
    return " ".join(bits).strip()


def build_query(
    *,
    case_intent: str = "",
    step_focus: str = "",
    extra: str = "",
    last_action: str = "",
    history: str = "",
    screen: str = "",
) -> str:
    """检索 query：用例意图 + 当前步骤焦点，再叠本步动作 / 历史 / 屏幕文案。

    入口类知识（「如何进入 X」）往往不出现在屏幕文案里，只靠屏幕会误匹配首页
    信息流，因此用例意图必须稳定带进 query。step_focus 单独抽出「此刻在干什么」，
    避免整份步骤块稀释当前信号。
    """
    bits = [case_intent, step_focus, extra, last_action, history, screen]
    return "\n".join(str(x).strip() for x in bits if str(x).strip())


def _intent_tokens(text: str) -> list[str]:
    """从用例意图里抽可对齐知识的关键短语（中英混合，不含业务词表）。"""
    raw = (text or "").strip().lower()
    if not raw:
        return []
    found: list[str] = []
    for m in _QUOTED_RE.finditer(raw):
        t = m.group(1).strip()
        if len(t) >= 2 and t not in found:
            found.append(t)
    for tok in _SPLIT_RE.split(raw):
        t = tok.strip()
        if len(t) < 2 or t in found or t in _TOKEN_STOP:
            continue
        if re.fullmatch(r"[a-z0-9_\-]{3,}", t) or re.search(r"[一-鿿]", t):
            found.append(t[:24])
        if len(found) >= 24:
            break
    return found


def is_path_item(row: dict[str, Any]) -> bool:
    """是否像「怎么走 / 入口 / 操作路径」说明（通用启发式，不绑业务词）。"""
    blob = " ".join(str(x) for x in (
        row.get("title") or "",
        row.get("category") or "",
        " ".join(str(t) for t in (row.get("tags") or [])),
    ))
    if _PATH_RE.search(blob):
        return True
    cat = str(row.get("category") or "")
    return "导航" in cat or cat.lower() in {"ui导航", "navigation", "nav"}


_CONTENT_ROLE_RE = re.compile(
    r"用户协议|隐私政策|隐私条款|服务条款|terms\s*of\s*service|privacy\s*policy",
    re.I,
)
_AUTH_ROLE_RE = re.compile(
    r"手机号登录|验证码|登录密码|短信验证码|auth\s*form|sign\s*in",
    re.I,
)


def infer_screen_role(hierarchy_text: str = "") -> str:
    """从屏幕文案粗分 screen_role，供情境检索；不绑具体产品。"""
    blob = str(hierarchy_text or "")[:2000]
    if not blob.strip():
        return ""
    if _CONTENT_ROLE_RE.search(blob):
        return "content"
    if _AUTH_ROLE_RE.search(blob):
        return "auth_form"
    return ""


def build_knowledge_scene(
    *,
    ctx,
    cursor,
    case: dict[str, Any] | None = None,
    hierarchy_text: str = "",
) -> dict[str, str]:
    """统一检索 scene：lane / need / facet / surface，与 phase 无关地由当前指针推导。"""
    scene: dict[str, str] = {}
    platform = str(getattr(ctx, "platform", "") or "").strip().lower()
    scene["surface"] = "web" if platform == "web" else "app"
    env = str(getattr(ctx, "env_profile", "") or getattr(ctx, "env_label", "") or "").strip()
    if env:
        scene["env"] = env

    phase = str(getattr(cursor, "phase", "") or "").strip()
    if phase == "prep":
        scene["lane"] = "prep"
        scene["need"] = "howto"
    elif phase == "check":
        scene["lane"] = "expect"
        scene["need"] = "judge_selected"
        scene["facet"] = "chrome"
        scene["screen_role"] = "chrome_nav"
    else:
        scene["lane"] = "step"
        scene["need"] = "howto"
        role = infer_screen_role(hierarchy_text)
        if role:
            scene["screen_role"] = role

    existing = getattr(ctx, "case_scene", None) or {}
    if isinstance(existing, dict):
        for key in ("facet", "lane", "need", "screen_role", "slot", "surface"):
            val = str(existing.get(key) or "").strip()
            if val:
                scene[key] = val
    return scene


def rank_for_intent(
    rows: list[dict[str, Any]],
    *,
    case_intent: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """重排：used / 情境 / 绑定优先，再叠用例意图字面重叠。"""
    tokens = _intent_tokens(case_intent)
    scored: list[tuple[tuple, dict[str, Any]]] = []
    for row in rows or []:
        from mino_nexus.services.knowledge_match import body_text

        title = str(row.get("title") or "")
        tags = " ".join(str(t) for t in (row.get("tags") or []))
        body = body_text(row)
        blob = f"{title} {tags} {body[:400]}".lower()
        overlap = sum(1 for t in tokens if t and t in blob)
        used = 1 if row.get("used") else 0
        bind = 1 if row.get("bind_hit") else 0
        sit = int(row.get("sit_score") or 0)
        pct = int(row.get("match_pct") or 0)
        text = int(row.get("score") or 0)
        pathish = 1 if is_path_item(row) else 0
        scored.append(((used, bind, sit, overlap, pathish, pct, text), row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[: max(1, int(limit or 3))]]


def build_index_text(rows: list[dict[str, Any]]) -> str:
    """知识目录：只含 id / 标题 / 时机，不含正文。"""
    lines: list[str] = []
    for r in rows or []:
        kid = str(r.get("id") or "").strip()
        title = str(r.get("title") or "").strip()
        if not kid or not title or r.get("used") is False:
            continue
        tags = [str(t).strip() for t in (r.get("tags") or []) if str(t).strip()]
        when = " ".join(tags[:6])
        cat = str(r.get("category") or "").strip()
        bit = f"- {kid} 「{title}」"
        if cat:
            bit += f" [{cat}]"
        if when:
            bit += f" when={when}"
        lines.append(bit)
    return "\n".join(lines)


def build_body_text(items: list[dict[str, Any]], *, max_chars: int = 800) -> str:
    """模型点名后展开的正文。限 1 条，避免挤掉屏幕信息。

    标题/引导语在 llm_jobs 的 knowledge_body 块里，这里只出正文。
    """
    from mino_nexus.services.knowledge_match import prompt_snippet

    snippets = [prompt_snippet(it, max_chars=max_chars) for it in (items or [])[:1]]
    return "\n".join(s for s in snippets if s).strip()


def should_auto_inject_do_body(rows: list[dict[str, Any]]) -> bool:
    """do 阶段：情境/绑定命中时自动展开正文，避免只靠目录索引被模型忽略。"""
    from mino_nexus.services.knowledge_situation import SIT_MATCH_USED

    for row in rows or []:
        if row.get("used") is False:
            continue
        via = str(row.get("used_via") or "").strip()
        if via in ("bind", "situation"):
            return True
        if int(row.get("sit_score") or 0) >= SIT_MATCH_USED:
            return True
    return False


def pick_auto_knowledge_body(rows: list[dict[str, Any]], *, max_chars: int = 800) -> str:
    """无 agent-decide 点名时，自动取首条可用知识正文（check 或 do 强命中）。"""
    used = [r for r in (rows or []) if r.get("used") is not False]
    if not used:
        return ""
    return build_body_text(used[:1], max_chars=max_chars)


def named_ids(decision, allowed_rows: list[dict[str, Any]]) -> list[str]:
    """模型点名的 id，过滤成目录里真实存在的那些。"""
    ids = [str(x).strip() for x in (getattr(decision, "knowledge_ids", None) or []) if str(x).strip()]
    if not ids:
        return []
    allowed = {str(r.get("id") or "") for r in (allowed_rows or []) if r.get("id")}
    return [i for i in ids if i in allowed]


__all__ = [
    "build_case_intent",
    "build_step_focus",
    "build_knowledge_scene",
    "infer_screen_role",
    "should_auto_inject_do_body",
    "build_query",
    "is_path_item",
    "rank_for_intent",
    "build_index_text",
    "build_body_text",
    "pick_auto_knowledge_body",
    "named_ids",
]
