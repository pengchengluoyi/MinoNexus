"""Nav Tab 偏好：从 nav_fsm 配置读首页/完整底栏（数据在库，代码不写 App 文案白名单）。"""
from __future__ import annotations

from typing import Any


def tab_bar_prefs_from_doc(doc: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(doc, dict):
        return {}
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    tb = meta.get("tab_bar") if isinstance(meta.get("tab_bar"), dict) else {}
    prefs = meta.get("tab_bar_prefs") if isinstance(meta.get("tab_bar_prefs"), dict) else {}
    home = str(tb.get("home_tab_label") or prefs.get("home_tab_label") or "").strip()
    labels = prefs.get("labels") or prefs.get("slot_labels") or tb.get("slot_labels") or []
    if not isinstance(labels, list):
        labels = []
    labels = [str(x or "").strip() for x in labels if str(x or "").strip()]
    anchor = str((meta.get("hierarchy_calibration") or {}).get("anchor_on_first_screen") or "").strip()
    return {
        "home_tab_label": home,
        "labels": labels[:12],
        "anchor_on_first_screen": anchor,
    }


def load_tab_bar_prefs(app_id: str) -> dict[str, Any]:
    aid = str(app_id or "").strip()
    if not aid:
        return {}
    try:
        from mino_nexus.services import nav_fsm_store as store

        doc = store.read_raw(aid)
    except Exception:  # noqa: BLE001
        doc = None
    return tab_bar_prefs_from_doc(doc if isinstance(doc, dict) else None)


def resolve_home_tab_label(app_id: str, tab_labels: list[str]) -> str:
    """冷启动默认 Tab：优先库里的 home_tab_label，其次 anchor 若恰为某一 Tab 文案。"""
    labels = [str(t or "").strip() for t in tab_labels if str(t or "").strip()]
    if not labels:
        return ""
    prefs = load_tab_bar_prefs(app_id)
    home = str(prefs.get("home_tab_label") or "").strip()
    if home and home in labels:
        return home
    anchor = str(prefs.get("anchor_on_first_screen") or "").strip()
    if anchor and anchor in labels:
        return anchor
    return labels[0]


def merge_configured_tab_labels(extracted: list[str], configured: list[str]) -> list[str]:
    """采集只露出部分 Tab 文案时，用库里完整底栏顺序补全（如中间图标 Tab）。"""
    ex = [str(t or "").strip() for t in extracted if str(t or "").strip()]
    cfg = [str(t or "").strip() for t in configured if str(t or "").strip()]
    if len(cfg) < 2:
        return ex
    if len(ex) < 2:
        return cfg[:12]
    ex_set = set(ex)
    cfg_set = set(cfg)
    if ex_set <= cfg_set:
        return cfg[:12]
    if len(ex_set & cfg_set) >= 2:
        return cfg[:12]
    return ex[:12]
