"""按当前 in_progress 里程碑裁剪 hierarchy（P2），减轻显著性偏置。"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from mino_nexus.loop.milestone_orchestrator import in_progress_milestone
from mino_nexus.loop.milestones import read_state

_TOKEN_SPLIT = re.compile(r"[\s,，、/|；;：:]+")
_DEFAULT_MAX_CHARS = 8000
_DEFAULT_MAX_LINES = 90
_HEAD_LINES = 12

# catalog key_ref → 裁剪时额外保留的屏上关键词（无图像算法）
_KEY_REF_HINTS: dict[str, tuple[str, ...]] = {
    "operation.dismiss_overlay": (
        "关闭",
        "跳过",
        "取消",
        "Later",
        "Not Now",
        "Learn",
        "允许",
        "拒绝",
        "×",
    ),
    "operation.tap": ("按钮", "提交", "确定", "登录", "Log in", "Sign"),
    "operation.input_text": ("输入", "EditText", "textfield", "邮箱", "手机", "验证码"),
    "operation.open_cart": ("购物车", "cart", "Cart"),
    "operation.nav_target": ("导航", "tab", "Tab"),
    "expected.session_state": ("登录", "头像", "我的", "Mine", "guest", "Log in"),
    "expected.ui_text": ("展示", "包含", "文案"),
    "expected.ui_absent": ("不应", "没有", "未展示", "不包含"),
    "operation.swipe": ("滑动", "scroll", "Swipe", "fling"),
    "operation.wait_ready": ("加载", "loading", "请稍候"),
}


def vision_trim_hierarchy_enabled() -> bool:
    raw = str(os.environ.get("MINO_VISION_TRIM_HIERARCHY", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _keywords_from_focus(row: dict[str, Any]) -> list[str]:
    blob = " ".join(
        str(row.get(k) or "")
        for k in ("title", "key_ref", "hook_cap", "nav_target", "id", "evaluate")
    )
    out: list[str] = []
    for part in _TOKEN_SPLIT.split(blob):
        t = part.strip()
        if len(t) >= 2:
            out.append(t)
        if len(t) >= 4:
            out.append(t[:4])
    ref = str(row.get("key_ref") or "")
    if ref:
        for seg in ref.split("."):
            if len(seg) >= 3:
                out.append(seg)
        hints = _KEY_REF_HINTS.get(ref)
        if hints:
            out.extend(hints)
        for key, hints in _KEY_REF_HINTS.items():
            if ref.startswith(key.rsplit(".", 1)[0] + ".") or key in ref:
                out.extend(hints)
    hook = str(row.get("hook_cap") or "")
    if hook == "tap_element":
        out.extend(_KEY_REF_HINTS.get("operation.tap", ()))
    return list(dict.fromkeys(out))[:48]


def trim_hierarchy_for_focus(
    hierarchy_text: str,
    focus: Optional[dict[str, Any]],
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_lines: int = _DEFAULT_MAX_LINES,
) -> tuple[str, bool]:
    raw = str(hierarchy_text or "")
    if not raw or not focus:
        return raw[:max_chars], False
    lines = raw.splitlines()
    if len(lines) <= 40 and len(raw) <= max_chars:
        return raw, False
    kws = _keywords_from_focus(focus)
    if not kws:
        return raw[:max_chars], len(raw) > max_chars

    scored: list[tuple[int, int, str]] = []
    lower_kws = [k.lower() for k in kws if k]
    for i, line in enumerate(lines):
        low = line.lower()
        score = sum(1 for kw in lower_kws if kw in low)
        if score:
            scored.append((score, i, line))
    scored.sort(key=lambda x: (-x[0], x[1]))
    keep: set[int] = set(range(min(_HEAD_LINES, len(lines))))
    for score, i, _ in scored:
        if len(keep) >= max_lines:
            break
        if score > 0:
            keep.add(i)
    out = "\n".join(lines[i] for i in sorted(keep))
    trimmed = out[:max_chars]
    return trimmed, trimmed != raw[:max_chars]


def apply_vision_observation_slots(
    inspect_slots: dict[str, Any],
    cursor: Any,
    *,
    writer: Any = None,
) -> None:
    if not vision_trim_hierarchy_enabled():
        return
    focus = in_progress_milestone(read_state(cursor))
    if not focus:
        return
    hier = str(inspect_slots.get("hierarchy_text") or "")
    trimmed, changed = trim_hierarchy_for_focus(hier, focus)
    if not changed:
        return
    inspect_slots["hierarchy_text"] = trimmed
    inspect_slots["hierarchy_trimmed"] = "1"
    if writer is not None:
        try:
            from mino_nexus.loop.llm_step_context import step_scope_key

            cs, ph = step_scope_key(cursor)
            writer.append(
                "observation/hierarchy_trim",
                {
                    "case_step": cs,
                    "phase": ph,
                    "focus_id": str(focus.get("id") or ""),
                    "before_chars": len(hier),
                    "after_chars": len(trimmed),
                },
            )
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "apply_vision_observation_slots",
    "trim_hierarchy_for_focus",
    "vision_trim_hierarchy_enabled",
]
