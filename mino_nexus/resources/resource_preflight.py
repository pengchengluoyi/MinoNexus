"""跑批 Preflight：Claim 对照登记簿与 RunContext，prep 收工硬门槛。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.core.step_text import precondition_requires_clear_cache
from mino_nexus.resources.session.scene_gate import clamp_case_scene
from mino_nexus.resources.case_resource_claim import (
    _case_resource_key,
    ensure_resource_key_on_case,
)


def _claim_from_case(case: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(case, dict):
        return None
    rk = _case_resource_key(case)
    if rk:
        return rk
    pre = str(case.get("precondition") or "").strip()
    if not pre:
        return None
    return ensure_resource_key_on_case(case)


def claim_requires_clear_cache(
    claim: dict[str, Any] | None,
    scene: dict[str, Any] | None,
    precondition: str = "",
) -> bool:
    if precondition_requires_clear_cache(precondition):
        return True
    scene_row = clamp_case_scene(scene if isinstance(scene, dict) else None)
    for it in scene_row.get("prep_items") or []:
        if isinstance(it, dict) and str(it.get("kind") or "") == "clear_cache":
            return True
    da = (claim or {}).get("device_app") if isinstance((claim or {}).get("device_app"), dict) else {}
    for it in da.get("prep") or []:
        if isinstance(it, dict) and str(it.get("kind") or "") == "clear_cache":
            return True
    return False


def effective_device_session(
    ctx: Any,
    *,
    sn: str,
    package_id: str,
) -> str:
    fact = dict(getattr(ctx, "session_fact", None) or {})
    fs = str(fact.get("session") or "").strip().lower()
    dirty = bool(getattr(ctx, "session_dirty", False))
    from mino_nexus.resources.accounts.session_match import normalize_device_session

    if dirty or fact.get("stale_reason"):
        return "unknown"
    if fs:
        return normalize_device_session(fs)
    from mino_nexus.contract.execution.device import is_web_slot
    if is_web_slot(sn, str(getattr(ctx, "platform", "") or "")):
        # 浏览器记录只供展示；本次 context 未观察时不能继承上次任务。
        return "unknown"
    from mino_nexus.resources.device_app_session_store import get_session

    row = get_session(sn, package_id)
    if row and not row.get("stale"):
        return normalize_device_session(row.get("session") or "")
    return normalize_device_session(fs)


def _session_allows_required(required: str, current: str, allow: list[str]) -> bool:
    from mino_nexus.resources.accounts.session_match import device_session_meets

    return device_session_meets(required, current, allow)


def run_start_resource_blockers(
    claim: dict[str, Any] | None,
    *,
    scene: dict[str, Any] | None,
    precondition: str,
    sn: str,
    package_id: str,
) -> list[str]:
    """开跑前不可由 prep 自动修复的缺口 → 直接拒跑。"""
    blockers: list[str] = []
    if not str(sn or "").strip():
        blockers.append("未指定设备 sn，无法核对机态")
    if not str(package_id or "").strip():
        blockers.append("未指定被测 App 包名")
    if not claim:
        return blockers
    plat = str(claim.get("platform") or "").strip().lower()
    da_plat = claim.get("device_app") if isinstance(claim.get("device_app"), dict) else {}
    if not plat and isinstance(da_plat, dict):
        plat = str(da_plat.get("platform") or "").strip().lower()
    if plat == "web" and not str(package_id or "").strip():
        blockers.append("Web 用例需指定 URL / 包名（playwright 目标）")
    if plat == "ios" and not str(sn or "").strip():
        blockers.append("iOS 用例需指定设备 sn（WDA 节点）")
    scene_row = clamp_case_scene(scene if isinstance(scene, dict) else None)
    pre = str(precondition or "").strip()
    da = claim.get("device_app") if isinstance(claim.get("device_app"), dict) else {}
    req_sess = str(
        da.get("required_session") or scene_row.get("required_session") or ""
    ).strip().lower()
    needs_guest = req_sess in ("guest", "logged_out") or scene_row.get("required_session") == "guest"
    if not needs_guest:
        return blockers
    from mino_nexus.resources.session.scene_gate import session_prep_intent
    # 差异可由前置退出修复，prep 完成时仍须当前观察证明未登录。
    if plat == "web" or session_prep_intent(scene=scene_row) == "logout":
        return blockers
    from mino_nexus.resources.device_app_session_store import get_session

    row = get_session(str(sn).strip(), str(package_id).strip()) or {}
    from mino_nexus.resources.accounts.session_match import normalize_device_session

    cur = "unknown" if row.get("stale") else normalize_device_session(row.get("session") or "")
    if cur != "logged_in":
        return blockers
    blockers.append(
        "【拒跑】登记簿机态为 logged_in，用例要求未登录/已登出/游客，且未声明退出登录前置。"
        "请先通过应用退出并重新观察确认；清缓存不等于退出登录。"
    )
    return blockers


def _pre_avatar_setup_lane(precondition: str) -> bool:
    return bool(re.search(r"未配置形象", str(precondition or ""), re.I))


def prep_avatar_setup_gate_issues(ctx: Any, precondition: str) -> list[str]:
    """前置「未配置形象」核对明确的形象字段；缓存不是业务复位证据。"""
    pre = str(precondition or "").strip()
    if not _pre_avatar_setup_lane(pre):
        return []
    lease = getattr(ctx, "resource_lease", None) or {}
    facets: dict[str, Any] = {}
    if isinstance(lease, dict) and isinstance(lease.get("facets"), dict):
        facets = lease["facets"]
    issues: list[str] = []
    from mino_nexus.resources.accounts.requirement_compile import PROFILE_SHAPE_FACETS

    for fk in PROFILE_SHAPE_FACETS:
        fv = str(facets.get(fk) or "").strip().lower()
        if fv in ("yes", "有", "filled"):
            issues.append(
                f"【资源门槛】前置「未配置形象」，但租约 {fk}={fv}；"
                "须使用实际满足形象初态的账号；清缓存不能重置账号形象。"
            )
            break
    return issues


def prep_resource_gate_issues(
    ctx: Any,
    case: dict[str, Any] | None,
    *,
    history_lines: list[str] | None = None,
) -> list[str]:
    """prep 阶段 signal_done 前必须为空。"""
    if not isinstance(case, dict):
        return []
    sn = str(getattr(ctx, "sn", "") or "").strip()
    pkg = str(getattr(ctx, "target_package", "") or "").strip()
    if not sn or not pkg:
        return []

    claim = _claim_from_case(case)
    scene = clamp_case_scene(
        getattr(ctx, "case_scene", None) if isinstance(getattr(ctx, "case_scene", None), dict) else None
    )
    pre = str(case.get("precondition") or scene.get("precondition") or "").strip()
    issues: list[str] = []

    from mino_nexus.resources.session.prep_trust import prep_lease_account_signal_done_block_reason

    lease_msg = prep_lease_account_signal_done_block_reason(
        ctx=ctx,
        case=case,
        scene=scene,
        history_lines=history_lines,
    )
    if lease_msg:
        issues.append(lease_msg)

    if claim_requires_clear_cache(claim, scene, pre):
        if not bool(getattr(ctx, "prep_clear_done", False)):
            issues.append(
                "【资源门槛】前置要求清除应用缓存：须本任务 clear_app_cache 成功，不可仅凭 history 或 signal_done 跳过。"
            )

    da = claim.get("device_app") if isinstance(claim, dict) and isinstance(claim.get("device_app"), dict) else {}
    req_sess = str(da.get("required_session") or scene.get("required_session") or "").strip().lower()
    allow = [str(x).strip().lower() for x in (da.get("allow") or []) if str(x).strip()]
    from mino_nexus.resources.session.scene_gate import session_prep_intent

    prep_intent = session_prep_intent(scene=scene)
    defer_device_session_gate = (
        req_sess == "logged_in"
        and prep_intent == "relogin"
        and not bool(getattr(ctx, "_login_flow_transition_emitted", False))
        and bool(getattr(ctx, "login_flow_macro_active", False))
    )
    if (
        req_sess
        and req_sess not in ("any", "")
        and not defer_device_session_gate
    ):
        cur = effective_device_session(ctx, sn=sn, package_id=pkg)
        if not _session_allows_required(req_sess, cur, allow):
            hint = ""
            if req_sess == "logged_in" and cur == "unknown":
                hint = "请先完成登录流块或刷新登记簿机态。"
            issues.append(
                f"【资源门槛】device_app.session 需要 {req_sess}"
                f"（允许 {allow or [req_sess]}），当前 {cur}。{hint}"
            )

    binding = str(da.get("binding") or "").strip()
    if (
        binding == "must_match_lease"
        and req_sess == "logged_in"
        and not defer_device_session_gate
    ):
        pid, lease_aid = "", ""
        lease = getattr(ctx, "resource_lease", None) or {}
        if isinstance(lease, dict):
            pid = str(lease.get("project_id") or "").strip()
            lease_aid = str(lease.get("account_id") or "").strip()
        if lease_aid:
            from mino_nexus.resources.device_app_session_store import get_session

            row = get_session(sn, pkg) or {}
            bound = str(row.get("bound_account_id") or "").strip()
            if bound and bound != lease_aid:
                issues.append(
                    f"【资源门槛】设备绑定账号 {bound} 与租约 {lease_aid} 不一致。"
                )

    if not issues:
        issues.extend(prep_avatar_setup_gate_issues(ctx, pre))

    return issues


def preflight_device_app_gap(
    claim: dict[str, Any] | None,
    *,
    sn: str,
    package_id: str,
    ctx: Any | None = None,
) -> list[str]:
    """批跑启动时软检查；ctx 有 prep_clear_done 时收紧 clear_cache 判定。"""
    from mino_nexus.resources.case_resource_claim import preflight_device_app_gap as _base_gap

    gaps = list(_base_gap(claim, sn=sn, package_id=package_id))
    if not ctx or not claim:
        return gaps
    scene = clamp_case_scene(
        getattr(ctx, "case_scene", None) if isinstance(getattr(ctx, "case_scene", None), dict) else None
    )
    pre = ""
    case = getattr(ctx, "case", None)
    if isinstance(case, dict):
        pre = str(case.get("precondition") or "").strip()
    if claim_requires_clear_cache(claim, scene, pre) and bool(getattr(ctx, "prep_clear_done", False)):
        gaps = [g for g in gaps if "clear_cache" not in g]
    return gaps
