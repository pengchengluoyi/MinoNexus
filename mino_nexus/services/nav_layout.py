"""NavFSM 屏面结构识别：布局框架 + 控件槽位，不把流式商品文案写进 identify。"""
from __future__ import annotations

import re
from typing import Any

_PRICE_RE = re.compile(r"^[\d.]+$|^¥|^#")
_VOLATILE_RE = re.compile(r"^\d+(\.\d+)?$|^#.+$|^\d+分钟|^\d+小时")
_BACK_RID_RE = re.compile(r"back|navigate_up|up_button", re.I)
_WIDGET_ORDER = (
    "search_bar",
    "carousel",
    "banner",
    "action_row",
    "dropdown",
    "grid_2col",
    "list_rows",
    "tab_shell",
)


def _bounds(node: dict[str, Any]) -> tuple[int, int, int, int] | None:
    b = node.get("bounds") or []
    if isinstance(b, (list, tuple)) and len(b) >= 4:
        return int(b[0]), int(b[1]), int(b[2]), int(b[3])
    return None


def _screen_width(nodes: list[dict[str, Any]]) -> int:
    w = 0
    for node in nodes:
        b = _bounds(node)
        if b:
            w = max(w, b[2])
    return w or 1080


def is_volatile_text(text: str) -> bool:
    """流式/商品/价格类文案，不能进 identify。"""
    val = str(text or "").strip()
    if len(val) < 2 or len(val) > 64:
        return True
    if _PRICE_RE.match(val) or _VOLATILE_RE.match(val):
        return True
    if val.startswith("#") and len(val) > 12:
        return True
    if re.search(r"\d{3,}", val):
        return True
    return False


