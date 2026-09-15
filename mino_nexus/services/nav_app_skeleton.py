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


def split_indices_by_wireframe_similarity(
    wireframes: list[dict[str, Any] | None],
    *,
    min_jaccard: float = WIRE_FRAME_MERGE_JACCARD,
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
        else:
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
    if not kept_keys:
        best = max(clean, key=lambda w: len(w.get("regions") or []))
        return dict(best)
    kept_keys.sort(
        key=lambda k: (
            float((sample_by_key[k].get("rect") or {}).get("y") or 0),
            float((sample_by_key[k].get("rect") or {}).get("x") or 0),
        )
    )
    base = dict(clean[-1])
    base["regions"] = [dict(sample_by_key[k]) for k in kept_keys]
    base["skeleton_merged_visits"] = n
    return base


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
