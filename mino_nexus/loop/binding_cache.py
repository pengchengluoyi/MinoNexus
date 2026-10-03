"""跑次/设备侧节点指纹缓存。记住 resource-id / 整词文案，不记坐标。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from mino_nexus.ai.schemas import AgentAction, AgentDecision
from mino_nexus.core.paths import data_dir
from mino_nexus.loop.hierarchy_slots import int_list, rid_short
from mino_nexus.loop.llm_step_context import step_scope_key
from mino_nexus.loop.milestones import read_state

SCHEMA = "mino.binding_cache.v1"
_CACHEABLE_CAPS = frozenset({
    "tap_element",
    "long_press_element",
    "multi_tap",
    "input_text",
})
_LABEL_MAX = 48
_COORD_KEYS = ("x", "y", "from_x", "from_y", "to_x", "to_y", "px", "py", "bounds")


def _nodes(ctx: Any) -> list[dict[str, Any]]:
    return [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(n, dict)]


def _focus(cursor: Any) -> dict[str, Any]:
    from mino_nexus.loop.milestone_orchestrator import in_progress_milestone

    row = in_progress_milestone(read_state(cursor))
    return dict(row) if isinstance(row, dict) else {}


def _observe(row: dict[str, Any]) -> str:
    return str(row.get("observe") or "exec").strip().lower()


def _in_interrupt(cursor: Any) -> bool:
    from mino_nexus.loop.fuse.interrupt_stack import stack_depth

    return stack_depth(cursor) > 0


def intent_key(cursor: Any, focus: dict[str, Any] | None = None) -> str:
    row = focus if isinstance(focus, dict) else _focus(cursor)
    case_step, phase = step_scope_key(cursor)
    return "|".join(
        [
            str(phase or ""),
            str(case_step or 0),
            str(row.get("key_ref") or ""),
            str(row.get("id") or ""),
            str(row.get("hook_cap") or row.get("device_cap") or ""),
        ]
    )


def program_graph_version(cursor: Any, ctx: Any = None) -> str:
    case = None
    if ctx is not None:
        case = getattr(ctx, "case", None)
    if not isinstance(case, dict):
        case = getattr(cursor, "case", None)
    meta = case.get("meta") if isinstance(case, dict) else {}
    spk = meta.get("step_program_keys") if isinstance(meta, dict) else None
    blob: Any = spk
    if not blob:
        blob = getattr(cursor, "do_program_plan", None) or getattr(cursor, "check_program_plan", None) or {}
    raw = json.dumps(blob, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def fingerprint_from_node(node: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(node, dict):
        return {}
    rid = rid_short(node.get("resource_id") or "")
    text = str(node.get("text") or "").strip()
    desc = str(node.get("content_desc") or "").strip()
    label = ""
    if 0 < len(text) <= _LABEL_MAX:
        label = text
    elif 0 < len(desc) <= _LABEL_MAX:
        label = desc
    role = str(node.get("role") or "").strip()
    fp: dict[str, str] = {}
    if rid:
        fp["resource_id"] = rid
    if label:
        fp["text"] = label
    if role:
        fp["role"] = role
    if not fp.get("resource_id") and not fp.get("text"):
        return {}
    for key in _COORD_KEYS:
        fp.pop(key, None)
    return fp


def _screen_wh(nodes: list[dict[str, Any]]) -> tuple[int, int]:
    sw = 0
    sh = 0
    for node in nodes:
        b = int_list(node.get("bounds"), 4)
        if len(b) == 4:
            sw = max(sw, b[2])
            sh = max(sh, b[3])
    return sw, sh


def _spatial_of(node: dict[str, Any], nodes: list[dict[str, Any]]) -> str:
    sw, sh = _screen_wh(nodes)
    b = int_list(node.get("bounds"), 4)
    if len(b) != 4 or sw <= 0 or sh <= 0:
        return ""
    cx = (b[0] + b[2]) / 2.0
    cy = (b[1] + b[3]) / 2.0
    left = cx < sw * 0.38
    right = cx > sw * 0.62
    top = cy < sh * 0.33
    bottom = cy > sh * 0.67
    if top and right:
        return "top_right"
    if top and left:
        return "top_left"
    if bottom and right:
        return "bottom_right"
    if bottom and left:
        return "bottom_left"
    if top:
        return "top"
    if bottom:
        return "bottom"
    if right:
        return "right"
    if left:
        return "left"
    return "center"


def match_fingerprint(
    nodes: list[dict[str, Any]],
    fp: dict[str, Any] | None,
    *,
    spatial_hint: str = "",
) -> Optional[dict[str, Any]]:
    if not isinstance(fp, dict):
        return None
    rid = rid_short(fp.get("resource_id") or "")
    text = str(fp.get("text") or "").strip()
    role = str(fp.get("role") or "").strip()
    if not rid and not text:
        return None
    hits: list[dict[str, Any]] = []
    for node in nodes:
        if rid and rid_short(node.get("resource_id") or "") != rid:
            continue
        if text:
            nt = str(node.get("text") or "").strip()
            nd = str(node.get("content_desc") or "").strip()
            if nt != text and nd != text:
                continue
        if role and not rid:
            nr = str(node.get("role") or node.get("class") or "")
            if role not in nr and rid_short(nr) != rid_short(role):
                continue
        hits.append(node)
    hint = str(spatial_hint or "").strip().lower()
    if hint and len(hits) > 1:
        ranked = [n for n in hits if _spatial_of(n, nodes) == hint]
        if len(ranked) == 1:
            return ranked[0]
        if ranked:
            hits = ranked
    if len(hits) == 1:
        return hits[0]
    return None


def _safe_seg(raw: str) -> str:
    seg = str(raw or "").strip().replace("\\", "/").split("/")[-1]
    seg = "".join(ch for ch in seg if ch.isalnum() or ch in "-_.")
    return "_" if seg in ("", ".", "..") else seg


def _device_path(ctx: Any, key: str) -> Path:
    sn = _safe_seg(str(getattr(ctx, "sn", "") or "unknown"))
    pkg = _safe_seg(str(getattr(ctx, "target_package", "") or getattr(ctx, "package_id", "") or "app"))
    digest = hashlib.sha1(str(key or "").encode("utf-8", errors="replace")).hexdigest()[:24]
    return data_dir() / "binding_cache" / sn / pkg / f"{digest}.json"


def _run_store(cursor: Any) -> dict[str, Any]:
    raw = getattr(cursor, "binding_cache_v1", None)
    if isinstance(raw, dict):
        return raw
    bag: dict[str, Any] = {}
    cursor.binding_cache_v1 = bag
    return bag


def _record(fp: dict[str, str], version: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "graph_version": version,
        "fingerprint": dict(fp),
    }


def _valid_record(row: Any, version: str) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    if str(row.get("graph_version") or "") != str(version or ""):
        return None
    fp = row.get("fingerprint")
    if not isinstance(fp, dict):
        return None
    clean = {str(k): str(v) for k, v in fp.items() if str(k) not in _COORD_KEYS and str(v).strip()}
    if not clean.get("resource_id") and not clean.get("text"):
        return None
    return clean


def lookup(cursor: Any, ctx: Any, key: str) -> dict[str, str] | None:
    version = program_graph_version(cursor, ctx)
    hit = _valid_record(_run_store(cursor).get(key), version)
    if hit:
        return hit
    path = _device_path(ctx, key)
    try:
        if not path.is_file():
            return None
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _valid_record(row, version)


def put(cursor: Any, ctx: Any, key: str, fp: dict[str, str], *, writer: Any = None) -> None:
    version = program_graph_version(cursor, ctx)
    rec = _record(fp, version)
    _run_store(cursor)[key] = rec
    path = _device_path(ctx, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=0), encoding="utf-8")
    except OSError:
        pass
    if writer is not None:
        writer.append(
            "binding_cache/write",
            {
                "intent": key[:160],
                "keys": sorted(fp.keys()),
                "graph_version": version,
            },
        )


def _params_from_node(
    focus: dict[str, Any],
    node: dict[str, Any],
    fp: dict[str, str],
) -> dict[str, Any]:
    params = dict(focus.get("params") or {}) if isinstance(focus.get("params"), dict) else {}
    target: dict[str, str] = {}
    if fp.get("resource_id"):
        target["resource_id"] = fp["resource_id"]
    if fp.get("text"):
        target["text"] = fp["text"]
        target["content_desc"] = fp["text"]
    if target:
        params["target"] = target
        params.setdefault("selector_text", fp.get("text") or fp.get("resource_id") or "")
    b = int_list(node.get("bounds"), 4)
    if len(b) == 4:
        params["x"] = (b[0] + b[2]) / 2.0
        params["y"] = (b[1] + b[3]) / 2.0
    return params


def try_replay_decision(
    cursor: Any,
    ctx: Any,
    *,
    writer: Any = None,
    focus: dict[str, Any] | None = None,
) -> AgentDecision | None:
    row = focus if isinstance(focus, dict) else _focus(cursor)
    if not row:
        return None
    if _in_interrupt(cursor):
        return None
    obs = _observe(row)
    cap = str(row.get("hook_cap") or row.get("device_cap") or "").strip()
    key = intent_key(cursor, row)
    if obs == "visual_each_run":
        return None
    if obs == "program" or cap not in _CACHEABLE_CAPS:
        return None
    fp = lookup(cursor, ctx, key)
    if not fp:
        if writer is not None:
            writer.append("binding_cache/miss", {"intent": key[:160], "reason": "empty"})
        return None
    hint = ""
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    if isinstance(params, dict):
        hint = str(params.get("spatial_hint") or "")
    node = match_fingerprint(_nodes(ctx), fp, spatial_hint=hint)
    if node is None:
        if writer is not None:
            writer.append("binding_cache/miss", {"intent": key[:160], "reason": "not_unique"})
        return None
    setattr(cursor, "binding_last_node", dict(node))
    out_params = _params_from_node(row, node, fp)
    if writer is not None:
        writer.append(
            "binding_cache/hit",
            {"intent": key[:160], "capability_id": cap, "keys": sorted(fp.keys())},
        )
        writer.append(
            "exec/replay",
            {
                "phase": step_scope_key(cursor)[1],
                "case_step": step_scope_key(cursor)[0],
                "capability_id": cap,
                "intent": key[:160],
                "exec_mode": "replay",
            },
        )
    return AgentDecision(
        status="continue",
        thought="binding cache unique hit",
        action=AgentAction(capability_id=cap, params=out_params),
        confidence=0.9,
    )


def _node_at_point(nodes: list[dict[str, Any]], x: float, y: float) -> Optional[dict[str, Any]]:
    hits: list[tuple[int, dict[str, Any]]] = []
    for node in nodes:
        b = int_list(node.get("bounds"), 4)
        if len(b) != 4:
            continue
        if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
            area = max(1, (b[2] - b[0]) * (b[3] - b[1]))
            hits.append((area, node))
    if not hits:
        return None
    hits.sort(key=lambda row: row[0])
    return hits[0][1]


def resolve_node_from_params(ctx: Any, params: dict[str, Any] | None) -> Optional[dict[str, Any]]:
    nodes = _nodes(ctx)
    p = params if isinstance(params, dict) else {}
    target = p.get("target") if isinstance(p.get("target"), dict) else {}
    probe = {
        "resource_id": str(target.get("resource_id") or p.get("resource_id") or ""),
        "text": str(target.get("text") or p.get("selector_text") or p.get("text") or ""),
    }
    hit = match_fingerprint(nodes, probe)
    if hit is not None:
        return hit
    try:
        x = float(p.get("x"))
        y = float(p.get("y"))
    except (TypeError, ValueError):
        return None
    w = int(getattr(ctx, "last_shot_width", 0) or 0)
    h = int(getattr(ctx, "last_shot_height", 0) or 0)
    if w > 0 and h > 0 and x <= 1000 and y <= 1000:
        from mino_nexus.ai.coords import milli_to_viewport_px

        pair = milli_to_viewport_px(x, y, w, h)
        if pair:
            x, y = float(pair[0]), float(pair[1])
    return _node_at_point(nodes, x, y)


def remember_success(
    cursor: Any,
    ctx: Any,
    *,
    params: dict[str, Any] | None = None,
    capability_id: str = "",
    writer: Any = None,
) -> None:
    row = _focus(cursor)
    if not row:
        return
    cap = str(row.get("hook_cap") or row.get("device_cap") or "").strip()
    dispatched = str(capability_id or "").strip()
    if dispatched and cap and dispatched != cap:
        return
    key = intent_key(cursor, row)
    if _in_interrupt(cursor):
        if writer is not None:
            writer.append("binding_cache/skip_write", {"intent": key[:160], "reason": "interrupt"})
        return
    if _observe(row) == "visual_each_run":
        if writer is not None:
            writer.append("binding_cache/skip_write", {"intent": key[:160], "reason": "visual_each_run"})
        return
    if cap not in _CACHEABLE_CAPS:
        return
    node = getattr(cursor, "binding_last_node", None)
    if not isinstance(node, dict):
        node = resolve_node_from_params(ctx, params)
    fp = fingerprint_from_node(node if isinstance(node, dict) else None)
    setattr(cursor, "binding_last_node", None)
    if not fp:
        return
    put(cursor, ctx, key, fp, writer=writer)


__all__ = [
    "try_replay_decision",
    "remember_success",
    "fingerprint_from_node",
    "match_fingerprint",
    "intent_key",
]
