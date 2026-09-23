"""跑批 Preflight：Claim 对照登记簿与 RunContext，prep 收工硬门槛。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.loop.step_contract import precondition_requires_clear_cache
from mino_nexus.runtime.session_gate import clamp_case_scene
from mino_nexus.services.case_resource_claim import (
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
    if fs and not dirty:
        return fs
    from mino_nexus.services.device_app_session_store import get_session

    row = get_session(sn, package_id)
    if row:
        return str(row.get("session") or "unknown").strip().lower()
    return fs or "unknown"


def _session_allows_required(required: str, current: str, allow: list[str]) -> bool:
    req = str(required or "").strip().lower()
    cur = str(current or "unknown").strip().lower()
    if not req or req == "any":
        return True
    if cur == req:
        return True
    if allow and cur in allow:
        return True
    if req == "logged_out":
        return cur in ("logged_out", "guest", "unknown")
    if req == "guest":
        return cur in ("guest", "logged_out", "unknown")
    return False


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
    from mino_nexus.services.device_app_session_store import get_session

    row = get_session(str(sn).strip(), str(package_id).strip()) or {}
    cur = str(row.get("session") or "unknown").strip().lower()
    if cur != "logged_in":
        return blockers
    if claim_requires_clear_cache(claim, scene_row, pre):
        return blockers
    blockers.append(
        "【拒跑】登记簿机态为 logged_in，用例要求未登录/游客，且未声明「清除应用缓存」prep，"
        "请先清缓存或调整前置/密钥。"
    )
    return blockers


def _pre_avatar_setup_lane(precondition: str) -> bool:
    return bool(re.search(r"未配置形象", str(precondition or ""), re.I))


def prep_avatar_setup_gate_issues(ctx: Any, precondition: str) -> list[str]:
    """前置「未配置形象」：禁止在未清缓存/换号前对 filled 租约空 signal_done。"""
    pre = str(precondition or "").strip()
    if not _pre_avatar_setup_lane(pre):
        return []
    if bool(getattr(ctx, "prep_clear_done", False)):
        return []
    lease = getattr(ctx, "resource_lease", None) or {}
    facets: dict[str, Any] = {}
    if isinstance(lease, dict) and isinstance(lease.get("facets"), dict):
        facets = lease["facets"]
    issues: list[str] = []
    pd = str(facets.get("profile_data") or "").strip().lower()
    if pd == "filled":
        issues.append(
            "【资源门槛】前置「未配置形象」，但租约账号 profile_data=filled；"
            "须本任务 clear_app_cache 或 lease 无资料账号，禁止空 signal_done。"
        )
    from mino_nexus.services.account_requirement_compile import PROFILE_SHAPE_FACETS

    for fk in PROFILE_SHAPE_FACETS:
        fv = str(facets.get(fk) or "").strip().lower()
        if fv in ("yes", "有", "filled"):
            issues.append(
                f"【资源门槛】前置「未配置形象」，但租约 {fk}={fv}；"
                "须清缓存或换号后再 prep 收工。"
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

    from mino_nexus.loop.session_prep_trust import prep_lease_account_signal_done_block_reason

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
        else:
            cur = effective_device_session(ctx, sn=sn, package_id=pkg)
            if cur == "logged_in":
                fact = dict(getattr(ctx, "session_fact", None) or {})
                if str(fact.get("session") or "").lower() in ("logged_out", "guest"):
                    pass
                else:
                    issues.append(
                        "【资源门槛】已清缓存但机态仍为 logged_in：请 inspect-session 确认或再次 clear_app_cache。"
                    )

    da = claim.get("device_app") if isinstance(claim, dict) and isinstance(claim.get("device_app"), dict) else {}
    req_sess = str(da.get("required_session") or scene.get("required_session") or "").strip().lower()
    allow = [str(x).strip().lower() for x in (da.get("allow") or []) if str(x).strip()]
    from mino_nexus.runtime.session_gate import session_prep_intent

    prep_intent = session_prep_intent(scene=scene)
    defer_device_session_gate = (
        req_sess == "logged_in"
        and prep_intent == "relogin"
        and not bool(getattr(ctx, "_login_flow_transition_emitted", False))
        and bool(getattr(ctx, "login_flow_macro_active", False))
    )
    skip_prep_session_registry_check = (
        req_sess == "logged_in" and prep_intent == "skip"
    )
    if (
        req_sess
        and req_sess not in ("any", "")
        and not defer_device_session_gate
        and not skip_prep_session_registry_check
    ):
        cur = effective_device_session(ctx, sn=sn, package_id=pkg)
        defer_unknown = False
        if req_sess == "logged_in" and cur == "unknown":
            if bool(getattr(ctx, "session_dirty", False)):
                defer_unknown = False
            else:
                fact = dict(getattr(ctx, "session_fact", None) or {})
                if str(fact.get("session") or "").strip().lower() == "logged_in":
                    defer_unknown = True
        if not defer_unknown and not _session_allows_required(req_sess, cur, allow):
            hint = ""
            if req_sess == "logged_in" and cur == "unknown":
                hint = "请先完成登录流块或刷新登记簿机态。"
            issues.append(
                f"【资源门槛】device_app.session 需要 {req_sess}"
                f"（允许 {allow or [req_sess]}），当前 {cur}。{hint}"
            )

    if not issues and claim_requires_clear_cache(claim, scene, pre):
        from mino_nexus.services.device_app_session_store import get_session

        row = get_session(sn, pkg) or {}
        if str(row.get("session") or "") == "logged_in" and not row.get("stale"):
            issues.append("【资源门槛】登记簿仍为 logged_in，清缓存未写入 device_app_sessions。")

    binding = str(da.get("binding") or "").strip()
    if (
        binding == "must_match_lease"
        and req_sess == "logged_in"
        and not defer_device_session_gate
        and not skip_prep_session_registry_check
    ):
        pid, lease_aid = "", ""
        lease = getattr(ctx, "resource_lease", None) or {}
        if isinstance(lease, dict):
            pid = str(lease.get("project_id") or "").strip()
            lease_aid = str(lease.get("account_id") or "").strip()
        if lease_aid:
            from mino_nexus.services.device_app_session_store import get_session

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
    from mino_nexus.services.case_resource_claim import preflight_device_app_gap as _base_gap

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
        cur = effective_device_session(ctx, sn=sn, package_id=package_id)
        if cur == "logged_in":
            gaps.append("机态仍为 logged_in（清缓存后未对齐）")
    return gaps
