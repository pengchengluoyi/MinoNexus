"""用例步骤效果探针：预期文案是否已在屏上出现（纯 hierarchy，不调 LLM）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.loop.hierarchy_slots import match_any

_PREFIXES = (
    "跳转到",
    "切换到",
    "进入",
    "显示",
    "出现",
    "底部",
    "界面",
    "tab",
    "Tab",
)
_QUOTE_RE = re.compile(r"[「『\"“]([^」』\"”]{1,16})[」』\"”]")
_ABSENCE_MARKERS = ("不显示", "不再显示", "不应出现", "没有出现", "未显示", "不可见")
_LOGOUT_ABSENT_TERMS = ("退出登录", "注销", "登出")


def _clean_chunk(text: str) -> str:
    s = str(text or "").strip()
    for prefix in _PREFIXES:
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix) :].strip()
    if s.endswith("页面") and len(s) > 2:
        s = s[:-2]
    elif s.endswith("页") and len(s) > 2:
        s = s[:-1]
    if s.endswith("提示") and len(s) > 3:
        s = s[:-2]
    return s.strip("：:，,、 ")


def _quoted_tokens(text: str) -> list[str]:
    out: list[str] = []
    for match in _QUOTE_RE.finditer(str(text or "")):
        token = str(match.group(1) or "").strip()
        if len(token) >= 2 and token not in out:
            out.append(token)
    return out


def expected_absence_terms(expected: str) -> list[str]:
    """否定类 expected：屏上应不存在某控件文案。"""
    raw = str(expected or "").strip()
    if not raw:
        return []
    if not any(m in raw for m in _ABSENCE_MARKERS):
        return []
    out: list[str] = []
    for tok in _quoted_tokens(raw):
        if tok not in out:
            out.append(tok)
    for term in _LOGOUT_ABSENT_TERMS:
        if term in raw and term not in out:
            out.append(term)
    if not out and "退出" in raw:
        out.append("退出登录")
    return out[:6]


def probe_absence(
    nodes: list[dict[str, Any]],
    terms: list[str],
) -> tuple[bool, list[str]]:
    """terms 在 hierarchy 上均不可见 → 命中。"""
    if not terms or not nodes:
        return False, []
    missing: list[str] = []
    for term in terms:
        conds = [
            {"text_contains": term},
            {"content_desc_contains": term},
        ]
        if match_any(nodes, conds):
            return False, []
        missing.append(term)
    return True, missing


def probe_diagnostic(
    expected: str,
    nodes: list[dict[str, Any]],
    *,
    instruction: str = "",
    defer_expected_to_check: bool = False,
) -> dict[str, Any]:
    """运维/日志：探针模式与命中摘要。"""
    exp = str(expected or "").strip()
    absent = expected_absence_terms(exp) if not defer_expected_to_check else []
    if absent:
        ok, kws = probe_absence(nodes, absent)
        return {
            "mode": "absence",
            "hit": ok,
            "keywords": kws,
            "absence_terms": absent,
        }
    hit, kws = probe_expected_for_do(
        exp,
        nodes,
        instruction=instruction,
        defer_expected_to_check=defer_expected_to_check,
    )
    mode = "presence" if exp else "instruction_only"
    if expected_profile_shape_config(exp):
        mode = "profile_shape"
    return {
        "mode": mode,
        "hit": hit,
        "keywords": kws[:8],
        "terms": _probe_terms(exp, instruction)[:8] if exp else [],
    }


def _probe_terms(expected: str, instruction: str = "") -> list[str]:
    raw = str(expected or "").strip()
    terms: list[str] = []
    if raw:
        for chunk in re.split(r"[、,/；;|｜]", raw):
            cleaned = _clean_chunk(chunk)
            if len(cleaned) >= 2 and cleaned not in terms:
                terms.append(cleaned)
        if not terms:
            cleaned = _clean_chunk(raw)
            if len(cleaned) >= 2:
                terms.append(cleaned)
        extra: list[str] = []
        for term in list(terms):
            if len(term) >= 4:
                for width in (2, 3):
                    piece = term[-width:]
                    if len(piece) >= 2 and piece not in terms and piece not in extra:
                        extra.append(piece)
        terms.extend(extra)
    for token in _quoted_tokens(instruction):
        if token not in terms:
            terms.append(token)
    return terms[:12]


_PROFILE_SHAPE_EXPECT_RE = re.compile(
    r"形象配置|选择.{0,8}形象|多个头像|头像.{0,6}完成",
    re.I,
)
_CAMERA_NOT_PROFILE_RE = re.compile(
    r"完整入镜|确保物品|取景|快门|拍摄按钮|拍照按钮|重拍",
    re.I,
)


def expected_profile_shape_config(expected: str) -> bool:
    return bool(_PROFILE_SHAPE_EXPECT_RE.search(str(expected or "")))


def is_camera_not_profile_config(nodes: list[dict[str, Any]]) -> bool:
    """相机取景/拍摄页：常有入镜提示或快门，但不是形象向导（无独立「完成」保存）。"""
    if not nodes:
        return False
    cam = match_any(
        nodes,
        [
            {"text_contains": "入镜"},
            {"text_contains": "完整入镜"},
            {"text_contains": "确保物品"},
            {"content_desc_contains": "快门"},
        ],
    )
    if not cam and not _CAMERA_NOT_PROFILE_RE.search(_nodes_text_blob(nodes)):
        return False
    wizard = match_any(
        nodes,
        [
            {"text_contains": "选择你的形象"},
            {"text_contains": "形象配置"},
            {"text_equals": "完成"},
        ],
    )
    return not wizard


def _nodes_text_blob(nodes: list[dict[str, Any]]) -> str:
    bits: list[str] = []
    for n in nodes[:80]:
        if not isinstance(n, dict):
            continue
        bits.append(str(n.get("text") or ""))
        bits.append(str(n.get("content_desc") or ""))
    return " ".join(bits)


def probe_profile_shape_config(
    expected: str,
    nodes: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    if is_camera_not_profile_config(nodes):
        return False, []
    hits: list[str] = []
    need_complete = "完成" in str(expected or "")
    if need_complete:
        if match_any(nodes, [{"text_equals": "完成"}, {"text_contains": "完成"}]):
            hits.append("完成")
    for term in ("形象", "头像", "配置"):
        if term in str(expected or "") and match_any(
            nodes,
            [{"text_contains": term}, {"content_desc_contains": term}],
        ):
            hits.append(term)
    avatar_like = sum(
        1
        for n in nodes
        if isinstance(n, dict)
        and str(n.get("class") or "").lower() in ("imageview", "image", "img")
    )
    if avatar_like >= 3 and need_complete and "完成" in hits:
        hits.append("头像区")
    return bool(hits) and (not need_complete or "完成" in hits), hits


def profile_shape_mismatch_hint(
    expected: str,
    nodes: list[dict[str, Any]],
) -> str:
    if not expected_profile_shape_config(expected):
        return ""
    if is_camera_not_profile_config(nodes):
        return (
            "【形象配置】当前为相机/取景页，不是「选择形象配置」向导。"
            "请先返回或走 onboarding，直到屏上出现可选头像网格与「完成」按钮，再 signal_done。"
        )
    hit, kws = probe_profile_shape_config(expected, nodes)
    if hit:
        return ""
    return (
        "【形象配置】尚未命中形象向导：需要屏上同时可见「完成」与形象/头像相关文案。"
        "勿在拍照页反复点同一控件。"
    )


def probe_expected_for_do(
    expected: str,
    nodes: list[dict[str, Any]],
    *,
    instruction: str = "",
    defer_expected_to_check: bool = False,
) -> tuple[bool, list[str]]:
    """do 阶段探针：expected 延后到 check 时，只用 instruction 引号内目标，避免「生成中」等弱命中拖住 do。"""
    if defer_expected_to_check:
        return probe("", nodes, instruction=instruction)
    if expected_profile_shape_config(expected):
        return probe_profile_shape_config(expected, nodes)
    return probe(expected, nodes, instruction=instruction)


def probe(
    expected: str,
    nodes: list[dict[str, Any]],
    *,
    instruction: str = "",
) -> tuple[bool, list[str]]:
    """本步预期文案是否已在屏上出现。返回 (命中, 命中的关键词)。"""
    absent = expected_absence_terms(expected)
    if absent:
        return probe_absence(nodes, absent)
    terms = _probe_terms(expected, instruction)
    if not terms or not nodes:
        return False, []
    hits: list[str] = []
    for term in terms:
        conds = [
            {"text_contains": term},
            {"content_desc_contains": term},
        ]
        if match_any(nodes, conds):
            hits.append(term)
    return bool(hits), hits


def localized_matches_step(
    localized: dict[str, Any] | None,
    *,
    instruction: str = "",
    expected: str = "",
) -> bool:
    """定位到的逻辑页是否已覆盖本步引号内目标（不把别名模糊匹配当达成）。"""
    loc = localized if isinstance(localized, dict) else {}
    chosen = str(loc.get("chosen") or "").strip()
    try:
        conf = float(loc.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if not chosen or conf < 0.35:
        return False
    display = str(loc.get("display_name") or loc.get("label") or "").strip()
    aliases = [
        str(item).strip()
        for item in (loc.get("aliases") or [])
        if str(item).strip()
    ]
    tokens = _quoted_tokens(instruction)
    cleaned = _clean_chunk(expected)
    if cleaned and len(cleaned) >= 2 and cleaned not in tokens:
        tokens.append(cleaned)
    for token in tokens:
        if len(token) < 2:
            continue
        if display and (token == display or token in display or display in token):
            return True
        if token in aliases:
            return True
    return False


def _expects_full_page_navigation(expected: str) -> bool:
    exp = str(expected or "").strip()
    if not exp:
        return False
    if any(token in exp for token in ("跳转到", "切换到", "进入", "打开")):
        return True
    return "页面" in exp and len(exp) >= 8


def should_auto_enter_check(
    *,
    loc_hit: bool,
    probe_hit: bool,
    expected: str = "",
    keywords: list[str] | None = None,
    hit_streak: int = 0,
    defer_expected_to_check: bool = False,
) -> bool:
    """已在目标逻辑页或强达成探针命中时，直接进 check，不再等模型 signal_done。

    仅底栏短文案命中（弱）时仍要连续两轮，避免详情页看见 Tab 字就收工。
    """
    if defer_expected_to_check:
        return False
    if loc_hit:
        return True
    if not probe_hit:
        return False
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if _expects_full_page_navigation(expected) and kws and all(len(k) <= 4 for k in kws):
        return int(hit_streak or 0) >= 2
    if probe_hit and kws and all(len(str(k)) <= 4 for k in kws):
        return int(hit_streak or 0) >= 2
    return True


def maybe_mark_deferred_nav_tab(
    cursor: Any,
    *,
    instruction: str,
    expected: str,
    localized: dict[str, Any] | None,
    nodes: list[dict[str, Any]] | None,
    thought: str = "",
    coerce_done: bool = False,
) -> bool:
    """expected 延后到 check 时，人已在目标页则 nav_tab 意图视为完成（底栏 Tab 可能已不可见）。"""
    from mino_nexus.loop.step_intent import instruction_required_intents
    from mino_nexus.loop.step_pointer import _expected_defers_to_check
    from mino_nexus.loop.thought_done import thought_implies_signal_done

    if not _expected_defers_to_check(expected):
        return False
    if "nav_tab" not in instruction_required_intents(instruction):
        return False
    defer = True
    hit, _ = probe_expected_for_do(
        expected,
        list(nodes or []),
        instruction=instruction,
        defer_expected_to_check=defer,
    )
    loc_hit = localized_matches_step(
        localized,
        instruction=instruction,
        expected=expected,
    )
    thought_ok = bool(coerce_done and thought_implies_signal_done(thought))
    if not (hit or loc_hit or thought_ok):
        return False
    done = getattr(cursor, "step_intents_done", None)
    if not isinstance(done, set):
        return False
    done.add("nav_tab")
    refresh = getattr(cursor, "refresh_do_subphase", None)
    if callable(refresh):
        refresh()
    return True


def achievement_hint(keywords: list[str], *, expected: str = "") -> str:
    if not keywords:
        return ""
    shown = " / ".join(str(k) for k in keywords[:6])
    if _expects_full_page_navigation(expected) and all(len(str(k)) <= 4 for k in keywords):
        return (
            f"【达成提示·弱】屏上可见「{shown}」等文案，可能仅为底栏 Tab，不等于已进入目标页。"
            f"若步骤要求进入完整页面，请继续 fsm_navigate 或点击目标 Tab；勿仅因此 signal_done。"
        )
    return (
        f"【达成提示】本步预期已在屏上出现（命中：{shown}）。"
        f"若无其它待办，立即 signal_done。"
    )
