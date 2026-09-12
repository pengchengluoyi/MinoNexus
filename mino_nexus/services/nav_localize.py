"""我现在在哪一屏。设计稿 docs/NAVIGATION_ATLAS.md §2。

主路径**不调 VLM**：规则信号 + 置信度就够跑通主链，VLM 的延迟/成本/误判会把迭代拖死。
`extra_signals` 是给 pHash / VLM 预留的入口，默认没人传。

三档决策（§2.2）：
  ≥ 0.75  推荐边 + 允许动作集
  0.45–0.75  只探索（recover + 允许动作集内）
  < 0.45  恢复边优先

hierarchy 拿不到时（§10.0.2 降级）：tab_bar / text_landmarks 判不了，只剩 session + history，
`degraded=True`，置信度天然上不去 —— 这正是我们要的，宁可退回恢复边也别瞎点。
"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.loop.hierarchy_slots import match_any, node_matches

# 默认权重，与设计稿 §2.2 的「高/中/低」对应
DEFAULT_WEIGHTS: dict[str, float] = {
    "session": 1.0,
    "tab_bar": 0.9,
    "layout_framework": 0.85,
    "text_landmarks": 0.7,
    "history": 0.6,
    "phash_delta": 0.2,
}

BAND_HIGH = 0.75
BAND_LOW = 0.45

# tab_bar 只靠文案命中（hierarchy 不暴露 selected 属性，§17.1）时的折价
_TAB_TEXT_ONLY = 0.6
# required 信号明确不命中时的降权。不清零 —— 还要靠它排序，全零就没法给候选了
_REQUIRED_MISS_FACTOR = 0.25
# 两个候选差距小于此值即判 ambiguous
_AMBIGUOUS_DELTA = 0.08


def band_of(confidence: float) -> str:
    if confidence >= BAND_HIGH:
        return "high"
    if confidence >= BAND_LOW:
        return "explore"
    return "recover"


def localize(
    *,
    states: list[dict[str, Any]],
    nodes: list[dict[str, Any]] | None = None,
    session_block: str = "",
    last_edge_to: str = "",
    last_edge_passed: bool = False,
    weights: dict[str, float] | None = None,
    extra_signals: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """返回 §2.1 的输出形状。`extra_signals` 形如 `{state_id: {"vlm_screen": 0.8}}`。"""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    nodes = list(nodes or [])
    degraded = not nodes

    candidates: list[dict[str, Any]] = []
    for state in states or []:
        sid = str(state.get("id") or state.get("state_id") or "").strip()
        if not sid:
            continue
        signals, required_miss = _score_state(
            state,
            nodes=nodes,
            session_block=session_block,
            last_edge_to=last_edge_to,
            last_edge_passed=last_edge_passed,
            state_id=sid,
        )
        for name, val in (extra_signals or {}).get(sid, {}).items():
            signals[name] = float(val)
        conf = _weighted(signals, w)
        if required_miss:
            conf *= _REQUIRED_MISS_FACTOR
        candidates.append(
            {
                "state_id": sid,
                "confidence": round(conf, 4),
                "signals": {k: round(v, 3) for k, v in signals.items()},
                "required_miss": required_miss,
            }
        )

    candidates.sort(key=lambda c: (-c["confidence"], c["state_id"]))
    top = candidates[0] if candidates else None
    second = candidates[1] if len(candidates) > 1 else None
    conf = float(top["confidence"]) if top else 0.0
    ambiguous = bool(
        top and second and (conf - float(second["confidence"])) < _AMBIGUOUS_DELTA and conf > 0
    )
    return {
        "candidates": candidates[:5],
        "chosen": str(top["state_id"]) if top and conf > 0 else "",
        "confidence": round(conf, 4),
        "ambiguous": ambiguous,
        "band": band_of(conf),
        "degraded": degraded,
    }


def _weighted(signals: dict[str, float], weights: dict[str, float]) -> float:
    """只对**评得出来**的信号做加权平均。判不了的信号不该拉低也不该抬高置信度。"""
    total = 0.0
    hit = 0.0
    for name, val in signals.items():
        weight = float(weights.get(name, 0.5))
        total += weight
        hit += weight * float(val)
    return hit / total if total > 0 else 0.0


def _score_state(
    state: dict[str, Any],
    *,
    nodes: list[dict[str, Any]],
    session_block: str,
    last_edge_to: str,
    last_edge_passed: bool,
    state_id: str,
) -> tuple[dict[str, float], bool]:
    identify = state.get("identify") if isinstance(state.get("identify"), dict) else {}
    signals: dict[str, float] = {}
    required_miss = False

    for bucket, is_required in (("required", True), ("optional", False)):
        for spec in identify.get(bucket) or []:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("signal") or "").strip()
            if not name:
                continue
            val = _eval_signal(
                name,
                spec,
                nodes=nodes,
                session_block=session_block,
                last_edge_to=last_edge_to,
                last_edge_passed=last_edge_passed,
                state_id=state_id,
            )
            if val is None:  # 判不了（没 hierarchy / 没这信号），不计入
                continue
            # 同名信号出现多次取最高分：多条 landmark 规则命中任一即可
            signals[name] = max(signals.get(name, 0.0), val)
            if is_required and val <= 0.0:
                required_miss = True
    return signals, required_miss


def _eval_signal(
    name: str,
    spec: dict[str, Any],
    *,
    nodes: list[dict[str, Any]],
    session_block: str,
    last_edge_to: str,
    last_edge_passed: bool,
    state_id: str,
) -> Optional[float]:
    """返回 0..1；`None` 表示这条信号本轮判不了。"""
    if name == "session":
        return _eval_session(spec, session_block)
    if name == "history":
        if not last_edge_to:
            return None
        return 1.0 if (last_edge_passed and last_edge_to == state_id) else 0.0
    if not nodes:
        return None  # 剩下的都要 hierarchy，没有就判不了（§10.0.2 降级）
    if name == "tab_bar":
        return _eval_tab_bar(spec, nodes)
    if name == "layout_framework":
        from mino_nexus.services.nav_layout import match_layout_framework

        return match_layout_framework(nodes, spec)
    if name == "text_landmarks":
        return _eval_landmarks(spec, nodes)
    if name == "hierarchy":
        return 1.0 if match_any(nodes, spec.get("match_any") or []) else 0.0
    return None


def _eval_session(spec: dict[str, Any], session_block: str) -> Optional[float]:
    blob = str(session_block or "").strip()
    if not blob:
        return None
    match = spec.get("match") if isinstance(spec.get("match"), dict) else {}
    if not match:
        return None
    for key, want in match.items():
        if f"{key}={want}" not in blob:
            return 0.0
    return 1.0


def _eval_tab_bar(spec: dict[str, Any], nodes: list[dict[str, Any]]) -> Optional[float]:
    """hierarchy 不暴露 `selected`（§17.1），所以文案命中只能算部分证据。

    配置里给了 `selected_resource_id_regex` 才算强证据 —— 那是校准时实测出来的选中态 id。
    """
    match = spec.get("match") if isinstance(spec.get("match"), dict) else {}
    rid_re = str(match.get("selected_resource_id_regex") or "").strip()
    if rid_re and match_any(nodes, [{"resource_id_regex": rid_re}]):
        return 1.0
    label = str(match.get("selected") or "").strip()
    if not label:
        return None if not rid_re else 0.0
    node = match_any(nodes, [{"text_eq": label}, {"content_desc_contains": label}])
    if node is None:
        return 0.0
    # 有 selected 属性就用（未来 Scout 若补上这个字段，这里自动变强证据）
    if node.get("selected") is True:
        return 1.0
    return _TAB_TEXT_ONLY


def _eval_landmarks(spec: dict[str, Any], nodes: list[dict[str, Any]]) -> Optional[float]:
    any_terms = spec.get("any") or spec.get("any_of") or []
    none_terms = spec.get("none_of") or spec.get("none") or []
    if not any_terms and not none_terms:
        return None
    if none_terms and match_any(nodes, none_terms) is not None:
        return 0.0
    if not any_terms:
        return 1.0
    return 1.0 if match_any(nodes, any_terms) is not None else 0.0
