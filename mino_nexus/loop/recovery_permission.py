"""系统权限弹窗：优先点「使用时允许」类；其次点厂商 grant 文案；不点拒绝/不允许。"""
from __future__ import annotations

import re
from typing import Any, Optional

_PREFERRED_ALLOW_ORDER = (
    "仅在使用中允许",
    "仅在使用该应用时允许",
    "使用时允许",
    "使用应用时允许",
    "Allow while using the app",
    "While using the app",
    "允许",
)

_ALLOW_RE = re.compile(
    r"仅在使用中允许|仅在使用该应用时允许|使用时允许|使用应用时允许|"
    r"Allow while using the app|While using the app|允许",
    re.I,
)

_GRANT_LIKE_RE = re.compile(
    r"使用.{0,8}应用|使用.{0,6}时|应用时|仅此一次|始终允许|允许访问|"
    r"授予.{0,4}权限|授权|权限|Allow|While using|Only this time|All the time|"
    r"Grant|Permission",
    re.I,
)

_DENY_RE = re.compile(
    r"拒绝|不允许|禁止|勿允许|Deny|Don't allow|Do not allow|Never",
    re.I,
)


def _node_texts(node: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("text", "content_desc", "label"):
        val = str(node.get(key) or "").strip()
        if val:
            out.append(val)
    return out


def pick_permission_allow_text(
    *,
    screen_texts: list[str] | None = None,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
    match_reasons: list[str] | None = None,
) -> Optional[str]:
    """从 match reasons / 屏上 OCR / hierarchy 挑最可信的一条 allow 文案。"""
    candidates: list[str] = []
    for r in match_reasons or []:
        text = str(r or "")
        if text.startswith("screen_text~"):
            candidates.append(text.split("~", 1)[-1].strip())
    for t in screen_texts or []:
        t = str(t or "").strip()
        if t and _ALLOW_RE.search(t) and not _DENY_RE.search(t):
            candidates.append(t)
    for node in hierarchy_nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("clickable") is False:
            continue
        for val in _node_texts(node):
            if val and _ALLOW_RE.search(val) and not _DENY_RE.search(val):
                candidates.append(val)
    if not candidates:
        return None
    ranked: list[tuple[int, str]] = []
    for raw in candidates:
        for i, pref in enumerate(_PREFERRED_ALLOW_ORDER):
            if pref in raw or raw in pref:
                ranked.append((i, pref if len(raw) > 24 else raw[:32]))
                break
        else:
            ranked.append((len(_PREFERRED_ALLOW_ORDER), raw[:32]))
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


def pick_permission_grant_text(
    *,
    screen_texts: list[str] | None = None,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
    match_reasons: list[str] | None = None,
) -> Optional[str]:
    """Allow 正则未命中时，匹配厂商 grant 类按钮（含「使用应用时」等）。"""
    pool: list[str] = []
    for r in match_reasons or []:
        text = str(r or "")
        if text.startswith("screen_text~"):
            pool.append(text.split("~", 1)[-1].strip())
    for t in screen_texts or []:
        t = str(t or "").strip()
        if t and _GRANT_LIKE_RE.search(t) and not _DENY_RE.search(t):
            pool.append(t)
    for node in hierarchy_nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("clickable") is False:
            continue
        for val in _node_texts(node):
            if len(val) > 48:
                continue
            if val and _GRANT_LIKE_RE.search(val) and not _DENY_RE.search(val):
                pool.append(val)
    if not pool:
        return None
    ranked: list[tuple[int, str]] = []
    for raw in pool:
        score = 50
        if "使用" in raw and "应用" in raw:
            score = 5
        elif "使用时" in raw or "应用时" in raw:
            score = 8
        elif "允许" in raw:
            score = 12
        elif re.search(r"Allow|While using", raw, re.I):
            score = 10
        ranked.append((score, raw[:32]))
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


def pick_permission_tap_for_grant(
    *,
    screen_texts: list[str] | None = None,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
    match_reasons: list[str] | None = None,
) -> tuple[Optional[str], str]:
    """返回 (tap_text, kind) kind ∈ allow|grant|''。"""
    allow = pick_permission_allow_text(
        screen_texts=screen_texts,
        hierarchy_nodes=hierarchy_nodes,
        match_reasons=match_reasons,
    )
    if allow:
        return allow, "allow"
    grant = pick_permission_grant_text(
        screen_texts=screen_texts,
        hierarchy_nodes=hierarchy_nodes,
        match_reasons=match_reasons,
    )
    if grant:
        return grant, "grant"
    return None, ""


def permission_choice_dialog_present(
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> bool:
    """同时有「使用时允许」和「拒绝」才算系统权限二选一，避免误点页面里的允许。"""
    allow, _kind = pick_permission_tap_for_grant(hierarchy_nodes=hierarchy_nodes)
    if not allow:
        return False
    for node in hierarchy_nodes or []:
        if not isinstance(node, dict) or node.get("clickable") is False:
            continue
        for val in _node_texts(node):
            if _DENY_RE.search(val):
                return True
    return False


def pick_permission_dismiss_text(
    *,
    hierarchy_nodes: list[dict[str, Any]] | None = None,
) -> Optional[str]:
    """仅用于非权限挡屏的「取消/关闭」，绝不返回拒绝/不允许。"""
    pool = [n for n in (hierarchy_nodes or []) if isinstance(n, dict)]
    found: list[str] = []
    for node in pool:
        if not node.get("clickable"):
            continue
        for val in _node_texts(node):
            if not val or len(val) > 8:
                continue
            if _DENY_RE.search(val):
                continue
            for pref in ("取消", "Cancel", "关闭", "Close"):
                if pref in val or val == pref:
                    found.append(val if len(val) <= 8 else pref)
                    break
    if not found:
        return None
    for pref in ("取消", "Cancel", "关闭", "Close"):
        if pref in found:
            return pref
    return found[0]


def system_dialog_present(hierarchy_nodes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """结构上确认前台是 Android 系统弹窗（权限 / 安装器 / 选择器）。

    与 ``permission_choice_dialog_present`` 的区别：不要求「允许 + 拒绝」同时可见，
    也不要求有等待回看的业务动作。只要有系统弹窗宿主包名 + 至少一个可点选择，
    就认定是系统弹窗 —— 这类挡屏应该由 ``fb.global.system_dialog`` 处理，
    而不是判成「异应用」把截图扣掉（cr-a390541e5dd2 的 214/226/233）。
    """
    from mino_nexus.navigation.atlas.nav_target_scope import is_system_dialog_pkg

    owners = ("com.android.permissioncontroller", "com.google.android.permissioncontroller",
              "com.android.packageinstaller", "com.google.android.packageinstaller",
              "com.android.documentsui", "com.android.providers.media.module")
    nodes = [n for n in (hierarchy_nodes or [])
             if isinstance(n, dict) and n.get("visible", True) and not n.get("cross_origin")]
    hosting = [n for n in nodes
               if any(str(n.get("package") or "") == owner or str(n.get("resource_id") or "").startswith(owner + ":id/")
                      for owner in owners)]
    if not hosting:
        return {}
    titles = [str(n.get("text") or n.get("content_desc") or "").strip() for n in hosting
              if str(n.get("resource_id") or "").endswith(":id/alertTitle")]
    title = next((t for t in titles if t), "")
    if not title:
        title = next((str(n.get("text") or "").strip() for n in nodes
                      if str(n.get("clickable")) in ("True", "true", "1") and str(n.get("text") or "").strip()), "")
    if not title:
        return {}
    package = str(hosting[0].get("package") or "").split(":")[0] or next(
        (owner for owner in owners if str(hosting[0].get("resource_id") or "").startswith(owner + ":id/")), "")
    return {"title": title, "package": package, "dialog_kind": "permission" if is_system_dialog_pkg(package) else "system"}


def system_dialog_block_id(ctx: Any) -> str:
    """编译期定好的系统弹窗处置块（allow / deny），缺省按允许。"""
    scene = getattr(ctx, "case_scene", None) or {}
    block = str(scene.get("system_dialog_block_id") or "").strip() if isinstance(scene, dict) else ""
    return block or "fb.global.system_dialog"


def _dialog_button_bounds(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """从系统弹窗结构里挑「允许」类按钮，返回它的 bounds 与文案。

    块里的 ``tap_element`` 走结构锚点（DOM），厂商文案和资源 id 一变就整块失败；
    而节点本身带 bounds —— 按 bounds 点中心是不需要猜的确定性动作。
    优先级：允许类文案 > 任意可点且非拒绝文案 > 无。
    """
    best: tuple[int, dict[str, Any]] = (-1, {})
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("visible", True)) in ("False", "false") or str(node.get("enabled", True)) in ("False", "false"):
            continue
        bounds = node.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        texts = " ".join(str(node.get(k) or "") for k in ("text", "content_desc", "label")).strip()
        if not texts:
            continue
        if _DENY_RE.search(texts):
            continue
        clickable = str(node.get("clickable")) in ("True", "true", "1")
        score = -1
        if _ALLOW_RE.search(texts):
            score = 100 + (10 if clickable else 0)
            for rank, word in enumerate(_PREFERRED_ALLOW_ORDER):
                if word and word.lower() in texts.lower():
                    score += 20 - rank
                    break
        elif clickable and _GRANT_LIKE_RE.search(texts):
            score = 10
        if score > best[0]:
            best = (score, {"bounds": [int(v) for v in bounds], "text": texts, "clickable": clickable})
    return best[1] if best[0] >= 0 else {}


def tap_dialog_button(router: Any, ctx: Any, info: dict[str, Any], *, writer: Any = None,
                      shot: Any = None) -> dict[str, Any]:
    """按 bounds 点系统弹窗里的允许按钮（块的 DOM 锚点失败时的确定性兜底）。"""
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or [])
             if isinstance(n, dict) and n.get("visible", True) and not n.get("cross_origin")]
    host = str(info.get("package") or "")
    scoped = [n for n in nodes if host and str(n.get("package") or "").split(":")[0] == host] or nodes
    pick = _dialog_button_bounds(scoped)
    if not pick:
        return {"ok": False, "reason": "no_dialog_button"}
    shot = shot if shot is not None else getattr(ctx, "last_shot", None)
    width = int(getattr(shot, "width", 0) or getattr(ctx, "screen_width", 0) or 0)
    height = int(getattr(shot, "height", 0) or getattr(ctx, "screen_height", 0) or 0)
    left, top, right, bottom = pick["bounds"]
    if width <= 0 or height <= 0:
        # 没有真实画面尺寸就不能自己编：按 bounds 反推会把相对坐标算偏
        # （cr-d1189fc4ddcf 用 1078×2084 当屏宽高，点出来的 y=964 实际落到 2514px）。
        return {"ok": False, "reason": "screen_size_unknown", "bounds": pick["bounds"]}
    x_param = {"x": int(round(((left + right) / 2.0) / width * 1000)),
               "y": int(round(((top + bottom) / 2.0) / height * 1000))}
    from mino_nexus.loop.recovery import _dispatch
    from mino_nexus.core.schemas import PlanEvent

    event = PlanEvent(
        seq=0, capability_id="tap_element", event_kind="tap_element",
        params={**x_param, "point_policy": "coordinate", "package": str(getattr(ctx, "target_package", "") or "")},
        ai_reasoning="系统弹窗按节点 bounds 点允许", label="点系统弹窗允许",
    )
    # 这一拍必须走坐标：DOM 方案会把 x/y 抹掉并改回 node 锚点，
    # Scout 于是回「DOM 点击锚点未命中，不用坐标兜底」，坐标就成了死参数。
    prev_scheme = getattr(ctx, "action_scheme", None)
    prev_obs = getattr(ctx, "observation_mode", None)
    try:
        ctx.action_scheme = "visual"
        ctx.observation_mode = "visual"
        res = _dispatch(router, ctx, event, agent_turn=int(getattr(ctx, "turn", 0) or 0),
                        diagnostic_purpose="recovery_action")
    finally:
        if prev_scheme is None:
            try:
                delattr(ctx, "action_scheme")
            except AttributeError:
                pass
        else:
            ctx.action_scheme = prev_scheme
        if prev_obs is None:
            try:
                delattr(ctx, "observation_mode")
            except AttributeError:
                pass
        else:
            ctx.observation_mode = prev_obs
    status = str(getattr(getattr(res, "status", None), "value", getattr(res, "status", "")) or "")
    ok = status == "pass"
    if writer:
        writer.append("recovery/dialog_bounds_tap", {
            "status": status or "fail", "text": pick.get("text"), "bounds": pick["bounds"],
            "xy": x_param, "summary": str(getattr(res, "summary", ""))[:200],
        })
    return {"ok": ok, "summary": f"按 bounds 点「{pick.get('text')}」", "bounds": pick["bounds"], **x_param}


def try_handle_system_dialog(
    router: Any, ctx: Any, cursor: Any, *, turn_seq: int, writer: Any = None, shot: Any = None,
) -> Optional[dict[str, Any]]:
    """系统弹窗直接走 ``fb.global.system_dialog`` 逻辑块。

    不依赖「业务动作待回看」，也不让模型看着一张被扣掉的截图猜：
    当前结构能确认是系统弹窗，就派块（点允许/拒绝 → 确认 → 核验），
    本回合按 defer 结束，下一回合重新观察。
    """
    from mino_nexus.action_space.scheme import action_scheme
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

    if action_scheme(ctx) != "dom_preferred" or ui_channel_from_ctx(ctx) != UiChannel.ANDROID:
        return None
    info = system_dialog_present(getattr(ctx, "nav_hierarchy_nodes", None))
    if not info:
        return None
    attempts = getattr(cursor, "system_dialog_block_attempts", None)
    if not isinstance(attempts, dict):
        attempts = {}
    key = str(info.get("title") or "")
    if key in attempts or len(attempts) >= 4:
        return {"ok": False, "status": "blocked", "capability_id": "tap_element",
                "summary": "系统弹窗点击后仍未消失，或处理次数已达上限；原业务动作尚未确认。"}
    attempts[key] = str(info.get("package") or "")
    cursor.system_dialog_block_attempts = attempts
    block = system_dialog_block_id(ctx)
    from mino_nexus.loop.block_dispatch import run_block

    out = run_block(block, ctx=ctx, router=router, purpose="recovery_action",
                    target_package=str(getattr(ctx, "target_package", "") or ""))
    ok = bool(out.get("ok"))
    if not ok:
        # 块的允许步骤走 DOM 锚点，厂商文案/资源 id 一变就整块失败；
        # 结构里已经有按钮 bounds，按中心点点一次是确定的，不需要模型或猜测。
        fb = tap_dialog_button(router, ctx, info, writer=writer, shot=shot)
        if fb.get("ok"):
            ok = True
            out = {**out, "summary": fb.get("summary"), "fallback": "dialog_bounds_tap"}
    if writer:
        writer.append("recovery/system_dialog", {
            "status": "pass" if ok else "fail", "block_id": block, "title": key,
            "package": info.get("package"), "turn": int(turn_seq),
            "business_result_confirmed": False, "fresh_observation_required": True,
        })
    return {"ok": ok, "status": "defer" if ok else "blocked", "capability_id": "tap_element",
            "block_id": block, "title": key,
            "summary": (f"系统弹窗「{key}」由 {block} 处理" if ok else f"系统弹窗「{key}」处理失败：{out.get('error') or out.get('summary') or ''}")}


def try_recover_pending_system_permission(
    router: Any, ctx: Any, cursor: Any, *, turn_seq: int, writer: Any = None,
) -> Optional[dict[str, Any]]:
    """业务动作待回看时先处理明确的 Android 系统权限，不裁定原业务动作。"""
    from mino_nexus.action_space.scheme import action_scheme
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

    prior = getattr(cursor, "visual_prior", None)
    if (not isinstance(prior, dict) or not prior or action_scheme(ctx) != "dom_preferred"
            or ui_channel_from_ctx(ctx) != UiChannel.ANDROID):
        return None
    nodes = [n for n in (getattr(ctx, "nav_hierarchy_nodes", None) or [])
             if isinstance(n, dict) and n.get("visible", True) and n.get("enabled", True)
             and not n.get("cross_origin")]
    owners = ("com.android.permissioncontroller", "com.google.android.permissioncontroller")
    controller_nodes = [n for n in nodes if any(
        str(n.get("resource_id") or "").startswith(owner + ":id/") for owner in owners)]
    if not controller_nodes or not permission_choice_dialog_present(nodes):
        return None
    titles = [str(n.get("text") or n.get("content_desc") or "") for n in controller_nodes
              if str(n.get("resource_id") or "").endswith(":id/alertTitle")]
    if len(titles) != 1 or not titles[0].strip():
        return None
    from mino_nexus.loop.recovery import collect_evidence, invalidate_evidence_cache

    evidence = collect_evidence(ctx, router, target_package=str(getattr(ctx, "target_package", "") or ""))
    if evidence.target_alive != "yes" or evidence.foreground_pkg not in owners:
        return None
    # 同一权限提示仅派发一次；不因状态不明重放点击，也不无限循环处理新权限。
    attempts = getattr(cursor, "system_permission_recovery_attempts", None)
    if not isinstance(attempts, dict):
        attempts = {}
    key = titles[0]
    if key in attempts or len(attempts) >= 4:
        return {"ok": False, "status": "blocked", "capability_id": "tap_element",
                "summary": "系统权限点击后仍未消失，或权限恢复次数已达上限；原业务动作尚未确认。"}
    attempts[key] = True
    cursor.system_permission_recovery_attempts = attempts
    from mino_nexus.loop.system_dialog_recovery import try_proactive_system_permission

    previous_mode = getattr(ctx, "observation_mode", "visual")
    previous_proxy_mode = getattr(router, "observation_mode", "visual")
    ctx.observation_mode = "dom"
    router.observation_mode = "dom"
    try:
        result = try_proactive_system_permission(ctx=ctx, router=router,
                    target_package=str(getattr(ctx, "target_package", "") or ""), turn_seq=turn_seq)
    finally:
        ctx.observation_mode = previous_mode
        router.observation_mode = previous_proxy_mode
        invalidate_evidence_cache(ctx)
    result = result or {"ok": False, "summary": "当前系统权限控件未能定位"}
    result["status"] = "defer" if result.get("ok") else "blocked"
    result["capability_id"] = "tap_element"
    if writer:
        writer.append("recovery/system_permission", {
            "status": result["status"], "source": "current_system_structure",
            "parent_step_id": str(prior.get("milestone_id") or ""),
            "attempt_ref": str(prior.get("attempt_ref") or ""),
            "business_result_confirmed": False, "fresh_observation_required": True,
        })
    return result
