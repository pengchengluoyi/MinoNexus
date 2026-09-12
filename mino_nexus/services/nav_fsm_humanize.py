"""把 NavFSM 技术错误翻译成运营能看懂的中文。"""
from __future__ import annotations

import re

from mino_nexus.services.nav_fsm_store import CALIBRATE_MARK

_PATH_LABELS: dict[str, str] = {
    "meta.hierarchy_calibration.calibration_id": "采集证据编号",
    "meta.hierarchy_calibration.evidence_rel_path": "采集文件位置",
    "meta.hierarchy_calibration.account_id": "校准账号",
    "meta.hierarchy_calibration.follow_filled_detectable": "跟随输入框可识别信号",
    "meta.hierarchy_calibration.anchor_on_first_screen": "首屏锚点文字",
}

_KIND_LABELS: dict[str, str] = {
    "text_landmark": "界面文字",
    "resource_id": "控件 ID",
    "fill_calibrate": "待填字段",
    "feedback_landmark": "反馈·界面文字",
    "feedback_resource": "反馈·控件 ID",
    "feedback_note": "反馈备注",
}


def path_label(path: str) -> str:
    p = str(path or "").strip()
    if p in _PATH_LABELS:
        return _PATH_LABELS[p]
    if "hierarchy_calibration" in p:
        return "层级采集元数据"
    if "identify" in p:
        return "页面识别条件"
    if "guards" in p:
        return "弹窗/守卫检测"
    if "execute" in p:
        return "导航动作"
    if "effect_assert" in p:
        return "跳转后校验"
    return p or "未知字段"


def humanize_runtime_reason(reason: str) -> str:
    text = str(reason or "").strip()
    if not text:
        return ""
    if CALIBRATE_MARK in text:
        m = re.search(rf"未校准：(.+?) 仍是 {CALIBRATE_MARK}", text)
        field = path_label(m.group(1).strip() if m else "")
        return (
            f"导航配置尚未完成：「{field}」还是待填项。"
            "请确认已跑过用例（有采集数据），再点「一键发布」或「重新发布」；系统会自动用采集数据填入。"
        )
    if "没有 nav_fsm 配置" in text or "尚未配置导航图" in text:
        return "还没有发布导航配置。先跑一条用例，再回本页点「一键发布」即可。"
    if "project_id" in text:
        return "导航配置与当前应用的项目不一致，请在项目设置中确认 project_id。"
    if "account_id" in text and "不一致" in text:
        return "校准账号与跑批账号不一致，请用同一账号重新跑用例后再发布。"
    return text


def humanize_pending_list(paths: list[str], *, limit: int = 8) -> str:
    rows = [path_label(p) for p in (paths or [])[:limit]]
    if not rows:
        return "没有待填项，可以发布。"
    extra = len(paths) - limit
    tail = f"等共 {len(paths)} 项" if extra > 0 else ""
    return "仍缺：" + "、".join(rows) + (f"（{tail}）" if tail else "")


def candidate_kind_label(kind: str) -> str:
    return _KIND_LABELS.get(str(kind or "").strip(), "其他")


def assess_nav_fsm_quality(doc: dict) -> dict:
    """评估已发布导航图对跑批的实际价值（给人看，不影响校验门禁）。"""
    states = doc.get("states") or []
    nav_edges = [e for e in (doc.get("edges") or []) if str(e.get("kind") or "") == "nav"]
    recover_edges = [e for e in (doc.get("edges") or []) if str(e.get("kind") or "") == "recover"]
    recover_meta = (doc.get("meta") or {}).get("recover") or {}
    recover_cap_count = len(recover_meta.get("capabilities") or []) if recover_meta.get("runtime_only") else 0
    warnings: list[str] = []

    if len(states) < 2:
        warnings.append("识别出的页面少于 2 个，跑批时很难判断「当前在哪一屏」。")
    if not nav_edges:
        warnings.append("没有业务导航边：图不能指导「怎么去下一页」，只能辅助认屏和迷路恢复。")

    for st in states:
        if not isinstance(st, dict):
            continue
        identify = st.get("identify") or {}
        required = identify.get("required")
        blocks = required if isinstance(required, list) else ([required] if required else [])
        tab_label = ""
        any_list: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("signal") == "tab_bar":
                tab_label = str((block.get("match") or {}).get("selected") or "").strip()
            any_list.extend([str(x) for x in (block.get("any") or []) if str(x).strip()])
        label = tab_label or (any_list[0] if any_list else str(st.get("id") or "?"))
        if tab_label:
            continue
        if len(any_list) <= 1:
            warnings.append(f"「{label}」只靠 1 个关键字定位，容易和别的屏混淆。")
        has_tab_signal = any(
            isinstance(block, dict) and block.get("signal") == "tab_bar" for block in blocks
        )
        if str(st.get("kind") or "") == "dialog" and has_tab_signal:
            warnings.append(f"「{label}」带 Tab 栏识别信号，不应标成弹窗页。")

    effects = [
        "跑用例时：每步读取界面 → 判断属于哪个页面（localize）",
        "认屏失败或置信度低：可走恢复边（返回 / 回首页）",
    ]
    if nav_edges:
        effects.append(f"有 {len(nav_edges)} 条导航边：可校验「从 A 到 B」是否跳成功")
    else:
        effects.append("当前无导航边：不会替 Agent 规划点击路径")

    level = "good" if not warnings else "weak"
    if not states:
        level = "empty"

    summary = (
        "可用于跑批认屏与迷路恢复。"
        if level == "good"
        else "已发布但偏「草稿」：建议补采集（点 Tab 切换）或在高级里手调页面。"
    )
    return {
        "level": level,
        "usable": level == "good",
        "warnings": warnings,
        "effects": effects,
        "summary": summary,
        "nav_edge_count": len(nav_edges),
        "recover_edge_count": recover_cap_count or len(recover_edges),
        "state_count": len(states),
    }
