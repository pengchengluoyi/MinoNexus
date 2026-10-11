"""当前焦点输入的运行能力门禁。不凭 Scout 版本号猜测。"""
from __future__ import annotations

from typing import Any


def supports_focus_input(capabilities: Any, *, executor: str, platform: str, write_mode: str = "replace") -> bool:
    """节点上报的清单里是否有 input_text 的 current_focus + 写入模式。"""
    if not isinstance(capabilities, list):
        return False
    exe = str(executor or "").strip().lower()
    plat = str(platform or "").strip().lower()
    mode = str(write_mode or "replace").strip().lower()
    for row in capabilities:
        if not isinstance(row, dict) or str(row.get("id") or "") != "input_text":
            continue
        if exe and str(row.get("executor") or "").strip().lower() != exe:
            continue
        row_plat = str(row.get("platform") or "").strip().lower()
        if plat and row_plat and row_plat != plat:
            continue
        for combo in row.get("supported_combinations") or []:
            if not isinstance(combo, dict):
                continue
            if str(combo.get("target_mode") or "") != "current_focus":
                continue
            writes = {str(x) for x in (combo.get("write_modes") or [])}
            if mode in writes:
                return True
    return False


def _executor_for(ctx: Any) -> tuple[str, str]:
    from mino_nexus.loop.ui_channel import UiChannel, ui_channel_from_ctx

    channel = ui_channel_from_ctx(ctx)
    if channel == UiChannel.WEB:
        return "playwright", "web"
    if channel == UiChannel.IOS:
        return "ios_wda", "ios"
    return "adb", "android"


def _requires_focus_input(case: dict[str, Any]) -> bool:
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    compiled = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    app_id = str(case.get("app_id") or case.get("project_id") or "")
    for bundle in compiled.values():
        if not isinstance(bundle, dict):
            continue
        if "input_text" in (bundle.get("required_capabilities") or []):
            return True
        plan = bundle.get("do_program_plan") if isinstance(bundle.get("do_program_plan"), dict) else {}
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if str(step.get("hook_cap") or "") == "input_text":
                return True
            block_id = str(step.get("block_id") or "")
            if not block_id:
                continue
            from mino_nexus.services.nav_flow_block_catalog import resolve_effective_steps

            if any(
                str(row.get("hook_cap") or row.get("device_cap") or "") == "input_text"
                for row in resolve_effective_steps(app_id=app_id, block_id=block_id)
                if isinstance(row, dict)
            ):
                return True
    return False


def _prep_requires_focus_input(ctx: Any, case: dict[str, Any]) -> bool:
    """前置登录是隐式程序依赖，不能只查看 do 的编译步骤。"""
    pre = str(case.get("precondition") or case.get("precondition_raw") or "").strip()
    if not pre:
        return False
    from mino_nexus.loop.prep_program_plan import build_prep_program_plan, resolve_prep_claim

    plan = build_prep_program_plan(
        claim=resolve_prep_claim(case, ctx),
        scene=getattr(ctx, "case_scene", None), precondition=pre, ctx=ctx, case=case,
    )
    return any(
        str(step.get("hook_cap") or "") == "input_text"
        or (str(step.get("hook_cap") or "") == "relogin"
            and str(step.get("required_session") or "") == "logged_in")
        for step in plan.get("steps") or [] if isinstance(step, dict) and step.get("status") != "skipped"
    )


def focus_input_block_reason(ctx: Any) -> str:
    """看图方案里这次用例要写字，而节点没声明 current_focus 时，开跑前阻塞。"""
    from mino_nexus.action_space.scheme import action_scheme

    if action_scheme(ctx) == "dom":
        return ""
    case = getattr(ctx, "case", None)
    if not isinstance(case, dict):
        return ""
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    compiled = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    from mino_nexus.loop.step_pointer import build_seq_nodes

    if any(
        str(node.instruction or "").strip() and not isinstance(compiled.get(str(node.n)), dict)
        for node in build_seq_nodes(case)
    ):
        return "用例缺少完整的已编译执行计划；先重新编译用例再派发，不能跳过输入能力依赖检查"
    if not (_requires_focus_input(case) or _prep_requires_focus_input(ctx, case)):
        return ""
    sn = str(getattr(ctx, "sn", "") or "")
    if not sn:
        return ""
    from mino_nexus.services.node_registry import get_registry

    node, why = get_registry().resolve(sn)
    if node is None:
        return f"节点不可用，不能派发当前焦点输入：{why or sn}"
    executor, platform = _executor_for(ctx)
    caps = getattr(node, "runtime_capabilities", None)
    if supports_focus_input(caps, executor=executor, platform=platform, write_mode="replace"):
        return ""
    ver = str(getattr(node, "scout_version", "") or "未知")
    return (
        f"节点 {node.node_id}（正在运行的 Scout {ver}）未声明 "
        f"input_text/{executor}/{platform} 的 current_focus+replace。"
        "请升级并重启该节点后再跑。不会用补坐标掩盖不兼容。"
    )


def focus_reject_summary(summary: str, error: str = "") -> str:
    """只识别节点明确在写入前拒绝的焦点错误，兼容旧节点的错误前缀。"""
    reasons = {
        "contract_unsupported: 焦点目标已变化或引用失效",
        "contract_unsupported: 当前焦点不是可编辑控件",
        "contract_unsupported: 焦点点击引用失效",
        "focus_unavailable: page_context_missing",
        "focus_unavailable: page_changed",
        "focus_unavailable: document_changed",
        "focus_unavailable: document_check_failed",
        "focus_unavailable: no_active_element",
        "focus_unavailable: not_editable",
        "focus_unavailable: active_element_check_failed",
    }
    lines = list(dict.fromkeys(line.strip() for line in f"{summary}\n{error}".splitlines() if line.strip()))
    return "\n".join(lines) if lines and all(line in reasons for line in lines) else ""


def contract_reject_summary(summary: str, error: str = "") -> str:
    """旧 Scout 对无坐标输入的明确拒绝。只认这句，避免宽泛关键词。"""
    if focus_reject_summary(summary, error):
        return ""
    blob = "\n".join(dict.fromkeys(line.strip() for line in f"{summary}\n{error}".splitlines() if line.strip()))
    if "需要坐标 x/y" in blob or "contract_unsupported" in blob:
        return blob.strip()[:300]
    return ""
