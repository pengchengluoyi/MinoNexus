"""v2.5 被动 hierarchy 采集：跑用例时每 turn 落盘，不依赖 walkthrough 批次。

设计稿 docs/NAVIGATION_ATLAS.md §19.2–§19.4。
"""
from __future__ import annotations

import base64
import json
import shutil
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

from mino_nexus.core.paths import nav_capture_dir
from mino_nexus.loop.hierarchy_slots import HIERARCHY_FORMAT, flatten, rid_short

INDEX_NAME = "index.json"
MAX_TEXT_CHARS = 8000

_LAUNCHER_HINTS = frozenset(
    {
        "shortcut_menu_layer",
        "drag_layer",
        "workspace",
        "hotseat",
        "launcher",
        "status_bar_launch_animation_container",
    }
)


def infer_screen_package(
    nodes: list[dict[str, Any]] | None,
    *,
    target_package: str = "",
    platform: str = "",
    target_scope: dict[str, Any] | None = None,
) -> dict[str, str]:
    """从 hierarchy 推断前台（包名 / Web origin）与 screen_kind。"""
    from mino_nexus.services.nav_target_scope import TargetScope, infer_foreground

    scope = TargetScope.from_dict(target_scope) if target_scope else None
    if scope is None and target_package:
        from mino_nexus.services.nav_target_scope import scope_from_values

        scope = scope_from_values(platform, target_package)
    return infer_foreground(nodes, scope=scope, target_package=target_package, platform=platform)


_OVERLAY_PKG_HINTS = (
    "permissioncontroller",
    "lbe.security",
    "securitycenter",
    "packageinstaller",
)
_OVERLAY_RID_HINTS = frozenset(
    {
        "dialog_root_view",
        "parentpanel",
        "buttonpanel",
        "alerttitle",
    }
)


def is_overlay_screen(turn: dict[str, Any]) -> bool:
    """系统权限弹窗 / 模态层 —— 不进 App 图谱与 Tab 识别。"""
    if not turn:
        return False
    nodes = turn.get("nodes") or []
    rid_tokens: set[str] = set()
    for node in nodes:
        rid = str(node.get("resource_id") or "").lower()
        if not rid:
            continue
        if any(hint in rid for hint in _OVERLAY_PKG_HINTS):
            return True
        tail = rid.split("/")[-1]
        rid_tokens.add(tail)
    if _OVERLAY_RID_HINTS & rid_tokens:
        return True
    from mino_nexus.services.nav_screen_layout import is_modal_button_stack

    return is_modal_button_stack(nodes)


def is_system_screen(
    turn: dict[str, Any],
    *,
    scope: Any = None,
) -> bool:
    """系统桌面 / 非本 app 目标前台 / 权限弹窗 —— 不进图谱。"""
    if is_overlay_screen(turn):
        return True
    from mino_nexus.services.nav_target_scope import TargetScope, turn_matches_scope

    scoped = scope if isinstance(scope, TargetScope) else TargetScope.from_dict(scope)
    if scoped and not turn_matches_scope(turn, scoped):
        return True
    kind = str(turn.get("screen_kind") or "").strip()
    if kind in ("launcher", "foreign"):
        return True
    if kind == "app":
        return False
    pkg = infer_screen_package(
        turn.get("nodes") or [],
        target_package=str(turn.get("target_package") or ""),
        platform=str(turn.get("platform") or ""),
        target_scope=turn.get("target_scope") if isinstance(turn.get("target_scope"), dict) else None,
    )
    return pkg.get("screen_kind") in ("launcher", "foreign")


def synthesis_turns(
    turns: list[dict[str, Any]],
    *,
    scope: Any = None,
) -> list[dict[str, Any]]:
    """NavFSM 合成专用：仅保留本 app target_scope 内、非系统弹窗的采集帧。"""
    return [t for t in (turns or []) if not is_system_screen(t, scope=scope)]


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _save_screen_jpeg(path: Path, image_b64: str) -> bool:
    if not image_b64:
        return False
    try:
        from PIL import Image

        raw = str(image_b64).strip()
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        data = base64.b64decode(raw)
        img = Image.open(BytesIO(data)).convert("RGB")
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, format="JPEG", quality=88)
        return True
    except Exception:
        return False


