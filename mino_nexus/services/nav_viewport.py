"""视口维度：有限/无限高宽 + layout_class（结构信号，不硬编码 App 文案）。"""
from __future__ import annotations

from typing import Any


def classify_viewport_extent(
    framework: dict[str, Any] | None,
    wireframe: dict[str, Any] | None,
) -> dict[str, Any]:
    """返回 layout_extent + layout_class，写入 Atlas state.meta。"""
    from mino_nexus.services.nav_app_skeleton import infer_scroll_semantic

    fw = dict(framework or {})
    kind = str(fw.get("kind") or "").lower()
    scroll = infer_scroll_semantic(fw, wireframe)
    regions = (wireframe or {}).get("regions") or [] if isinstance(wireframe, dict) else []

    width_finite = True
    height_finite = True
    horiz_pager = scroll == "horizontal_pager" or "pager" in kind or "carousel" in kind
    if horiz_pager:
        width_finite = False
    wide_cols = sum(
        1
        for r in regions
        if float((r.get("rect") or {}).get("w") or 0) > 0.45
    )
    if wide_cols >= 2 and horiz_pager:
        width_finite = False

    feed_like = scroll == "vertical_list" or "feed" in kind or "timeline" in kind
    if feed_like or scroll == "nested_scroll":
        height_finite = False

    layout_class = "unknown"
    if kind == "dialog" or fw.get("overlay"):
        layout_class = "transient_overlay"
    elif horiz_pager:
        layout_class = "horizontal_pager"
    elif not height_finite and feed_like:
        layout_class = "infinite_feed"
    elif height_finite and width_finite:
        layout_class = "fixed_viewport"
    elif not height_finite:
        layout_class = "infinite_feed"

    return {
        "layout_class": layout_class,
        "layout_extent": {
            "height": "finite" if height_finite else "infinite",
            "width": "finite" if width_finite else "infinite",
        },
    }


def evidence_tier_from_counts(
    *,
    visit_count: int,
    session_count: int = 0,
    morph_count: int = 0,
) -> str:
    """逻辑页证据量（morph 不抬高 tier）。visit 按逻辑页计。"""
    vc = max(0, int(visit_count))
    sc = max(0, int(session_count))
    if vc >= 8 or sc >= 3:
        return "high"
    if vc >= 3 or sc >= 2:
        return "medium"
    return "low"
