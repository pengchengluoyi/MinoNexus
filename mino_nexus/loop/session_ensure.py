"""会话编排：按 CaseScene 租号 / 核对登录态 / 只走图上 logout 边。

不扫被测 App 文案。退出只认 NavFSM `action_type=logout`。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from mino_nexus.core.log import SLog
from mino_nexus.runtime.session_gate import (
    ensure_case_scene,
    required_session,
    session_prep_intent,
)
from mino_nexus.services.account_pool_templates import infer_template_id_from_text

TAG = "SessionEnsure"
_SESSION_RE = re.compile(r"session=(\w+)", re.I)


def account_need_from_case(
    case: dict[str, Any] | None,
    scene: dict[str, Any] | None,
) -> dict[str, Any]:
    row = ensure_case_scene(case or {}, scene if isinstance(scene, dict) else None)
    req = required_session(scene=row)
    prep = session_prep_intent(scene=row)
    pre = str(
        row.get("precondition")
        or (scene or {}).get("precondition")
        or (case or {}).get("precondition")
        or (case or {}).get("precondition_raw")
        or ""
    ).strip()
    session = ""
    if req == "logged_in" or prep == "relogin":
        session = "logged_in"
    elif req == "guest" or prep == "logout":
        session = "guest"
    blob = pre
    profile = bool(re.search(r"资料|昵称|头像|profile", blob, re.I))
    address = bool(re.search(r"地址|收货|address", blob, re.I))
    template_id = str(
        row.get("account_template_id")
        or row.get("template_id")
        or (case or {}).get("account_template_id")
        or (case or {}).get("template_id")
        or ""
    ).strip()
    if not template_id:
        template_id = infer_template_id_from_text(pre)
    lease_requirements = row.get("lease_requirements")
    if not isinstance(lease_requirements, dict):
        lease_requirements = (case or {}).get("lease_requirements") if isinstance((case or {}).get("lease_requirements"), dict) else {}
    need_account = bool(session or pre or profile or address or template_id)
    return {
        "need_account": need_account,
        "session": session,
        "prompt": pre,
        "profile": profile,
        "address": address,
        "template_id": template_id,
        "requirements": dict(lease_requirements or {}),
    }


def parse_session_value(block: str) -> str:
    match = _SESSION_RE.search(str(block or ""))
    if not match:
        return ""
    return str(match.group(1) or "").strip().lower()


def session_mismatch_reason(
    *,
    scene: dict[str, Any] | None,
    session_block: str,
) -> Optional[str]:
    req = required_session(scene=scene)
    current = parse_session_value(session_block)
    if not req or req == "any" or not current or current in ("unknown",):
        return None
    if req == "guest" and current == "logged_in":
        return (
            "当前仍是已登录会话，本条要求未登录。"
            "请走路线图 logout 边或 recover_restart_target_app 后观察，禁止 signal_done。"
        )
    if req == "logged_in" and current in ("logged_out", "guest"):
        return (
            "当前未登录，本条要求已登录。"
            "请先完成登录（lease_account / get_otp / input_text），禁止 signal_done。"
        )
    return None


def ensure_case_account(ctx: Any, case: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
    """开跑时按场景租号；ctx 上账号与当前前置不一致时由 lease_for_context 重选。"""
    scene = getattr(ctx, "case_scene", None) or {}
    need = account_need_from_case(case or {}, scene if isinstance(scene, dict) else {})
    if not need.get("need_account"):
        return None, ""
    from mino_nexus.services.account_lease import lease_for_context

    params: dict[str, Any] = {}
    prompt = str(need.get("prompt") or "").strip()
    if prompt:
        params["precondition"] = prompt
    observed = getattr(ctx, "account_probe_facets", None)
    obs_map = observed if isinstance(observed, dict) else None
    from mino_nexus.services.account_lease import INTERACTIVE_ACQUIRE_WAIT_MS

    row, err = lease_for_context(
        ctx,
        params,
        ai_reasoning=prompt,
        need_facets=need,
        observed_by_account=obs_map,
        wait_ms=INTERACTIVE_ACQUIRE_WAIT_MS,
    )
    if row:
        score = int(row.get("score") or 0)
        reason = str(row.get("reason") or "")
        if score < 0:
            SLog.w(TAG, f"auto-lease weak match score={score} reason={reason}")
        SLog.i(TAG, f"auto-leased {row.get('id') or row.get('phone') or '?'}")
        return row, ""
    SLog.w(TAG, f"auto-lease skipped: {err}")
    return None, err or ""


def find_logout_edges(fsm: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ed in (fsm or {}).get("edges") or []:
        if not isinstance(ed, dict):
            continue
        meta = ed.get("meta") if isinstance(ed.get("meta"), dict) else {}
        if str(meta.get("action_type") or "").strip().lower() == "logout":
            out.append(ed)
    return out


def _edge_exec_params(edge: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    exe = edge.get("execute") if isinstance(edge.get("execute"), dict) else {}
    steps = [str(s).strip() for s in (exe.get("steps") or []) if str(s).strip()]
    if "press_key" in steps or str(exe.get("key") or "").strip():
        key = str(exe.get("key") or "BACK").strip() or "BACK"
        return "press_key", {"key": key}
    sel = str(exe.get("selector_text") or exe.get("text") or exe.get("target_page") or "").strip()
    if sel:
        return "tap_element", {"selector_text": sel, "text": sel}
    meta = edge.get("meta") if isinstance(edge.get("meta"), dict) else {}
    label = str(meta.get("action_label") or "").strip()
    if label:
        tab = label.split("·", 1)[-1].strip() if "·" in label else label
        if tab:
            return "tap_element", {"selector_text": tab, "text": tab}
    return "", {}


def try_logout_via_nav(
    ctx: Any,
    router: Any,
    *,
    seq: int = 0,
) -> tuple[bool, str]:
    """只执行图上 action_type=logout 的边。没有边则失败，不猜文案。"""
    app_id = str(getattr(ctx, "app_id", "") or "").strip()
    if not app_id:
        return False, "缺少 app_id，无法查路线图 logout 边"
    from mino_nexus.services import nav_route

    fsm_doc, _ = nav_route.load_fsm_doc(
        app_id,
        project_id=str(getattr(ctx, "nav_project_id", "") or ""),
        use_live=False,
        app_version=str(getattr(ctx, "app_version", "") or ""),
    )
    edges = find_logout_edges(fsm_doc or {})
    if not edges:
        return False, "路线图没有 action_type=logout 的边；请 recover_restart_target_app"
    cap, params = _edge_exec_params(edges[0])
    if not cap or not params:
        return False, "logout 边未配置可执行动作"
    if router is None:
        return False, "未连接设备，无法执行 logout"
    from mino_nexus.core.schemas import PlanEvent
    from mino_nexus.loop.web_env import agent_step_idx

    event = PlanEvent(
        seq=seq,
        capability_id=cap,
        event_kind=cap,
        params=params,
        ai_reasoning="session_ensure logout 边",
        label="logout",
    )
    scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
    case_seq = int(getattr(ctx, "case_seq", 0) or 0)
    result = router.dispatch(event, run_id=scout_run_id, step_idx=agent_step_idx(case_seq, seq))
    st = result.status.value if hasattr(result.status, "value") else str(result.status)
    if st in ("pass", "done"):
        return True, str(result.summary or "已执行 logout 边")
    return False, str(result.summary or result.error or "logout 边执行失败")
