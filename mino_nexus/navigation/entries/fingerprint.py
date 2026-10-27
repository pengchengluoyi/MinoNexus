"""DOM 结构指纹 v2：用层级/节点几何/控件角色判页，而不是只看截图。

为什么不用纯图片
----------------
- 同一页换一张商品图，pHash 会漂；同一套页面骨架换文案，结构不漂。
- 采集链路本来就有 accessibility hierarchy，不需要把图像算法搬回 Nexus。
- 指纹可以解释：匹配不上时能指出是“少了一层容器”还是“入口锚点变了”。

指纹只编码结构（class、角色、可点击、子节点数、相对位置），正文/商品标题
只进 `anchors` 作为辅助证据。这样列表页滚动、文案变化不会把整页判成新页。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

_VERSION = 2
_MAX_NODES = 320
_MAX_ANCHORS = 80
_GENERIC_LABELS = {
    "确定", "取消", "关闭", "返回", "更多", "完成", "下一步", "上一步",
    "ok", "cancel", "close", "back", "more", "done", "next", "previous",
}


def _int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return default


def _bounds(node: dict[str, Any]) -> tuple[int, int, int, int] | None:
    raw = node.get("bounds")
    if isinstance(raw, dict):
        x1, y1 = _int(raw.get("x1", raw.get("left"))), _int(raw.get("y1", raw.get("top")))
        x2, y2 = _int(raw.get("x2", raw.get("right"))), _int(raw.get("y2", raw.get("bottom")))
    elif isinstance(raw, (list, tuple)) and len(raw) >= 4:
        x1, y1, x2, y2 = (_int(v) for v in raw[:4])
    elif isinstance(raw, str):
        nums = [int(v) for v in re.findall(r"-?\d+", raw)[:4]]
        if len(nums) < 4:
            return None
        x1, y1, x2, y2 = nums
    else:
        center = node.get("center") or {}
        if isinstance(center, (list, tuple)) and len(center) >= 2:
            return None
        width, height = _int(node.get("width")), _int(node.get("height"))
        if width <= 0 or height <= 0:
            return None
        cx, cy = _int(center.get("x")), _int(center.get("y"))
        x1, y1, x2, y2 = cx - width // 2, cy - height // 2, cx + width // 2, cy + height // 2
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _class_name(node: dict[str, Any]) -> str:
    raw = str(node.get("class") or node.get("class_name") or node.get("type") or "").strip()
    return raw.rsplit(".", 1)[-1][:40] or "Node"


def _label(node: dict[str, Any]) -> str:
    text = str(node.get("text") or "").strip()
    desc = str(node.get("content_desc") or node.get("desc") or "").strip()
    label = text or desc
    if not label:
        rid = str(node.get("resource_id") or node.get("id") or "").strip()
        label = rid.rsplit("/", 1)[-1] if rid else ""
    return re.sub(r"\s+", " ", label)[:80]


def _resource_id(node: dict[str, Any]) -> str:
    return str(node.get("resource_id") or node.get("id") or "").strip()[:120]


def _node_items(nodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    items: list[dict[str, Any]] = []
    max_x = max_y = 1
    for index, node in enumerate(nodes or []):
        if not isinstance(node, dict) or node.get("visible") is False:
            continue
        box = _bounds(node)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        max_x = max(max_x, x2)
        max_y = max(max_y, y2)
        area = max(0, x2 - x1) * max(0, y2 - y1)
        items.append({
            "index": index,
            "node": node,
            "box": box,
            "area": area,
            "class": _class_name(node),
            "label": _label(node),
            "rid": _resource_id(node),
            "clickable": bool(node.get("clickable")),
            "role": str(node.get("role") or "").strip()[:30],
        })
    if len(items) > _MAX_NODES:
        # 保留面积大的容器 + 可点击/有标签的节点，避免超大层级撑爆指纹。
        items.sort(key=lambda row: (row["area"], row["clickable"], bool(row["label"])), reverse=True)
        items = items[:_MAX_NODES]
        items.sort(key=lambda row: row["index"])
    return items, max_x, max_y


def _contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    return (
        outer[0] <= inner[0] + 2
        and outer[1] <= inner[1] + 2
        and outer[2] >= inner[2] - 2
        and outer[3] >= inner[3] - 2
    )


def _build_tree(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按包含关系建树。层级是扁平数组，没有 parent 字段，只能由几何反推。"""
    ordered = sorted(items, key=lambda row: (row["area"], row["index"]), reverse=True)
    roots: list[dict[str, Any]] = []
    for item in ordered:
        parent = None
        for candidate in ordered:
            if candidate is item:
                continue
            if candidate["area"] <= item["area"]:
                continue
            if _contains(candidate["box"], item["box"]):
                if parent is None or candidate["area"] < parent["area"]:
                    parent = candidate
        item["parent"] = parent
        if parent is None:
            roots.append(item)
    for item in items:
        item.setdefault("children", [])
    for item in items:
        parent = item.get("parent")
        if parent is not None:
            parent.setdefault("children", []).append(item)
    for item in items:
        item["children"].sort(key=lambda row: (row["box"][1], row["box"][0], -row["area"]))
    roots.sort(key=lambda row: (row["box"][1], row["box"][0], -row["area"]))
    return roots


