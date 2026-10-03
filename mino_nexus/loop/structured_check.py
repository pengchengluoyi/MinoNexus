"""结构化校验。看图只吃 element+rules；DOM 只读当前树，失败不改走看图。"""
from __future__ import annotations

from typing import Any

from mino_nexus.ai.schemas import AssertResult
from mino_nexus.loop.hierarchy_slots import node_flag

_STRUCTURED_MODES = frozenset({"copy", "style", "dialog", "input_value", "spatial"})

_RELATION_OK = {
    "right": lambda a, s: s[0] > a[0],
    "left": lambda a, s: s[0] < a[0],
    "above": lambda a, s: s[1] < a[1],
    "below": lambda a, s: s[1] > a[1],
}


def checkpoints_need_structured(checkpoints: list[dict[str, Any]] | None) -> bool:
    rows = [c for c in (checkpoints or []) if isinstance(c, dict)]
    if not rows:
        return False
    return all(
        str(c.get("checkpoint_kind") or "") in _STRUCTURED_MODES or c.get("element") or c.get("rules")
        for c in rows
    )


def hydrate_bound_text(cursor: Any, checkpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把同一步已输入的文本写进 rules，供看图或 DOM 比较。不改用例定义。"""
    typed = _typed_text(cursor)
    if not typed:
        return checkpoints
    out: list[dict[str, Any]] = []
    for raw in checkpoints:
        row = dict(raw)
        rules = dict(row.get("rules") or {}) if isinstance(row.get("rules"), dict) else {}
        if row.get("bind") or str(row.get("checkpoint_kind") or "") == "input_value":
            rules["bound_text"] = typed[:500]
            try:
                limit = int(rules.get("max_length") or 0)
            except (TypeError, ValueError):
                limit = 0
            if limit > 0:
                rules["expected_value"] = typed[:limit]
        if rules:
            row["rules"] = rules
        out.append(row)
    return out


def run_dom_structured_check(
    cursor: Any,
    ctx: Any,
    checkpoints: list[dict[str, Any]],
    *,
    writer: Any = None,
) -> AssertResult:
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    rows = hydrate_bound_text(cursor, checkpoints)
    if not nodes:
        result = AssertResult(
            passed=False,
            confidence=1.0,
            ai_reasoning="当前是 DOM 执行，层级为空，无法验证。",
            evidence="dom",
        )
    else:
        fails: list[str] = []
        for row in rows:
            ok, reason = _eval_one(row, nodes)
            if not ok:
                fails.append(reason)
        if fails:
            result = AssertResult(
                passed=False,
                confidence=1.0,
                ai_reasoning="；".join(fails)[:500],
                evidence="dom",
            )
        else:
            result = AssertResult(
                passed=True,
                confidence=1.0,
                ai_reasoning=f"DOM 校验通过（{len(rows)} 条）",
                evidence="dom",
            )
    if writer:
        writer.append(
            "assert/dom",
            {
                "passed": bool(result.passed),
                "checkpoints": len(rows),
                "nodes": len(nodes),
                "summary": str(result.ai_reasoning or "")[:300],
            },
        )
    return result


def _typed_text(cursor: Any) -> str:
    bag = getattr(cursor, "typed_by_step", None)
    if not isinstance(bag, dict) or not bag:
        return ""
    cur = cursor.current() if hasattr(cursor, "current") else None
    step_n = int(getattr(cur, "n", 0) or 0) if cur is not None else 0
    rows = bag.get(step_n) or bag.get(str(step_n)) or []
    if not rows and len(bag) == 1:
        rows = next(iter(bag.values()))
    if not isinstance(rows, list) or not rows:
        return ""
    return str(rows[-1] or "")


def _blob(node: dict[str, Any]) -> str:
    return f"{node.get('text') or ''} {node.get('content_desc') or ''}".strip()


def _cls(node: dict[str, Any]) -> str:
    return str(node.get("class") or node.get("role") or "").lower()


def _role_ok(node: dict[str, Any], role: str) -> bool:
    kind = str(role or "").strip().lower()
    cls = _cls(node)
    web = str(node.get("role") or "").strip().lower()
    if kind == "button":
        return cls.endswith("button") or "button" in cls or web == "button"
    if kind == "input":
        return (
            "edittext" in cls
            or "textfield" in cls
            or bool(node.get("editable"))
            or web in ("textbox", "searchbox", "input", "textarea")
        )
    if kind == "dialog":
        return "dialog" in cls or "alertdialog" in cls or web in ("dialog", "alertdialog")
    if kind == "checkbox":
        return "checkbox" in cls or web == "checkbox" or node_flag(node, "checkable")
    return True


def _matches_text(node: dict[str, Any], needle: str, *, mode: str) -> bool:
    text = _blob(node)
    want = str(needle or "").strip()
    if not want or not text:
        return False
    if mode == "equals":
        return text == want or str(node.get("text") or "").strip() == want
    return want in text


def _center(node: dict[str, Any]) -> tuple[int, int] | None:
    center = node.get("center")
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        try:
            return int(center[0]), int(center[1])
        except (TypeError, ValueError):
            return None
    bounds = node.get("bounds")
    if isinstance(bounds, (list, tuple)) and len(bounds) >= 4:
        try:
            left, top, right, bottom = (int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3]))
        except (TypeError, ValueError):
            return None
        return (left + right) // 2, (top + bottom) // 2
    return None


def _node_enabled(node: dict[str, Any]) -> bool:
    if "enabled" in node:
        return node_flag(node, "enabled")
    if "aria-disabled" in node or "aria_disabled" in node:
        return not node_flag(node, "aria-disabled" if "aria-disabled" in node else "aria_disabled")
    return bool(node.get("clickable"))


def _eval_one(row: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[bool, str]:
    mode = str(row.get("checkpoint_kind") or "")
    rules = row.get("rules") if isinstance(row.get("rules"), dict) else {}
    element = row.get("element") if isinstance(row.get("element"), dict) else {}
    role = str(element.get("role") or "")
    title = str(row.get("title") or mode or "校验")
    if mode == "copy":
        return _eval_copy(title, role, rules, nodes)
    if mode == "style":
        return _eval_style(title, role, rules, nodes)
    if mode == "dialog":
        return _eval_dialog(title, rules, nodes)
    if mode == "input_value":
        return _eval_input(title, rules, nodes)
    if mode == "spatial":
        return _eval_spatial(title, rules, nodes)
    return False, f"{title}无法验证：DOM 没有该检查的实现"


def _pool(nodes: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    if not role or role == "page":
        return list(nodes)
    matched = [n for n in nodes if _role_ok(n, role)]
    return matched or list(nodes)


def _eval_copy(title: str, role: str, rules: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[bool, str]:
    mode = "equals" if str(rules.get("match") or "") == "equals" else "contains"
    needle = str(rules.get("title") or rules.get("body") or rules.get("value") or rules.get("label") or "")
    if not needle:
        return False, f"{title}无法验证：文案检查缺少要核对的文本"
    pool = _pool(nodes, role if role != "dialog" else "")
    if any(_matches_text(n, needle, mode=mode) for n in pool):
        return True, ""
    return False, f"{title}未通过：树上没有「{needle[:40]}」"


def _eval_style(title: str, role: str, rules: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[bool, str]:
    style = str(rules.get("style") or "")
    label = str(rules.get("label") or "")
    pool = [n for n in nodes if _role_ok(n, role or "button")]
    if label:
        pool = [n for n in pool if label in _blob(n)]
    if len(pool) != 1:
        return False, f"{title}无法验证：样式检查命中 {len(pool)} 个节点"
    enabled = _node_enabled(pool[0])
    if style == "disabled" and enabled:
        return False, f"{title}未通过：按钮仍可点击"
    if style == "enabled" and not enabled:
        return False, f"{title}未通过：按钮不可点击"
    if style in ("disabled", "enabled"):
        return True, ""
    return False, f"{title}无法验证：未知样式 {style}"


def _eval_dialog(title: str, rules: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[bool, str]:
    which = str(rules.get("which") or "").strip()
    present = bool(rules.get("present"))
    exclusive = bool(rules.get("exclusive"))
    dialogs = [n for n in nodes if _role_ok(n, "dialog")]
    named = [n for n in nodes if which and which in _blob(n)] if which else []
    if present and which and not named:
        return False, f"{title}未通过：树上没有「{which[:40]}」"
    if exclusive:
        if len(dialogs) != 1:
            return False, f"{title}未通过：dialog 角色有 {len(dialogs)} 个，要求仅此一个"
    if present or named:
        return True, ""
    return False, f"{title}无法验证：弹窗检查缺少哪个弹窗"


def _eval_input(title: str, rules: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[bool, str]:
    bound = str(rules.get("bound_text") or "")
    if not bound:
        return False, f"{title}无法验证：没有读到同一步输入的文案"
    fields = [n for n in nodes if _role_ok(n, "input")]
    if len(fields) != 1:
        return False, f"{title}无法验证：输入框命中 {len(fields)} 个"
    value = str(fields[0].get("text") or "").strip()
    try:
        limit = int(rules.get("max_length") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        expect = bound[:limit]
        if value != expect:
            return False, f"{title}未通过：框内长度 {len(value)}，期望前 {limit} 字"
    if rules.get("accepts_special"):
        specials = [ch for ch in bound if not ch.isalnum() and not ch.isspace()]
        missing = [ch for ch in specials if ch not in value]
        if missing:
            return False, f"{title}未通过：特殊字符未保留"
    if rules.get("no_error_toast"):
        alerts = [n for n in nodes if _role_ok(n, "dialog") and "alert" in _cls(n)]
        if alerts:
            return False, f"{title}未通过：存在错误提示层"
    return True, ""


def _eval_spatial(title: str, rules: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[bool, str]:
    anchor = str(rules.get("relative_to") or "").strip()
    subject = str(rules.get("subject") or "").strip()
    relation = str(rules.get("relation") or "")
    pred = _RELATION_OK.get(relation)
    if not anchor or not subject or pred is None:
        return False, f"{title}无法验证：位置关系缺少参照或方向"
    anchors = [n for n in nodes if anchor in _blob(n) and _center(n)]
    subjects = [n for n in nodes if subject in _blob(n) and _center(n)]
    if len(anchors) != 1 or len(subjects) != 1:
        return False, f"{title}无法验证：参照 {len(anchors)} 个，主语 {len(subjects)} 个"
    a = _center(anchors[0])
    s = _center(subjects[0])
    if a is None or s is None or not pred(a, s):
        return False, f"{title}未通过：方位不是 {relation}"
    return True, ""
