"""应用骨骼：屏面聚类键与多访线框去噪（不依赖固定三层带布局假设）。"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any


def _short_hash(text: str, n: int = 10) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[: max(4, n)]


def _region_key(region: dict[str, Any]) -> str:
    rect = region.get("rect") or {}
    src = str(region.get("source") or "").strip()
    label = str(region.get("label") or "").strip()
    cls = str(region.get("class_name") or "").strip()
    if label == "FrameLayout" and cls == "FrameLayout":
        return ""
    x = round(float(rect.get("x") or 0), 2)
    y = round(float(rect.get("y") or 0), 2)
    w = round(float(rect.get("w") or 0), 2)
    h = round(float(rect.get("h") or 0), 2)
    return f"{src}:{label or cls}:{x}:{y}:{w}:{h}"


def normalize_framework_for_cluster(framework: dict[str, Any]) -> dict[str, Any]:
    """聚类用框架：tab_shell 与 Tab 主列表视为同一壳层。"""
    out = dict(framework or {})
    if str(out.get("kind") or "") == "tab_shell":
        out["kind"] = "profile_page"
    return out


def infer_scroll_semantic(framework: dict[str, Any], wireframe: dict[str, Any] | None) -> str:
    kind = str(framework.get("kind") or "").strip().lower()
    if "pager" in kind or "carousel" in kind or "gallery" in kind:
        return "horizontal_pager"
    if "feed" in kind or "list" in kind or "timeline" in kind:
        return "vertical_list"
    if framework.get("has_nested_scroll") or framework.get("nested_scroll"):
        return "nested_scroll"
    regions = (wireframe or {}).get("regions") or []
    horiz = sum(1 for r in regions if float((r.get("rect") or {}).get("w") or 0) > 0.55)
    if horiz >= 2:
        return "horizontal_pager"
    tall = sum(
        1
        for r in regions
        if float((r.get("rect") or {}).get("h") or 0) >= 0.12
    )
    if tall >= 3:
        return "vertical_list"
    return "none"


def shell_band_signature(wireframe: dict[str, Any] | None) -> str:
    """顶/底壳线框（量化坐标），不含中间流式列表区域。"""
    return shell_cluster_signature(wireframe, fine=True)


def shell_cluster_signature(wireframe: dict[str, Any] | None, *, fine: bool = False) -> str:
    """聚类用壳层签名：粗量化，忽略流式正文 label。"""
    if not isinstance(wireframe, dict):
        return "empty"
    step = 0.1 if fine else 0.25
    bits: list[str] = []
    for r in wireframe.get("regions") or []:
        rect = r.get("rect") or {}
        y = float(rect.get("y") or 0)
        h = float(rect.get("h") or 0)
        if y > 0.16 and y + h < 0.84:
            continue
        x = round(float(rect.get("x") or 0) / step) * step
        w = round(float(rect.get("w") or 0) / step) * step
        y = round(y / step) * step
        h = round(h / step) * step
        cls = str(r.get("class_name") or r.get("source") or "r")[:20]
        bits.append(f"{cls}:{x:.2f},{y:.2f},{w:.2f},{h:.2f}")
    bits.sort()
    if not bits:
        return "empty"
    return _short_hash("|".join(bits[:16]), 10)


def chrome_has_nav_back(chrome: list[str]) -> bool:
    return any(str(c or "").strip() in ("返回", "Back") for c in chrome)


def is_feed_surface_chrome(title: str) -> bool:
    """主列表/个人页顶栏统计、货号等 —— 不用于拆子页桶。"""
    from mino_nexus.services.nav_layout import is_volatile_text

    val = str(title or "").strip()
    if not val or is_volatile_text(val):
        return True
    if any(x in val for x in ("粉丝", "订单", "关注", "获赞")):
        return True
    if re.match(r"^G[-_]", val):
        return True
    return val in ("返回",)


def chrome_cluster_discriminator(
    turn: dict[str, Any],
    framework: dict[str, Any],
    *,
    tab: str,
    tab_labels: list[str] | None,
    y_tab_max: int,
    exclude_tabs: set[str] | None,
) -> str:
    """子页拆分：真实标题拆桶；主 Feed 面统一为 main。"""
    from mino_nexus.services.nav_layout import stable_chrome_texts

    fw = framework or {}
    kind = str(fw.get("kind") or "")
    has_back = bool(fw.get("has_back"))
    exclude = exclude_tabs or set()
    chrome = stable_chrome_texts(turn, y_tab_max=y_tab_max or 9999, exclude=exclude)
    title = chrome_header_title(chrome, tab=tab, tab_labels=tab_labels or [])
    chrome_has_back = chrome_has_nav_back(chrome)
    subpage = kind == "detail_page" or has_back or chrome_has_back

    if subpage:
        if title and not is_feed_surface_chrome(title):
            return _short_hash(title, 8)
        return "detail"

    if title and is_feed_surface_chrome(title):
        return "main"

    return "main"


STRUCT_MIN_REGION_KEYS = 3
WIRE_FRAME_MERGE_JACCARD = 0.65
MORPH_JACCARD_MIN = 0.35
SCROLL_MORPH_JACCARD_MIN = 0.12
VIEWPORT_TRANSFORM_JACCARD_MIN = 0.72
VIEWPORT_CROP_CONTENT_Y_MIN = 0.42
VIEWPORT_LETTERBOX_INSET_MIN = 0.06
# vision 渠道常把多任务/桌面叠在 App 屏上；不参与视口 match / 壳层 Tab
VISION_MATCH_IGNORE_LABEL_SUBSTR = (
    "应用图标网格",
    "搜索框",
    "底部应用栏",
    "底部应用",
    "桌面",
    "launcher",
)


def region_source_channel(region: dict[str, Any]) -> str:
    return str(region.get("source") or "").strip().lower()


def vision_region_ignored_for_match(region: dict[str, Any]) -> bool:
    if region_source_channel(region) != "vision":
        return False
    lab = str(region.get("label") or "")
    low = lab.lower()
    if any(x in lab for x in VISION_MATCH_IGNORE_LABEL_SUBSTR):
        return True
    if "launcher" in low or "app drawer" in low:
        return True
    return False


def regions_for_viewport_match(wireframe: dict[str, Any] | None) -> list[dict[str, Any]]:
    """视口/聚类比对：hierarchy 为主；vision 仅作补充，且排除 launcher 类误检。"""
    if not isinstance(wireframe, dict):
        return []
    out: list[dict[str, Any]] = []
    for r in wireframe.get("regions") or []:
        if not isinstance(r, dict) or vision_region_ignored_for_match(r):
            continue
        src = region_source_channel(r)
        if src == "vision":
            if region_is_fullscreen_container(r):
                continue
            out.append(r)
            continue
        if region_is_shell(r) or region_is_fullscreen_container(r):
            continue
        out.append(r)
    return out


def content_regions_for_match(wireframe: dict[str, Any] | None) -> list[dict[str, Any]]:
    """展示/多态等仍用 hierarchy 为主，并去掉 vision launcher 噪声。"""
    return regions_for_viewport_match(wireframe)


def _content_bbox(regs: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    x2: list[float] = []
    y2: list[float] = []
    for r in regs:
        rect = r.get("rect") or {}
        x = float(rect.get("x") or 0)
        y = float(rect.get("y") or 0)
        w = float(rect.get("w") or 0)
        h = float(rect.get("h") or 0)
        if w <= 0 or h <= 0:
            continue
        xs.append(x)
        ys.append(y)
        x2.append(x + w)
        y2.append(y + h)
    if not xs:
        return 0.0, 0.0, 1.0, 1.0
    mx, my = min(xs), min(ys)
    bw = max(max(x2) - mx, 0.05)
    bh = max(max(y2) - my, 0.05)
    return mx, my, bw, bh


def _region_key_aligned(
    region: dict[str, Any],
    *,
    src_bbox: tuple[float, float, float, float],
    dst_bbox: tuple[float, float, float, float],
    grid: float = 0.08,
) -> str:
    rect = region.get("rect") or {}
    x = float(rect.get("x") or 0)
    y = float(rect.get("y") or 0)
    w = float(rect.get("w") or 0)
    h = float(rect.get("h") or 0)
    if w <= 0 or h <= 0:
        return ""
    mx, my, bw, bh = src_bbox
    dx, dy, dbw, dbh = dst_bbox
    nx = (x - mx) / bw if bw else 0.0
    ny = (y - my) / bh if bh else 0.0
    nw = w / bw if bw else w
    nh = h / bh if bh else h
    ax = dx + nx * dbw
    ay = dy + ny * dbh
    aw = nw * dbw
    ah = nh * dbh
    cls = str(region.get("class_name") or region.get("source") or "r").split(".")[-1][:24]

    def _q(v: float) -> float:
        return round(v / grid) * grid

    return f"{cls}:{_q(ax):.2f},{_q(ay):.2f},{_q(aw):.2f},{_q(ah):.2f}"


def wireframe_aligned_transform_keys(
    wireframe: dict[str, Any] | None,
    *,
    ref_wireframe: dict[str, Any] | None = None,
) -> set[str]:
    regs = regions_for_viewport_match(wireframe)
    if not regs:
        return set()
    ref_regs = regions_for_viewport_match(ref_wireframe) if ref_wireframe else regs
    ref_bb = _content_bbox(ref_regs)
    var_bb = _content_bbox(regs)
    out: set[str] = set()
    for r in regs:
        k = _region_key_aligned(r, src_bbox=var_bb, dst_bbox=ref_bb)
        if k:
            out.add(k)
    return out


def wireframe_aligned_transform_jaccard(
    canonical: dict[str, Any] | None,
    variant: dict[str, Any] | None,
) -> float:
    if not isinstance(canonical, dict) or not isinstance(variant, dict):
        return 0.0
    ka = wireframe_aligned_transform_keys(canonical, ref_wireframe=canonical)
    kb = wireframe_aligned_transform_keys(variant, ref_wireframe=canonical)
    if not ka or not kb:
        return 0.0
    inter = len(ka & kb)
    union = len(ka | kb)
    return inter / union if union else 0.0


def _region_title_token(region: dict[str, Any]) -> str:
    lab = str(region.get("label") or "").strip()
    head = lab.split("\n")[0].strip()
    if len(head) < 2:
        return ""
    if head in ("0", "View", "ImageView", "LinearLayout", "ScrollView"):
        return ""
    if head.isdigit():
        return ""
    return head[:32]


def wireframe_content_label_overlap(
    a: dict[str, Any] | None,
    b: dict[str, Any] | None,
) -> float:
    la = {_region_title_token(r) for r in content_regions_for_match(a)} - {""}
    lb = {_region_title_token(r) for r in content_regions_for_match(b)} - {""}
    if not la or not lb:
        return 0.0
    shared = la & lb
    return len(shared) / min(len(la), len(lb))


def shell_tab_text_set(wireframe: dict[str, Any] | None) -> set[str]:
    if not isinstance(wireframe, dict):
        return set()
    out: set[str] = set()
    for r in wireframe.get("regions") or []:
        if not isinstance(r, dict) or not region_is_shell(r):
            continue
        head = str(r.get("label") or "").strip().split("\n")[0].strip()
        if 1 <= len(head) <= 8:
            out.add(head)
    return out


def wireframe_looks_viewport_cropped(wireframe: dict[str, Any] | None) -> bool:
    """多任务/后台卡片：内容整体下移或仅露出下半屏。"""
    regs = content_regions_for_match(wireframe)
    if len(regs) < 2:
        return False
    ys = [float((r.get("rect") or {}).get("y") or 0) for r in regs]
    if ys and min(ys) >= VIEWPORT_CROP_CONTENT_Y_MIN:
        return True
    for r in regs:
        rect = r.get("rect") or {}
        y = float(rect.get("y") or 0)
        w = float(rect.get("w") or 0)
        h = float(rect.get("h") or 0)
        if y >= 0.48 and w >= 0.92 and h >= 0.32:
            return True
    return False


def _wireframe_has_surface_token(wireframe: dict[str, Any] | None, token: str) -> bool:
    if not isinstance(wireframe, dict):
        return False
    want = str(token or "").strip()
    if not want:
        return False
    for r in wireframe.get("regions") or []:
        if want in str(r.get("label") or ""):
            return True
    return False


def wireframe_looks_letterbox_inset(wireframe: dict[str, Any] | None) -> bool:
    """启动/多任务缩略图：内容区相对全屏四边内缩。"""
    regs = regions_for_viewport_match(wireframe)
    if len(regs) < 3:
        return False
    mx, my, bw, bh = _content_bbox(regs)
    if mx >= VIEWPORT_LETTERBOX_INSET_MIN and my >= VIEWPORT_LETTERBOX_INSET_MIN:
        if mx + bw <= 1.0 - VIEWPORT_LETTERBOX_INSET_MIN * 0.5:
            return True
    return False


def wireframes_same_page_under_viewport_transform(
    canonical: dict[str, Any] | None,
    variant: dict[str, Any] | None,
) -> bool:
    """启动缩放、后台多任务卡片等：与 canonical 为同一逻辑页，非真实跳转。"""
    if not isinstance(canonical, dict) or not isinstance(variant, dict):
        return False
    aligned = wireframe_aligned_transform_jaccard(canonical, variant)
    if aligned >= VIEWPORT_TRANSFORM_JACCARD_MIN:
        return True
    label_ov = wireframe_content_label_overlap(canonical, variant)
    shared_labels = len(
        {_region_title_token(r) for r in regions_for_viewport_match(canonical)}
        & {_region_title_token(r) for r in regions_for_viewport_match(variant)}
        - {""}
    )
    cropped = wireframe_looks_viewport_cropped(variant)
    letterbox = wireframe_looks_letterbox_inset(variant)
    if cropped and shared_labels >= 2 and label_ov >= 0.3:
        return True
    if cropped and label_ov >= 0.28:
        for tok in ("灵感", "我的"):
            if _wireframe_has_surface_token(canonical, tok) and _wireframe_has_surface_token(
                variant, tok
            ):
                return True
    if letterbox and shared_labels >= 2 and label_ov >= 0.22:
        return True
    if aligned >= 0.45 and label_ov >= 0.4:
        return True
    if aligned >= 0.32 and label_ov >= 0.22 and shared_labels >= 2:
        return True
    if aligned >= 0.35 and label_ov >= 0.55:
        return True
    return False


def region_is_shell(region: dict[str, Any]) -> bool:
    if region_source_channel(region) == "vision":
        return False
    rect = region.get("rect") or {}
    y = float(rect.get("y") or 0)
    h = float(rect.get("h") or 0)
    w = float(rect.get("w") or 0)
    if w * h > 0.82:
        return False
    # 顶栏 / 状态条（宽 feed 容器 y≈0.1 不算壳）
    if y <= 0.06 or (y <= 0.12 and h <= 0.14):
        return True
    # 底栏 Tab 条
    if y + h >= 0.92 and h <= 0.14:
        return True
    role = str(region.get("role") or "").lower()
    if "tab" in role or role in ("bottom_nav", "tab_bar"):
        return True
    # 底栏 Tab 槽（窄条），排除信息流宽卡片行
    if y + h >= 0.86 and h <= 0.07 and w <= 0.24:
        return True
    return False


def region_is_fullscreen_container(region: dict[str, Any]) -> bool:
    """ScrollView / 根布局等铺满屏的容器，不参与架构卡片展示。"""
    rect = region.get("rect") or {}
    w = float(rect.get("w") or 0)
    h = float(rect.get("h") or 0)
    if w > 0.88 and h > 0.72:
        return True
    if h > 0.9 and w > 0.85:
        return True
    return False


def merge_display_wireframe_for_atlas(
    wireframes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """架构卡片：初始帧内容 + 各帧并集底栏/顶栏（去重）。"""
    clean = [w for w in wireframes if isinstance(w, dict) and (w.get("regions") or [])]
    if not clean:
        return None
    initial = pick_initial_wireframe_frame(clean) or clean[0]
    out = dict(initial)
    seen_body: set[str] = set()
    body: list[dict[str, Any]] = []
    for r in initial.get("regions") or []:
        if not isinstance(r, dict) or region_is_shell(r) or region_is_fullscreen_container(r):
            continue
        k = region_key_structural(r)
        if k and k in seen_body:
            continue
        if k:
            seen_body.add(k)
        body.append(dict(r))
    shells: list[dict[str, Any]] = []
    seen_shell: set[str] = set()
    for wf in clean:
        for r in wf.get("regions") or []:
            if not isinstance(r, dict) or not region_is_shell(r) or region_is_fullscreen_container(r):
                continue
            rect = r.get("rect") or {}
            lab = str(r.get("label") or "").strip().split("\n")[0][:20]
            key = f"{lab}|{round(float(rect.get('y') or 0), 2)}"
            if key in seen_shell:
                continue
            seen_shell.add(key)
            shells.append(dict(r))
    shells.sort(
        key=lambda r: (
            float((r.get("rect") or {}).get("y") or 0),
            float((r.get("rect") or {}).get("x") or 0),
        )
    )
    out["regions"] = body + shells
    return out


def merge_shell_regions_into_wireframe(
    base: dict[str, Any],
    wireframes: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(base)
    regions = [dict(r) for r in (out.get("regions") or []) if isinstance(r, dict)]
    seen: set[str] = set()
    for r in regions:
        k = _region_key(r)
        if k:
            seen.add(k)

    def _add(r: dict[str, Any]) -> None:
        k = _region_key(r)
        if not k or k in seen:
            return
        seen.add(k)
        regions.append(dict(r))

    for wf in wireframes:
        if not isinstance(wf, dict):
            continue
        for r in wf.get("regions") or []:
            if isinstance(r, dict) and region_is_shell(r):
                _add(r)
    regions.sort(
        key=lambda r: (
            float((r.get("rect") or {}).get("y") or 0),
            float((r.get("rect") or {}).get("x") or 0),
        )
    )
    out["regions"] = regions
    return out


def pick_initial_wireframe_frame(wireframes: list[dict[str, Any]]) -> dict[str, Any] | None:
    clean = [w for w in wireframes if isinstance(w, dict) and (w.get("regions") or [])]
    if not clean:
        return None
    if len(clean) == 1:
        return dict(clean[0])

    preferred: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for w in clean:
        regs = w.get("regions") or []
        shell_n = sum(1 for r in regs if isinstance(r, dict) and region_is_shell(r))
        n = len(regs)
        if shell_n >= 2 and 15 <= n <= 50:
            preferred.append(((-shell_n, abs(n - 30)), w))
    if preferred:
        return dict(min(preferred, key=lambda item: item[0])[1])

    def _score(w: dict[str, Any]) -> tuple[int, int]:
        regs = w.get("regions") or []
        shell = sum(1 for r in regs if isinstance(r, dict) and region_is_shell(r))
        return (-shell, len(regs))

    return dict(min(clean, key=_score))


def annotate_wireframe_region_morph(
    wireframe: dict[str, Any] | None,
    *,
    layout_class: str = "",
    layout_extent: dict[str, Any] | None = None,
) -> None:
    if not isinstance(wireframe, dict):
        return
    ext = layout_extent if isinstance(layout_extent, dict) else {}
    lc = str(layout_class or "")
    height_inf = str(ext.get("height") or "") == "infinite"
    width_inf = str(ext.get("width") or "") == "infinite"
    for r in wireframe.get("regions") or []:
        if not isinstance(r, dict):
            continue
        if region_is_shell(r):
            r["morph_axis"] = "none"
            continue
        rect = r.get("rect") or {}
        y = float(rect.get("y") or 0)
        h = float(rect.get("h") or 0)
        w = float(rect.get("w") or 0)
        mid_band = 0.12 < y and y + h < 0.82
        role = str(r.get("role") or "").lower()
        feed_role = "feed" in role or "list" in role or "timeline" in role
        if width_inf or (lc == "horizontal_pager" and w > 0.45 and mid_band):
            r["morph_axis"] = "horizontal"
        elif height_inf or feed_role or (lc == "infinite_feed" and mid_band and h < 0.55):
            r["morph_axis"] = "vertical"
        else:
            r["morph_axis"] = "none"


def annotate_morph_axes_from_turn_samples(
    display: dict[str, Any] | None,
    turn_wireframes: list[dict[str, Any]] | None,
    *,
    layout_class: str = "",
    layout_extent: dict[str, Any] | None = None,
) -> None:
    """组件级多态：同一结构 key 在多帧 y/x 漂移 → 竖/横滑区。"""
    annotate_wireframe_region_morph(
        display,
        layout_class=layout_class,
        layout_extent=layout_extent,
    )
    if not isinstance(display, dict):
        return
    samples = [w for w in (turn_wireframes or []) if isinstance(w, dict) and (w.get("regions") or [])]
    if len(samples) < 2:
        return
    key_y: dict[str, set[float]] = {}
    key_x: dict[str, set[float]] = {}
    for wf in samples:
        for r in wf.get("regions") or []:
            if not isinstance(r, dict) or region_is_shell(r) or region_is_fullscreen_container(r):
                continue
            k = region_key_structural(r)
            if not k:
                continue
            rect = r.get("rect") or {}
            key_y.setdefault(k, set()).add(round(float(rect.get("y") or 0), 2))
            key_x.setdefault(k, set()).add(round(float(rect.get("x") or 0), 2))
    vert_keys = {k for k, ys in key_y.items() if len(ys) >= 2 and max(ys) - min(ys) >= 0.06}
    horiz_keys = {k for k, xs in key_x.items() if len(xs) >= 2 and max(xs) - min(xs) >= 0.08}
    for r in display.get("regions") or []:
        if not isinstance(r, dict) or region_is_shell(r):
            continue
        k = region_key_structural(r)
        if k in horiz_keys:
            r["morph_axis"] = "horizontal"
        elif k in vert_keys:
            r["morph_axis"] = "vertical"


def count_scroll_morph_variants(wireframes: list[dict[str, Any]] | None) -> int:
    """同页竖滑多态：与初始帧 Jaccard 落在 scroll 带内的采集帧数。"""
    clean = [w for w in (wireframes or []) if isinstance(w, dict) and (w.get("regions") or [])]
    if len(clean) < 2:
        return 0
    initial = pick_initial_wireframe_frame(clean) or clean[0]
    n = 0
    for w in clean:
        j = wireframe_jaccard(initial, w)
        if SCROLL_MORPH_JACCARD_MIN <= j < WIRE_FRAME_MERGE_JACCARD:
            n += 1
    return n


def turn_indicates_vertical_scroll(prev_turn: dict[str, Any] | None, turn: dict[str, Any]) -> bool:
    cap = str((turn or {}).get("cap_id") or "").strip()
    if cap == "swipe_direction":
        blob = str((turn or {}).get("selector_text") or (turn or {}).get("action_key") or "").lower()
        if any(x in blob for x in ("up", "down", "vertical", "上", "下", "scroll")):
            return True
    if prev_turn and turn:
        from mino_nexus.services.nav_screen_registry import _scroll_delta_px

        if _scroll_delta_px(prev_turn.get("nodes") or [], turn.get("nodes") or []) >= 48:
            return True
    return False


def wireframes_share_scroll_morph(
    wfa: dict[str, Any] | None,
    wfb: dict[str, Any] | None,
    framework: dict[str, Any] | None,
) -> bool:
    if shell_cluster_signature(wfa) != shell_cluster_signature(wfb):
        return False
    scroll = infer_scroll_semantic(framework or {}, wfa)
    if scroll not in ("vertical_list", "nested_scroll"):
        return False
    return wireframe_jaccard(wfa, wfb) >= SCROLL_MORPH_JACCARD_MIN


def region_key_structural(region: dict[str, Any], *, grid: float = 0.08) -> str:
    """聚类用语义：类名 + 粗几何，不用流式文案。"""
    rect = region.get("rect") or {}
    cls = str(region.get("class_name") or region.get("source") or "r").split(".")[-1][:24]
    x = round(float(rect.get("x") or 0) / grid) * grid
    y = round(float(rect.get("y") or 0) / grid) * grid
    w = round(float(rect.get("w") or 0) / grid) * grid
    h = round(float(rect.get("h") or 0) / grid) * grid
    if w <= 0 or h <= 0:
        return ""
    return f"{cls}:{x:.2f},{y:.2f},{w:.2f},{h:.2f}"


def wireframe_structural_keys(wireframe: dict[str, Any] | None) -> set[str]:
    if not isinstance(wireframe, dict):
        return set()
    out: set[str] = set()
    for r in wireframe.get("regions") or []:
        if not isinstance(r, dict):
            continue
        k = region_key_structural(r)
        if k:
            out.add(k)
    return out


def wireframe_jaccard(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float:
    ka, kb = wireframe_structural_keys(a), wireframe_structural_keys(b)
    if not ka or not kb:
        return 0.0
    inter = len(ka & kb)
    union = len(ka | kb)
    return inter / union if union else 0.0


def structural_cluster_token(wireframe: dict[str, Any] | None) -> str:
    """多组件骨骼 token；组件不足时不与「仅返回」类帧共用粗桶。"""
    keys = sorted(wireframe_structural_keys(wireframe))
    if len(keys) >= STRUCT_MIN_REGION_KEYS:
        return _short_hash("|".join(keys[:28]), 10)
    if keys:
        return _short_hash(f"sparse|{'|'.join(keys)}", 10)
    return wireframe_structure_signature(wireframe)


def should_coalesce_morph_clusters(
    wireframes: list[dict[str, Any] | None],
    clusters: list[list[int]],
) -> bool:
    """同 chrome、内容 Jaccard 在 morph 带内 → 不拆 *-sN。"""
    if len(clusters) <= 1:
        return False
    chrome: set[str] = set()
    indices: list[int] = []
    for cl in clusters:
        for i in cl:
            indices.append(i)
            if 0 <= i < len(wireframes):
                chrome.add(shell_cluster_signature(wireframes[i]))
    if len(chrome) != 1:
        return False
    pairs: list[float] = []
    for a in indices:
        for b in indices:
            if a < b:
                pairs.append(wireframe_jaccard(wireframes[a], wireframes[b]))
    if not pairs:
        return False
    mn, mx = min(pairs), max(pairs)
    if mn >= MORPH_JACCARD_MIN and mx < WIRE_FRAME_MERGE_JACCARD:
        return True
    if mn >= SCROLL_MORPH_JACCARD_MIN and mx < WIRE_FRAME_MERGE_JACCARD:
        return True
    aligned: list[float] = []
    for a in indices:
        for b in indices:
            if a < b and wireframes[a] and wireframes[b]:
                aligned.append(
                    wireframe_aligned_transform_jaccard(wireframes[a], wireframes[b])
                )
    if aligned and min(aligned) >= VIEWPORT_TRANSFORM_JACCARD_MIN:
        return True
    for a in indices:
        for b in indices:
            if a < b and wireframes[a] and wireframes[b]:
                if wireframes_same_page_under_viewport_transform(wireframes[a], wireframes[b]):
                    return True
    return False


def split_indices_by_wireframe_similarity(
    wireframes: list[dict[str, Any] | None],
    *,
    min_jaccard: float = WIRE_FRAME_MERGE_JACCARD,
    frameworks: list[dict[str, Any]] | None = None,
) -> list[list[int]]:
    """采集增多时：簇内 Jaccard 不够则拆成多页。"""
    n = len(wireframes)
    if n <= 1:
        return [list(range(n))]
    clusters: list[list[int]] = []
    for i in range(n):
        best_ci = -1
        best_sim = -1.0
        for ci, cl in enumerate(clusters):
            sims = [wireframe_jaccard(wireframes[i], wireframes[j]) for j in cl]
            avg = sum(sims) / len(sims) if sims else 0.0
            if avg > best_sim:
                best_sim = avg
                best_ci = ci
        if best_ci >= 0 and best_sim >= min_jaccard:
            clusters[best_ci].append(i)
            continue
        fw_i = (frameworks or [])[i] if frameworks and i < len(frameworks) else {}
        if (
            best_ci >= 0
            and frameworks is not None
            and wireframes_share_scroll_morph(
                wireframes[i],
                wireframes[clusters[best_ci][0]],
                fw_i,
            )
        ):
            clusters[best_ci].append(i)
            continue
        if (
            best_ci >= 0
            and wireframes[i]
            and wireframes[clusters[best_ci][0]]
            and wireframes_same_page_under_viewport_transform(
                wireframes[clusters[best_ci][0]],
                wireframes[i],
            )
        ):
            clusters[best_ci].append(i)
            continue
        if (
            best_ci >= 0
            and wireframe_aligned_transform_jaccard(
                wireframes[clusters[best_ci][0]],
                wireframes[i],
            )
            >= VIEWPORT_TRANSFORM_JACCARD_MIN
        ):
            clusters[best_ci].append(i)
            continue
        clusters.append([i])
    return clusters


def wireframe_structure_signature(wireframe: dict[str, Any] | None) -> str:
    if not isinstance(wireframe, dict):
        return "empty"
    regions = wireframe.get("regions") or []
    if not regions:
        return "empty"
    bits: list[str] = []
    for r in sorted(
        regions,
        key=lambda x: (
            round(float((x.get("rect") or {}).get("y") or 0), 3),
            round(float((x.get("rect") or {}).get("x") or 0), 3),
        ),
    ):
        rk = _region_key(r)
        if rk:
            bits.append(rk)
    if not bits:
        return "empty"
    return _short_hash("|".join(bits[:32]), 10)


def skeleton_fp_from_turn(
    turn: dict[str, Any],
    wireframe: dict[str, Any] | None,
    framework: dict[str, Any],
    *,
    tab: str = "",
    tab_labels: list[str] | None = None,
    y_tab_max: int = 0,
    exclude_tabs: set[str] | None = None,
    nav_edge_hint: str = "",
) -> str:
    """聚类粗车道 v9：仅 surface/kind/cols/scroll；细粒度合并由 Atlas 桶内 Jaccard 完成。"""
    raw_fw = framework or {}
    fw = normalize_framework_for_cluster(raw_fw)
    kind = str(fw.get("kind") or "unknown")
    cols = int(fw.get("columns") or 0)
    scroll = infer_scroll_semantic(fw, wireframe)
    subpage = kind == "detail_page" or bool(fw.get("has_back"))

    surface = "sub" if subpage else "main"
    body = f"{surface}|{kind}|{cols}|{scroll}"

    edge = str(nav_edge_hint or "").strip()
    raw = f"v9|{body}"
    if edge:
        raw = f"{raw}|nav:{edge}"
    return _short_hash(raw, 12)


def merge_skeleton_wireframe(
    wireframes: list[dict[str, Any]],
    *,
    min_visit_ratio: float = 0.5,
) -> dict[str, Any] | None:
    """多访去噪：保留在多数帧出现的 region；单次则返回当帧线框。"""
    clean = [w for w in wireframes if isinstance(w, dict) and (w.get("regions") or [])]
    if not clean:
        return wireframes[0] if wireframes and isinstance(wireframes[0], dict) else None
    if len(clean) == 1:
        return dict(clean[0])
    n = len(clean)
    need = max(1, math.ceil(min_visit_ratio * n))
    counts: Counter[str] = Counter()
    sample_by_key: dict[str, dict[str, Any]] = {}
    for wf in clean:
        seen_in_frame: set[str] = set()
        for r in wf.get("regions") or []:
            if not isinstance(r, dict):
                continue
            k = _region_key(r)
            if not k or k in seen_in_frame:
                continue
            seen_in_frame.add(k)
            counts[k] += 1
            sample_by_key.setdefault(k, r)
    kept_keys = [k for k, c in counts.items() if c >= need]
    for wf in clean:
        for r in wf.get("regions") or []:
            if not isinstance(r, dict) or not region_is_shell(r):
                continue
            k = _region_key(r)
            if k and k not in kept_keys:
                kept_keys.append(k)
                sample_by_key.setdefault(k, r)
    if not kept_keys:
        best = pick_initial_wireframe_frame(clean) or max(
            clean, key=lambda w: len(w.get("regions") or [])
        )
        return merge_shell_regions_into_wireframe(dict(best), clean)
    kept_keys.sort(
        key=lambda k: (
            float((sample_by_key[k].get("rect") or {}).get("y") or 0),
            float((sample_by_key[k].get("rect") or {}).get("x") or 0),
        )
    )
    initial = pick_initial_wireframe_frame(clean)
    base = dict(initial or clean[-1])
    base["regions"] = [dict(sample_by_key[k]) for k in kept_keys]
    base["skeleton_merged_visits"] = n
    return merge_shell_regions_into_wireframe(base, clean)


def chrome_header_title(chrome_texts: list[str], *, tab: str = "", tab_labels: list[str] | None = None) -> str:
    from mino_nexus.services.nav_layout import is_volatile_text

    tabs = {str(t or "").strip() for t in (tab_labels or []) if str(t or "").strip()}
    skip = tabs | {str(tab or "").strip(), "返回"}
    from mino_nexus.services.nav_layout import is_status_bar_chrome_text

    for text in chrome_texts:
        val = str(text or "").strip()
        if not val or val in skip or is_volatile_text(val) or is_status_bar_chrome_text(val):
            continue
        if 2 <= len(val) <= 24:
            return val
    return ""
