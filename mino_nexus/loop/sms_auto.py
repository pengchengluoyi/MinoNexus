"""短信发送：几何成立时程序 request_sms_code，降低 LLM 选路方差。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.loop.step_contract import instruction_allows_login_flow
from mino_nexus.loop.web_env import frame_step
from mino_nexus.runtime.session_gate import compile_sms_send_hint


def sms_send_geometry_ready(
    *,
    accounts_brief: str,
    hierarchy_nodes: list[dict[str, Any]] | None,
    has_request_sms_code: bool,
    ui_channel: str = "android",
) -> bool:
    if not has_request_sms_code:
        return False
    hint = compile_sms_send_hint(
        accounts_brief=accounts_brief,
        hierarchy_nodes=hierarchy_nodes,
        has_request_sms_code=True,
        ui_channel=ui_channel,
    )
    return bool(hint)


def try_auto_request_sms_code(
    proxy: RouterProxy,
    ctx: Any,
    *,
    run_id: str,
    case_seq: int,
    turn_seq: int,
    instruction: str,
    login_module_case: bool,
    hierarchy_nodes: list[dict[str, Any]] | None,
    menu_cap_ids: list[str],
    sms_auto_attempted: bool,
    history_has_send_pass: bool,
) -> Optional[dict[str, Any]]:
    """本步最多尝试一次程序发码。返回 trace 摘要 dict 或 None。"""
    if sms_auto_attempted or history_has_send_pass:
        return None
    if not instruction_allows_login_flow(instruction, login_module_case=login_module_case):
        return None
    brief = str(getattr(ctx, "accounts_brief", "") or "")
    has_cap = "request_sms_code" in set(menu_cap_ids or [])
    if not sms_send_geometry_ready(
        accounts_brief=brief,
        hierarchy_nodes=hierarchy_nodes,
        has_request_sms_code=has_cap,
    ):
        return None
    event = PlanEvent(
        seq=int(turn_seq),
        capability_id="request_sms_code",
        event_kind="request_sms_code",
        params={},
        ai_reasoning="程序：手机号已填且发送控件可见，自动 request_sms_code",
        label="程序发送验证码",
    )
    step_idx = frame_step(case_seq, max(1, int(turn_seq)))
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.loop.local_executors import dispatch_local

    res = dispatch_local(
        event,
        ctx=ctx,
        router=proxy,
        target_package=str(getattr(ctx, "target_package", "") or ""),
    )
    st = res.status.value if hasattr(res.status, "value") else str(res.status)
    ok = str(st) in ("pass", EventStatus.PASS.value)
    return {
        "ok": ok,
        "status": st,
        "summary": str(res.summary or res.error or "request_sms_code"),
        "capability_id": "request_sms_code",
    }
