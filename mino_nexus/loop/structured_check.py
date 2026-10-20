"""结构足够时确定性校验；不可观察项可截图兜底，确定失败优先。"""
from __future__ import annotations

from typing import Any

from mino_nexus.ai.schemas import AssertResult
from mino_nexus.loop.hierarchy_slots import node_flag

_STRUCTURED_MODES = frozenset({"copy", "language_intent", "style", "dialog", "input_value", "spatial", "ui_absent"})


class CheckUnavailable(ValueError):
    pass

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

    def _row_ok(c: dict[str, Any]) -> bool:
        kind = str(c.get("checkpoint_kind") or "")
        if kind == "ui_absent":
            element = c.get("element") if isinstance(c.get("element"), dict) else {}
            return bool(str(element.get("name") or "").strip())
        return kind in _STRUCTURED_MODES or bool(c.get("element") or c.get("rules"))

    return all(_row_ok(c) for c in rows)


def hydrate_bound_text(cursor: Any, checkpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把同一步已输入的文本写进 rules，供看图或 DOM 比较。不改用例定义。"""
    typed = _typed_text(cursor)
    from mino_nexus.core.secret_redaction import sanitize
    if sanitize(typed) != typed:
        return checkpoints
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
    provider_id: str = "",
) -> AssertResult:
    from mino_nexus.services.checkpoint_text_policy import text_policy
    from mino_nexus.loop.text_assert import checkpoint_report, run_dom_text_checks

    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]
    rows = hydrate_bound_text(cursor, checkpoints)
    observation = dict(getattr(ctx, "observation_contract", None) or {})
    incomplete = not observation or bool(observation.get("truncated")) or bool(observation.get("missing"))
    checkpoint_results: list[dict[str, Any]] = []
    semantic: list[dict[str, Any]] = []
    required = {str(row.get("point_id") or ""): row.get("required") is not False for row in rows}
    for row in rows:
        rules = row.get("rules") if isinstance(row.get("rules"), dict) else {}
        if text_policy(row) == "intent" or text_policy(row) and rules.get("expected_language"):
            semantic.append(row)
            continue
        error_kind = ""
        try:
            if not nodes:
                raise CheckUnavailable("当前层级为空，无法验证")
            if incomplete and (
                rules.get("exclusive")
                or (row.get("checkpoint_kind") == "dialog" and rules.get("present") is False)
                or row.get("checkpoint_kind") == "ui_absent"
            ):
                raise CheckUnavailable("观察范围不完整，无法证明弹窗不存在或唯一")
            ok, reason = _eval_one(row, nodes)
            if not ok and incomplete and str(row.get("checkpoint_kind") or "") in {"copy", "dialog", "ui_absent"}:
                raise CheckUnavailable("结构采集存在截断或缺口，未命中目标不能证明不存在")
        except CheckUnavailable as exc:
            ok, reason, error_kind = False, str(exc), "dom_check_unavailable"
        entry = {"point_id": str(row.get("point_id") or ""), "passed": ok,
                 "evidence": reason or "DOM 目标与规则匹配", "error_kind": error_kind,
                 "evidence_scope": "unknown" if error_kind else "visible_full"}
        if text_policy(row):
            entry["text_match"] = text_policy(row)
        checkpoint_results.append(entry)
    definite_fail = any(required.get(row["point_id"]) and not row["passed"] and not row["error_kind"]
                        for row in checkpoint_results)
    service_error = ""
    if semantic and not definite_fail:
        text_result = run_dom_text_checks(semantic, ctx, provider_id=provider_id)
        service_error = text_result.error_kind
        if service_error:
            checkpoint_results.extend({"point_id": row["point_id"], "passed": False,
                "evidence": text_result.ai_reasoning, "error_kind": service_error,
                "evidence_scope": "unknown", "text_match": text_policy(row)} for row in semantic)
        else:
            checkpoint_results.extend({**row, "error_kind": row.get("error_kind") or ""} for row in text_result.checkpoint_results)
    elif semantic:
        checkpoint_results.extend({"point_id": row["point_id"], "passed": False,
            "evidence": "其他必选项已明确失败，本项未调用模型", "error_kind": "not_evaluated",
            "evidence_scope": "unknown", "text_match": text_policy(row)} for row in semantic)
    fails = [row["evidence"] for row in checkpoint_results
             if required.get(row["point_id"]) and not row["passed"] and not row.get("error_kind")]
    unavailable = [row["evidence"] for row in checkpoint_results
                   if required.get(row["point_id"]) and row.get("error_kind")]
    error_kind = (service_error or "dom_check_unavailable") if unavailable and not fails else ""
    result = AssertResult(
        passed=not fails and not unavailable,
        confidence=0.0 if error_kind else 1.0,
        ai_reasoning="；".join(fails or unavailable)[:500] if fails or unavailable else f"DOM 校验通过（{len(rows)} 条）",
        evidence="dom", checkpoint_results=checkpoint_results, error_kind=error_kind,
    )
    if writer:
        writer.append("assert/dom", {
            "passed": bool(result.passed),
            "status": "blocked" if result.error_kind else "pass" if result.passed else "fail",
            "error_kind": result.error_kind, "checkpoints": len(rows), "nodes": len(nodes),
            "summary": str(result.ai_reasoning or "")[:300],
            "checkpoint_results": [checkpoint_report(row) for row in result.checkpoint_results],
        })
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
    raise CheckUnavailable("当前节点缺少可验证的 enabled/disabled 状态")


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
    if mode == "ui_absent":
        return _eval_absent(title, role, element, rules, nodes)
    raise CheckUnavailable(f"{title}无法验证：DOM 没有该检查的实现")


def _pool(nodes: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    if not role or role == "page":
        return list(nodes)
    matched = [n for n in nodes if _role_ok(n, role)]
    return matched or list(nodes)


def _eval_copy(title: str, role: str, rules: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[bool, str]:
    mode = "equals" if str(rules.get("match") or "") == "equals" else "contains"
    needle = str(rules.get("title") or rules.get("body") or rules.get("value") or rules.get("label") or "")
    if not needle:
        raise CheckUnavailable(f"{title}无法验证：文案检查缺少要核对的文本")
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
        raise CheckUnavailable(f"{title}无法验证：样式检查命中 {len(pool)} 个节点")
    enabled = _node_enabled(pool[0])
    if style == "disabled" and enabled:
        return False, f"{title}未通过：按钮仍可点击"
    if style == "enabled" and not enabled:
        return False, f"{title}未通过：按钮不可点击"
    if style in ("disabled", "enabled"):
        return True, ""
    raise CheckUnavailable(f"{title}无法验证：未知样式 {style}")


def _eval_absent(
    title: str,
    role: str,
    element: dict[str, Any],
    rules: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> tuple[bool, str]:
    which = str(
        element.get("name") or rules.get("which") or rules.get("title") or rules.get("label") or ""
    ).strip()
    if not which:
        raise CheckUnavailable(f"{title}无法验证：不可见检查缺少目标名称")
    pool = _pool(nodes, role) if role and role != "page" else list(nodes)
    named = [n for n in pool if which in _blob(n)]
    if named:
        return False, f"{title}未通过：DOM 仍包含「{which[:40]}」"
    return True, f"DOM 未发现「{which[:40]}」"


def _eval_dialog(title: str, rules: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[bool, str]:
    which = str(rules.get("which") or "").strip()
    present = bool(rules.get("present"))
    exclusive = bool(rules.get("exclusive"))
    dialogs = [n for n in nodes if _role_ok(n, "dialog")]
    named = [n for n in nodes if which and which in _blob(n)] if which else []
    if rules.get("present") is False:
        if not which:
            raise CheckUnavailable(f"{title}无法验证：否定弹窗检查缺少目标")
        if named:
            return False, f"{title}未通过：检查时仍发现「{which[:40]}」"
        return True, "检查时未发现指定弹窗"
    if present and which and not named:
        return False, f"{title}未通过：树上没有「{which[:40]}」"
    if exclusive:
        if len(dialogs) != 1:
            return False, f"{title}未通过：dialog 角色有 {len(dialogs)} 个，要求仅此一个"
    if present or named:
        return True, ""
    raise CheckUnavailable(f"{title}无法验证：弹窗检查缺少哪个弹窗")


def _eval_input(title: str, rules: dict[str, Any], nodes: list[dict[str, Any]]) -> tuple[bool, str]:
    bound = str(rules.get("bound_text") or "")
    if not bound:
        raise CheckUnavailable(f"{title}无法验证：没有读到同一步输入的文案")
    fields = [n for n in nodes if _role_ok(n, "input")]
    if len(fields) != 1:
        raise CheckUnavailable(f"{title}无法验证：输入框命中 {len(fields)} 个")
    if fields[0].get("password"):
        raise CheckUnavailable("密码框值已脱敏，不能从空文本推断输入失败")
    value = str(fields[0].get("text") or "")
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
        raise CheckUnavailable(f"{title}无法验证：位置关系缺少参照或方向")
    anchors = [n for n in nodes if anchor in _blob(n) and _center(n)]
    subjects = [n for n in nodes if subject in _blob(n) and _center(n)]
    if len(anchors) != 1 or len(subjects) != 1:
        raise CheckUnavailable(f"{title}无法验证：参照 {len(anchors)} 个，主语 {len(subjects)} 个")
    a = _center(anchors[0])
    s = _center(subjects[0])
    if a is None or s is None or not pred(a, s):
        return False, f"{title}未通过：方位不是 {relation}"
    return True, ""