def screen_path(app_id: str, session_id: str, turn_id: int) -> Path:
    return _session_root(app_id, session_id) / f"turn_{int(turn_id):04d}.screen.jpg"


def _session_root(app_id: str, session_id: str) -> Path:
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id 必填")
    return nav_capture_dir(app_id, sid)


def append_turn(
    app_id: str,
    session_id: str,
    *,
    turn_id: int,
    project_id: str = "",
    account_id: str = "",
    run_id: str = "",
    case_id: str = "",
    run_type: str = "",
    nodes: list[dict[str, Any]] | None = None,
    hierarchy_stale: bool = False,
    localized: dict[str, Any] | None = None,
    layout_hierarchy: dict[str, Any] | None = None,
    layout_vision: dict[str, Any] | None = None,
    target_package: str = "",
    platform: str = "",
    target_scope: dict[str, Any] | None = None,
    cap_id: str = "",
    error: str = "",
    screenshot_b64: str = "",
) -> dict[str, Any] | None:
    """记一帧。空层级不记 —— 与旧 walkthrough 记步规则一致。"""
    if not nodes:
        return None
    root = _session_root(app_id, session_id)
    tid = int(turn_id)
    stem = f"turn_{tid:04d}"
    text = flatten(nodes)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n…"

    from mino_nexus.services.nav_target_scope import TargetScope, scope_from_values

    store_nodes = list(nodes or [])
    scope = TargetScope.from_dict(target_scope) if target_scope else scope_from_values(platform, target_package)
    screen_pkg = infer_screen_package(
        store_nodes,
        target_package=target_package,
        platform=platform,
        target_scope=scope.as_dict() if scope else None,
    )
    meta = {
        "app_id": str(app_id or ""),
        "session_id": str(session_id or ""),
        "project_id": str(project_id or ""),
        "account_id": str(account_id or ""),
        "run_id": str(run_id or ""),
        "case_id": str(case_id or ""),
        "run_type": str(run_type or ""),
        "turn_id": tid,
        "at": int(time.time()),
        "hierarchy_format": HIERARCHY_FORMAT,
        "node_count": len(nodes or []),
        "nodes_stored": len(store_nodes),
        "hierarchy_stale": bool(hierarchy_stale),
        "hierarchy_text": text,
        "localized": dict(localized or {}),
        "layout_hierarchy": dict(layout_hierarchy or {}),
        "layout_vision": dict(layout_vision or {}),
        "platform": str(screen_pkg.get("platform") or scope.platform if scope else platform or "android"),
        "target_package": screen_pkg["target_package"],
        "foreground_package": screen_pkg["foreground_package"],
        "foreground_id": str(screen_pkg.get("foreground_id") or screen_pkg.get("foreground_package") or ""),
        "screen_kind": screen_pkg["screen_kind"],
        "target_scope": scope.as_dict() if scope else {},
        "cap_id": str(cap_id or ""),
        "error": str(error or "")[:200],
        "nodes_rel": f"{stem}.nodes.json",
        "screenshot_rel": "",
    }
    screen_rel = f"{stem}.screen.jpg"
    if _save_screen_jpeg(root / screen_rel, screenshot_b64):
        meta["screenshot_rel"] = screen_rel
    _write_json(root / f"{stem}.meta.json", meta)
    _write_json(root / f"{stem}.nodes.json", store_nodes)

    index = _read_json(root / INDEX_NAME, {"session_id": session_id, "turns": []})
    turns = [t for t in (index.get("turns") or []) if int(t.get("turn_id") or 0) != tid]
    turns.append(
        {
            "turn_id": tid,
            "at": meta["at"],
            "node_count": meta["node_count"],
            "case_id": meta["case_id"],
            "account_id": meta["account_id"],
            "state_id": str((localized or {}).get("chosen") or ""),
            "confidence": float((localized or {}).get("confidence") or 0.0),
        }
    )
    turns.sort(key=lambda x: int(x.get("turn_id") or 0))
    index.update(
        {
            "app_id": str(app_id or ""),
            "session_id": str(session_id or ""),
            "project_id": str(project_id or ""),
            "updated_at": int(time.time()),
            "turn_count": len(turns),
            "turns": turns,
        }
    )
    _write_json(root / INDEX_NAME, index)
    return meta