def stable_chrome_texts(
    sample: dict[str, Any],
    *,
    y_tab_max: int,
    exclude: set[str],
    limit: int = 4,
) -> list[str]:
    """Tab 内子页可用的稳定 chrome 文案：顶栏短文本，不含 feed 流式内容。"""
    out: list[str] = []
    chrome_y = int(y_tab_max * 0.42)
    for node in sample.get("nodes") or []:
        b = _bounds(node)
        if not b or b[3] > chrome_y:
            continue
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if not text or text in exclude or is_volatile_text(text):
            continue
        if 2 <= len(text) <= 16 and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _content_cards(
    nodes: list[dict[str, Any]],
    *,
    y_tab_max: int,
    content_top_px: int,
    exclude: set[str],
    screen_w: int,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for node in nodes:
        b = _bounds(node)
        if not b:
            continue
        x1, y1, x2, y2 = b
        if y2 > y_tab_max or y1 < content_top_px:
            continue
        w, h = x2 - x1, y2 - y1
        if w < 72 or h < 56 or w > int(screen_w * 0.92):
            continue
        text = str(node.get("text") or "").strip()
        if text in exclude:
            continue
        clickable = bool(node.get("clickable"))
        if not clickable and not text:
            continue
        if w > int(screen_w * 0.62):
            continue
        cards.append({"cx": (x1 + x2) / 2.0, "cy": (y1 + y2) / 2.0, "w": w, "h": h, "y1": y1})
    return cards


def _profile_stat_hits(
    nodes: list[dict[str, Any]],
    *,
    y_tab_max: int,
    exclude: set[str],
) -> int:
    """个人页顶栏常见多组「数字 + 短标签」结构，不依赖 App 文案。"""
    hits = 0
    chrome_y = int(y_tab_max * 0.42)
    for node in nodes:
        b = _bounds(node)
        if not b or b[3] > chrome_y:
            continue
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if not text or text in exclude:
            continue
        if re.match(r"^[\d,.+万千百]+$", text):
            hits += 1
        elif 2 <= len(text) <= 8 and not is_volatile_text(text):
            hits += 1
    return hits


def _detect_widgets(
    nodes: list[dict[str, Any]],
    *,
    y_tab_max: int,
    exclude: set[str],
    screen_w: int,
    cards: list[dict[str, Any]],
) -> list[str]:
    widgets: list[str] = []

    for node in nodes:
        b = _bounds(node)
        if not b or b[3] > y_tab_max:
            continue
        x1, y1, x2, y2 = b
        w, h = x2 - x1, y2 - y1
        cls = str(node.get("class") or "").lower()
        rid = str(node.get("resource_id") or "").lower()
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if text in exclude:
            continue
        if y2 < 320 and w > screen_w * 0.5 and 48 <= h <= 140:
            if "edit" in cls or "search" in rid:
                widgets.append("search_bar")
                break

    top_band = [c for c in cards if c["y1"] < y_tab_max * 0.35]
    if len(top_band) >= 3:
        ys = sorted({int(c["cy"]) for c in top_band})
        if len(ys) <= 3:
            xs = sorted(c["cx"] for c in top_band)
            if xs[-1] - xs[0] > screen_w * 0.45:
                widgets.append("carousel")

    for node in nodes:
        b = _bounds(node)
        if not b or b[3] > y_tab_max:
            continue
        x1, y1, x2, y2 = b
        w, h = x2 - x1, y2 - y1
        if y2 < 260 and w > screen_w * 0.82 and 80 <= h <= 280:
            widgets.append("banner")
            break

    action_y = [c for c in cards if c["y1"] < 420]
    if len(action_y) >= 2 and len(action_y) <= 6:
        heights = [c["h"] for c in action_y]
        if max(heights) - min(heights) < 80:
            widgets.append("action_row")

    for node in nodes:
        b = _bounds(node)
        if not b or b[3] > y_tab_max:
            continue
        cls = str(node.get("class") or "").lower()
        text = str(node.get("text") or "").strip()
        if "spinner" in cls:
            widgets.append("dropdown")
            break

    if len(cards) >= 4:
        mid = screen_w / 2.0
        left = [c for c in cards if c["cx"] < mid]
        right = [c for c in cards if c["cx"] >= mid]
        if len(left) >= 2 and len(right) >= 2:
            widgets.append("grid_2col")
    elif len(cards) >= 3:
        span = max(c["cx"] for c in cards) - min(c["cx"] for c in cards)
        if span < screen_w * 0.4:
            widgets.append("list_rows")

    out: list[str] = []
    for key in _WIDGET_ORDER:
        if key in widgets and key not in out:
            out.append(key)
    return out


def detect_layout_framework(
    sample: dict[str, Any],
    *,
    y_tab_max: int,
    exclude: set[str] | None = None,
    content_top_px: int | None = None,
) -> dict[str, Any]:
    """从 hierarchy 推断布局框架 + 控件槽位（不依赖具体商品文案）。"""
    exclude = exclude or set()
    nodes = sample.get("nodes") or []
    if not nodes:
        return {"kind": "unknown", "columns": 0, "widgets": []}

    from mino_nexus.services.nav_screen_layout import infer_content_bands

    bands = infer_content_bands(nodes)
    screen_w = int(bands.get("screen_w") or _screen_width(nodes))
    top_px = int(content_top_px if content_top_px is not None else bands.get("content_top_px") or 0)
    y_tab_max = min(int(y_tab_max), int(bands.get("content_bottom_px") or y_tab_max))
    texts = [
        str(n.get("text") or n.get("content_desc") or "").strip()
        for n in nodes
        if str(n.get("text") or n.get("content_desc") or "").strip()
    ]
    has_back = any(
        _BACK_RID_RE.search(str(n.get("resource_id") or ""))
        or "navigate up" in str(n.get("content_desc") or "").lower()
        for n in nodes
    )
    chrome = stable_chrome_texts(sample, y_tab_max=y_tab_max, exclude=exclude)
    cards = _content_cards(
        nodes, y_tab_max=y_tab_max, content_top_px=top_px, exclude=exclude, screen_w=screen_w
    )
    widgets = _detect_widgets(nodes, y_tab_max=y_tab_max, exclude=exclude, screen_w=screen_w, cards=cards)

    profile_hits = _profile_stat_hits(nodes, y_tab_max=y_tab_max, exclude=exclude)
    if profile_hits >= 4:
        return {"kind": "profile_page", "columns": 1, "widgets": widgets, **({"has_back": True} if has_back else {})}

    if has_back and len(chrome) >= 1:
        return {"kind": "detail_page", "columns": 1, "widgets": widgets, "has_back": True}

    if "grid_2col" in widgets:
        return {"kind": "feed_grid", "columns": 2, "widgets": widgets}
    if "list_rows" in widgets:
        return {"kind": "feed_list", "columns": 1, "widgets": widgets}
    if widgets:
        return {"kind": "content_page", "columns": 1, "widgets": widgets}
    if chrome:
        return {"kind": "chrome_page", "columns": 1, "widgets": widgets}

    return {"kind": "tab_shell", "columns": 0, "widgets": ["tab_shell"]}


_FEED_LIKE_KINDS = frozenset({"feed_grid", "feed_list", "content_page", "tab_shell", "unknown"})
_SEMANTIC_SUB_KINDS = frozenset({"profile_page", "detail_page"})
_CHROMELESS_FP_KINDS = _FEED_LIKE_KINDS

_KIND_PRIORITY = ("feed_grid", "feed_list", "content_page", "chrome_page", "tab_shell", "unknown")


def semantic_page_role(fw: dict[str, Any] | None) -> str:
    """业务子页角色：feed / profile / detail / main。控件组合不单独成页。"""
    data = fw if isinstance(fw, dict) else {}
    kind = str(data.get("kind") or "")
    if kind == "profile_page":
        return "profile"
    if kind == "detail_page" or data.get("has_back"):
        return "detail"
    if kind in _FEED_LIKE_KINDS:
        return "feed"
    return "main"


def merge_frameworks(frameworks: list[dict[str, Any]]) -> dict[str, Any]:
    """合并同一业务页内多次采集见到的控件槽位（并集），不因滚动少识别一个轮播就多建一页。"""
    rows = [dict(fw) for fw in frameworks if isinstance(fw, dict)]
    if not rows:
        return {"kind": "tab_shell", "columns": 0, "widgets": []}
    kinds = [str(r.get("kind") or "") for r in rows]
    kind = next((k for k in _KIND_PRIORITY if k in kinds), kinds[0] or "content_page")
    widgets: list[str] = []
    has_back = False
    columns = 0
    for row in rows:
        if row.get("has_back"):
            has_back = True
        columns = max(columns, int(row.get("columns") or 0))
        for w in row.get("widgets") or []:
            key = str(w or "").strip()
            if key and key not in widgets:
                widgets.append(key)
    out: dict[str, Any] = {"kind": kind, "columns": columns, "widgets": widgets}
    if has_back:
        out["has_back"] = True
    return out


def layout_fp_from_match(match: dict[str, Any] | None) -> str:
    """与 `framework_fingerprint` 第一分量一致，用于跨发布对齐子页面。"""
    fw = match if isinstance(match, dict) else {}
    kind = str(fw.get("kind") or "unknown")
    widgets = fw.get("widgets") or []
    parts = [str(w) for w in widgets if str(w).strip()]
    return f"{kind}|{'+'.join(parts) if parts else 'none'}"


def chrome_key_from_state_blocks(blocks: list[dict[str, Any]]) -> tuple[str, ...]:
    stable: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("signal") != "text_landmarks":
            continue
        for text in block.get("any") or []:
            val = str(text or "").strip()
            if val and not is_volatile_text(val) and val not in stable:
                stable.append(val)
    return tuple(sorted(stable)[:4])


def sub_page_identity_from_state(st: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    """子页面稳定键：(tab, 业务角色, chrome_key)。feed 不按控件组合拆页。"""
    identify = st.get("identify") or {}
    required = identify.get("required")
    blocks = required if isinstance(required, list) else ([required] if required else [])
    tab = ""
    fw_match: dict[str, Any] | None = None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("signal") == "tab_bar":
            tab = str((block.get("match") or {}).get("selected") or "").strip()
        if block.get("signal") == "layout_framework":
            fw_match = dict(block.get("match") or {})
    role = semantic_page_role(fw_match)
    chrome_key = () if role in ("feed", "main") else chrome_key_from_state_blocks(blocks)
    return tab, role, chrome_key


def framework_fingerprint(fw: dict[str, Any], chrome: list[str]) -> tuple[str, tuple[str, ...]]:
    role = semantic_page_role(fw)
    fp = f"role:{role}"
    if role in ("feed", "main"):
        return fp, ()
    stable = tuple(sorted({t for t in chrome if t and not is_volatile_text(t)})[:4])
    return fp, stable


def match_layout_framework(nodes: list[dict[str, Any]], spec: dict[str, Any], *, y_tab_max: int = 99999) -> float:
    """localize 用：当前屏是否匹配配置的 layout_framework。"""
    match = spec.get("match") if isinstance(spec.get("match"), dict) else {}
    want_kind = str(match.get("kind") or "").strip()
    if not want_kind:
        return 0.0
    detected = detect_layout_framework({"nodes": nodes}, y_tab_max=y_tab_max, exclude=set())
    if str(detected.get("kind") or "") != want_kind:
        return 0.0
    want_cols = match.get("columns")
    if want_cols is not None and int(detected.get("columns") or 0) != int(want_cols):
        return 0.0
    if match.get("has_back") and not detected.get("has_back"):
        return 0.0
    want_widgets = match.get("widgets")
    if isinstance(want_widgets, list) and want_widgets:
        have = set(detected.get("widgets") or [])
        if not set(str(w) for w in want_widgets).issubset(have):
            return 0.0
    return 1.0