def _walk(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def visit(item: dict[str, Any], path: str) -> None:
        item["path"] = path
        out.append(item)
        for index, child in enumerate(item.get("children") or []):
            visit(child, f"{path}.{index}")

    for index, root in enumerate(roots):
        visit(root, str(index))
    return out


def page_fingerprint(nodes: list[dict[str, Any]], *, platform: str = "") -> dict[str, Any]:
    """返回可持久化的结构指纹。空层级返回空指纹，调用方不得据此判同页。"""
    items, max_x, max_y = _node_items(list(nodes or []))
    if not items:
        return {}
    roots = _build_tree(items)
    walked = _walk(roots)
    signatures: list[str] = []
    shapes: list[str] = []
    anchors: list[dict[str, Any]] = []
    for item in walked:
        x1, y1, x2, y2 = item["box"]
        nx = round(x1 / max_x * 50) * 2
        ny = round(y1 / max_y * 50) * 2
        nw = max(1, round((x2 - x1) / max_x * 50) * 2)
        nh = max(1, round((y2 - y1) / max_y * 50) * 2)
        child_count = min(len(item.get("children") or []), 9)
        sig = (
            f"{item['path']}|{item['class']}|{item['role']}|"
            f"{1 if item['clickable'] else 0}|{child_count}|{nx},{ny},{nw},{nh}"
        )
        signatures.append(sig)
        shapes.append(f"{item['class']}|{item['role']}|{1 if item['clickable'] else 0}|{nw},{nh}")
        label = item["label"]
        if label and label.lower() not in _GENERIC_LABELS:
            anchors.append({
                "path": item["path"],
                "class": item["class"],
                "rid": item["rid"],
                "label": label,
                "clickable": item["clickable"],
            })
    anchors = anchors[:_MAX_ANCHORS]
    raw = "\n".join(signatures)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return {
        "version": _VERSION,
        "hash": f"fp2.{digest}",
        "platform": str(platform or ""),
        "node_count": len(walked),
        "max_x": max_x,
        "max_y": max_y,
        "signatures": signatures,
        "shapes": shapes,
        "anchors": anchors,
    }


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _anchor_score(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> float:
    def keys(rows: list[dict[str, Any]]) -> set[str]:
        out = set()
        for row in rows:
            label = str(row.get("label") or "").strip().lower()
            rid = str(row.get("rid") or "").strip().lower()
            if rid:
                out.add("rid:" + rid.rsplit("/", 1)[-1])
            if label:
                out.add("label:" + re.sub(r"\s+", "", label))
        return out

    a, b = keys(left), keys(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def compare_fingerprints(stored: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """返回 0..1 相似度和可解释分解；两边任一为空则 0。"""
    if not isinstance(stored, dict) or not isinstance(current, dict):
        return {"score": 0.0, "reason": "missing"}
    if str(stored.get("hash") or "") and stored.get("hash") == current.get("hash"):
        return {"score": 1.0, "reason": "hash_equal", "structure": 1.0, "anchors": 1.0}
    signatures_a = [str(x) for x in stored.get("signatures") or []]
    signatures_b = [str(x) for x in current.get("signatures") or []]
    shapes_a = [str(x) for x in stored.get("shapes") or []]
    shapes_b = [str(x) for x in current.get("shapes") or []]
    structure = 0.55 * _jaccard(signatures_a, signatures_b) + 0.45 * _jaccard(shapes_a, shapes_b)
    anchor = _anchor_score(list(stored.get("anchors") or []), list(current.get("anchors") or []))
    if anchor > 0:
        score = 0.65 * structure + 0.35 * anchor
    else:
        score = structure
    return {
        "score": round(min(1.0, max(0.0, score)), 4),
        "reason": "structure+anchors" if anchor > 0 else "structure",
        "structure": round(structure, 4),
        "anchors": round(anchor, 4),
    }


def matches(stored: dict[str, Any], nodes: list[dict[str, Any]], *, threshold: float = 0.62) -> dict[str, Any]:
    current = page_fingerprint(nodes)
    result = compare_fingerprints(stored, current)
    result["current_hash"] = str(current.get("hash") or "")
    result["matched"] = bool(result["score"] >= threshold)
    return result


def label_visible(nodes: list[dict[str, Any]], labels: list[str]) -> bool:
    """目标页名是否作为可见文本/描述出现。只用于辅助到达判定。"""
    wanted = [
        re.sub(r"\s+", "", str(label or "")).lower()
        for label in labels or []
        if str(label or "").strip()
    ]
    if not wanted:
        return False
    for node in nodes or []:
        if not isinstance(node, dict) or node.get("visible") is False:
            continue
        text = re.sub(r"\s+", "", f"{node.get('text') or ''} {node.get('content_desc') or ''}").lower()
        if not text:
            continue
        for label in wanted:
            if len(label) >= 2 and label in text:
                return True
    return False