def list_sessions(app_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    root = nav_capture_dir(app_id)
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir():
            continue
        index = _read_json(child / INDEX_NAME, {})
        if not index.get("turns"):
            continue
        rows.append(
            {
                "session_id": child.name,
                "turn_count": int(index.get("turn_count") or len(index.get("turns") or [])),
                "updated_at": int(index.get("updated_at") or 0),
                "project_id": str(index.get("project_id") or ""),
                "last_case_id": str((index.get("turns") or [{}])[-1].get("case_id") or ""),
            }
        )
        if len(rows) >= max(1, limit):
            break
    return rows


def read_session_index(app_id: str, session_id: str) -> dict[str, Any] | None:
    root = _session_root(app_id, session_id)
    if not root.exists():
        return None
    index = _read_json(root / INDEX_NAME, {})
    return index if index.get("turns") else None


def read_turn(app_id: str, session_id: str, turn_id: int) -> dict[str, Any] | None:
    root = _session_root(app_id, session_id)
    stem = f"turn_{int(turn_id):04d}"
    meta = _read_json(root / f"{stem}.meta.json", None)
    if not meta:
        return None
    nodes = _read_json(root / f"{stem}.nodes.json", [])
    row = {**meta, "nodes": nodes}
    if not meta.get("foreground_package"):
        pkg = infer_screen_package(
            nodes,
            target_package=str(meta.get("target_package") or ""),
            platform=str(meta.get("platform") or ""),
            target_scope=meta.get("target_scope") if isinstance(meta.get("target_scope"), dict) else None,
        )
        row.update(pkg)
    from mino_nexus.services.nav_screen_layout import merge_layout_views, wireframe_from_hierarchy

    h_layout = wireframe_from_hierarchy(nodes) if nodes else {}
    v_layout = meta.get("layout_vision") if isinstance(meta.get("layout_vision"), dict) else {}
    wf = merge_layout_views(h_layout, v_layout)
    wf["capture"] = {
        "app_id": str(meta.get("app_id") or app_id),
        "session_id": str(session_id or ""),
        "turn_id": int(turn_id),
        "has_screenshot": bool(meta.get("screenshot_rel")),
    }
    row["layout_wireframe"] = wf
    if meta.get("screenshot_rel"):
        row["has_screenshot"] = True
        row["screenshot_rel"] = str(meta.get("screenshot_rel") or "")
    return row


def patch_turn_layout(
    app_id: str,
    session_id: str,
    turn_id: int,
    *,
    layout_vision: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """决策/断言后补上 VLM 布局（与 hierarchy 并行，同 turn 合并）。"""
    if not layout_vision:
        return None
    root = _session_root(app_id, session_id)
    stem = f"turn_{int(turn_id):04d}"
    meta_path = root / f"{stem}.meta.json"
    meta = _read_json(meta_path, None)
    if not meta:
        return None
    meta["layout_vision"] = dict(layout_vision)
    _write_json(meta_path, meta)
    return meta


def iter_cumulative_turns(
    app_id: str,
    *,
    limit_sessions: int = 40,
    limit_turns: int = 800,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按时间合并多次跑用例采集，供 NavFSM 累积合成。"""
    sessions = list_sessions(app_id, limit=max(1, limit_sessions))
    rows: list[tuple[int, int, str, dict[str, Any]]] = []
    for sess in sessions:
        sid = str(sess.get("session_id") or "")
        index = read_session_index(app_id, sid) or {}
        for turn_row in index.get("turns") or []:
            turn = read_turn(app_id, sid, int(turn_row.get("turn_id") or 0))
            if not turn or not turn.get("nodes"):
                continue
            localized = dict(turn.get("localized") or {})
            chosen = str(turn_row.get("state_id") or localized.get("chosen") or "").strip()
            if chosen:
                localized["chosen"] = chosen
                localized.setdefault("confidence", float(turn_row.get("confidence") or 0))
            if localized:
                turn = {**turn, "localized": localized}
            turn = {**turn, "session_id": sid}
            at = int(turn.get("at") or turn_row.get("at") or 0)
            tid = int(turn_row.get("turn_id") or 0)
            rows.append((at, tid, sid, turn))
    rows.sort(key=lambda x: (x[0], x[1]))
    if limit_turns > 0:
        rows = rows[-limit_turns:]
    ordered = [t for _, _, _, t in rows]
    meta = {
        "sessions": len(sessions),
        "sessions_used": len({sid for _, _, sid, _ in rows}),
        "turns": len(ordered),
        "latest_session_id": str(sessions[0]["session_id"]) if sessions else "",
    }
    return ordered, meta


def iter_samples(app_id: str, *, limit_sessions: int = 30, limit_turns: int = 500) -> Iterator[dict[str, Any]]:
    """供候选编译器遍历样本。"""
    count = 0
    for sess in list_sessions(app_id, limit=limit_sessions):
        sid = str(sess.get("session_id") or "")
        index = read_session_index(app_id, sid) or {}
        for row in index.get("turns") or []:
            if count >= limit_turns:
                return
            turn = read_turn(app_id, sid, int(row.get("turn_id") or 0))
            if turn and turn.get("nodes"):
                yield turn
                count += 1


def clear_all(app_id: str) -> dict[str, Any]:
    """删除该 App 下全部被动采集 session（不可恢复）。"""
    root = nav_capture_dir(app_id)
    sessions = 0
    turns = 0
    if root.exists():
        for child in list(root.iterdir()):
            if not child.is_dir():
                continue
            index = _read_json(child / INDEX_NAME, {})
            turns += len(index.get("turns") or [])
            sessions += 1
            shutil.rmtree(child, ignore_errors=True)
    return {
        "app_id": str(app_id or ""),
        "deleted_sessions": sessions,
        "deleted_turns": turns,
    }


def capture_report(app_id: str) -> dict[str, Any]:
    sessions = list_sessions(app_id, limit=200)
    turns = sum(int(s.get("turn_count") or 0) for s in sessions)
    accounts: set[str] = set()
    state_hits: dict[str, int] = {}
    landmark_counts: dict[str, int] = {}

    for sample in iter_samples(app_id, limit_sessions=50, limit_turns=300):
        aid = str(sample.get("account_id") or "").strip()
        if aid:
            accounts.add(aid)
        st = str((sample.get("localized") or {}).get("chosen") or "").strip()
        if st:
            state_hits[st] = state_hits.get(st, 0) + 1
        for node in sample.get("nodes") or []:
            text = str(node.get("text") or "").strip()
            if 1 < len(text) <= 40:
                landmark_counts[text] = landmark_counts.get(text, 0) + 1

    top_landmarks = [
        {"text": k, "count": v}
        for k, v in sorted(landmark_counts.items(), key=lambda x: (-x[1], x[0]))[:20]
    ]
    return {
        "app_id": str(app_id or ""),
        "sessions": len(sessions),
        "turns_captured": turns,
        "accounts_seen": sorted(accounts),
        "state_hits": state_hits,
        "top_landmarks": top_landmarks,
    }


def extract_landmark_stats(samples: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for sample in samples:
        for node in sample.get("nodes") or []:
            text = str(node.get("text") or "").strip()
            if 1 < len(text) <= 48:
                counts[text] = counts.get(text, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))


def extract_resource_stats(samples: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for sample in samples:
        for node in sample.get("nodes") or []:
            rid = rid_short(str(node.get("resource_id") or ""))
            if rid and not rid.startswith("__"):
                counts[rid] = counts.get(rid, 0) + 1
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))
