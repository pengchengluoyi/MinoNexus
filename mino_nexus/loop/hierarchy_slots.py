"""hierarchy 通道：采集结构化 UI 层级、派生文本槽、提供统一匹配原语。

为什么单独一个模块
------------------
`agent_loop` 里 `inspect_slots["hierarchy_text"]` 长期恒空（见 docs/NAVIGATION_ATLAS.md §17.3），
而 `planner` / `knowledge_hint.infer_screen_role` / `session_gate` / `step_pointer.screen_fingerprint`
都已经在读它。这里把「取一帧层级」收成一处，顺带把 guard / localize / effect_assert 三处
要用的匹配语义也收进来 —— 三份各写一遍必然漂移。

形态由协议钉死：`docs/PROTOCOL.md` §4.4.2，`RESULT.data["nodes"]` 结构化节点数组
（`extra["nodes"]` 是镜像）。**文本形态是派生物**，只喂 prompt；所有判定走结构化 `nodes`。

坐标警告：节点的 `bounds` / `center` 是**设备像素**，与 `EXECUTE.params` 的 0–1000 千分比
不是同一个体系（协议 §4.4.2）。本模块不做换算，也不把像素坐标交给决策层。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# 协议 §4.4.2 钉死的唯一 hierarchy 形态
HIERARCHY_FORMAT = "accessibility_json"

# 派生文本的规模上限。层级动辄几百个节点，全塞 prompt 既贵又淹没信号。
_MAX_TEXT_NODES = 220
_MAX_TEXT_CHARS = 6000

_TRUE = frozenset({"1", "true", "yes", "on"})


def observe_hierarchy_enabled(ctx: Any) -> bool:
    """跑用例时默认采集 hierarchy；仅 env 显式关时才停（不再经 playbook 开关）。

    `MINO_OBSERVE_HIERARCHY=0` 可在一键回滚。
    """
    env = str(os.environ.get("MINO_OBSERVE_HIERARCHY", "") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    return True


def nav_calibration_enabled(ctx: Any) -> bool:
    """【遗留】walkthrough 高密度批次模式（§8.4）。

    v2.5 起默认用被动采集（`nav_capture_store`），`observe_hierarchy` 开着即每 turn 落盘。
    本开关仅保留给需要旧版 `calibration_id` 目录的兼容场景。
    """
    if not observe_hierarchy_enabled(ctx):
        return False
    env = str(os.environ.get("MINO_NAV_CALIBRATION", "") or "").strip().lower()
    if env:
        return env in _TRUE
    playbook = getattr(ctx, "playbook", None)
    if isinstance(playbook, dict):
        return playbook.get("nav_calibration") is True
    return False


def calibration_step_keys(ctx: Any, case: dict[str, Any] | None = None) -> list[str]:
    """walkthrough 有序步标（W1…W5）。playbook 优先，其次 case.nav_calibration.steps。"""
    playbook = getattr(ctx, "playbook", None)
    if isinstance(playbook, dict):
        raw = playbook.get("nav_calibration_steps") or playbook.get("calibration_step_keys")
        if isinstance(raw, list) and raw:
            return [str(x).strip() for x in raw if str(x).strip()]
    block = (case or {}).get("nav_calibration")
    if isinstance(block, dict):
        raw = block.get("steps") or block.get("step_keys")
        if isinstance(raw, list) and raw:
            return [str(x).strip() for x in raw if str(x).strip()]
    return []


@dataclass
class HierarchySnapshot:
    """一帧 UI 层级。`stale` 时调用方必须按「无 hierarchy」处理（设计稿 §10.0.2）。"""

    ok: bool = False
    nodes: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    turn_id: int = 0
    stale: bool = False
    error: str = ""
    elapsed_ms: int = 0
    source: str = ""
    format: str = HIERARCHY_FORMAT

    def usable(self) -> bool:
        """能不能拿来判 guard / effect_assert。空层级不算可用 —— 无输入不误判。"""
        return bool(self.ok and self.nodes and not self.stale)

    def brief(self) -> dict[str, Any]:
        """写 session_log 用的摘要，不带正文。"""
        return {
            "ok": self.ok,
            "node_count": len(self.nodes),
            "stale": self.stale,
            "turn_id": self.turn_id,
            "elapsed_ms": self.elapsed_ms,
            "source": self.source,
            "format": self.format,
            "error": self.error[:200],
            "text_len": len(self.text),
        }


def capture(proxy: Any, *, turn_id: int, screenshot_turn_id: int | None = None) -> HierarchySnapshot:
    """取一帧层级。**失败不抛** —— 拿不到层级只降级，不该让整个 turn 挂掉。

    `screenshot_turn_id` 给出时用于对齐：两个信号不同轮 → `stale`，本 turn 不评 effect_assert。
    """
    try:
        shot = proxy.observe("hierarchy", force_fresh=True)
    except Exception as exc:  # noqa: BLE001 — 观察失败不能拖垮跑批
        return HierarchySnapshot(turn_id=turn_id, error=f"{type(exc).__name__}: {exc}")

    if not getattr(shot, "ok", False):
        return HierarchySnapshot(
            turn_id=turn_id,
            error=str(getattr(shot, "error", "") or "hierarchy 未返回"),
            elapsed_ms=int(getattr(shot, "elapsed_ms", 0) or 0),
        )

    detail = dict(getattr(shot, "remote_detail", None) or {})
    nodes = normalize_nodes(detail.get("nodes"))
    stale = screenshot_turn_id is not None and int(screenshot_turn_id) != int(turn_id)
    return HierarchySnapshot(
        ok=True,
        nodes=nodes,
        text=flatten(nodes),
        turn_id=turn_id,
        stale=stale,
        elapsed_ms=int(getattr(shot, "elapsed_ms", 0) or detail.get("elapsed_ms") or 0),
        source=str(detail.get("source") or getattr(shot, "source", "") or ""),
    )


def normalize_nodes(raw: Any) -> list[dict[str, Any]]:
    """协议节点 → 内部形状。缺字段按空值补齐，未知字段丢弃（协议 §7 允许对方加字段）。"""
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "resource_id": str(item.get("resource_id") or ""),
                "text": str(item.get("text") or ""),
                "content_desc": str(item.get("content_desc") or ""),
                "class": str(item.get("class") or item.get("cls") or ""),
                "clickable": bool(item.get("clickable")),
                "bounds": [int(x) for x in (item.get("bounds") or [0, 0, 0, 0])[:4]],
                "center": [int(x) for x in (item.get("center") or [0, 0])[:2]],
            }
        )
    return out


def rid_short(resource_id: str) -> str:
    """去掉包名前缀。代码里不该出现具体包名，比较时统一用后半段。"""
    rid = str(resource_id or "")
    return rid.split("/", 1)[-1] if "/" in rid else rid


def _cls_short(cls: str) -> str:
    return str(cls or "").rsplit(".", 1)[-1]


def flatten(nodes: list[dict[str, Any]]) -> str:
    """节点 → 供 prompt 的紧凑文本。只留有语义的节点，纯布局容器不进。"""
    lines: list[str] = []
    for node in nodes or []:
        rid = rid_short(node.get("resource_id", ""))
        text = str(node.get("text") or "").strip()
        desc = str(node.get("content_desc") or "").strip()
        if not (rid or text or desc):
            continue
        bits = []
        if rid:
            bits.append(f"id={rid}")
        if text:
            bits.append(f"text={text}")
        if desc and desc != text:
            bits.append(f"desc={desc}")
        cls = _cls_short(node.get("class", ""))
        if cls:
            bits.append(f"cls={cls}")
        if node.get("clickable"):
            bits.append("clickable")
        lines.append(" ".join(bits))
        if len(lines) >= _MAX_TEXT_NODES:
            lines.append(f"...(+{max(0, len(nodes) - len(lines))} 个节点未列出)")
            break
    out = "\n".join(lines)
    return out if len(out) <= _MAX_TEXT_CHARS else out[:_MAX_TEXT_CHARS] + "\n...(截断)"


# ---------------- 匹配原语（guard / localize / effect_assert 共用） ----------------


def _node_blob(node: dict[str, Any]) -> str:
    return "\n".join(
        str(node.get(k) or "") for k in ("resource_id", "text", "content_desc")
    )


def node_matches(node: dict[str, Any], cond: Any) -> bool:
    """单节点是否命中一条 detect 条件。

    条件形状见设计稿 §8.3。裸字符串等价于 `{"hierarchy_contains": <str>}`，
    这样 `match_none: ["未关注"]` 这种简写也能直接用。
    """
    if isinstance(cond, str):
        cond = {"hierarchy_contains": cond}
    if not isinstance(cond, dict) or not cond:
        return False

    for key, want in cond.items():
        if want in (None, "", [], {}):
            continue
        if key == "hierarchy_contains":
            if str(want) not in _node_blob(node):
                return False
        elif key == "resource_id_regex":
            if not _search(str(want), str(node.get("resource_id") or "")):
                return False
        elif key == "text_eq":
            if str(node.get("text") or "").strip() != str(want).strip():
                return False
        elif key == "text_contains":
            if str(want) not in str(node.get("text") or ""):
                return False
        elif key == "content_desc_contains":
            if str(want) not in str(node.get("content_desc") or ""):
                return False
        elif key == "class_regex":
            if not _search(str(want), str(node.get("class") or "")):
                return False
        elif key == "clickable":
            if bool(node.get("clickable")) is not bool(want):
                return False
        else:
            # 未知条件键：宁可不命中，也不要静默当成命中（漏拦好过误拦）
            return False
    return True


def _search(pattern: str, target: str) -> bool:
    try:
        return bool(re.search(pattern, target))
    except re.error:
        return False


def match_any(nodes: list[dict[str, Any]], conds: Any) -> Optional[dict[str, Any]]:
    """命中任一条件的第一个节点；都不命中返回 None。"""
    for cond in conds or []:
        for node in nodes or []:
            if node_matches(node, cond):
                return node
    return None


def match_none(nodes: list[dict[str, Any]], conds: Any) -> bool:
    """所有反证条件都不命中才为真。`match_none` 为空视为通过。"""
    return match_any(nodes, conds) is None


def screen_contains(nodes: list[dict[str, Any]], term: str) -> bool:
    """整屏是否出现某段文本。用于 landmark 类判定。"""
    needle = str(term or "").strip()
    if not needle:
        return False
    return any(needle in _node_blob(n) for n in nodes or [])
