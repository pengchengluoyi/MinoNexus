"""前置程序计划：与 catalog prep_flow 对齐的可执行步骤（编译期就要算）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx
from mino_nexus.resources.case_resource_key_catalog import catalog_payload
from mino_nexus.resources.case_resource_claim import _case_resource_key
from mino_nexus.resources.resource_preflight import _claim_from_case, claim_requires_clear_cache
from mino_nexus.resources.session.ensure import account_need_from_case


def resolve_prep_claim(case: dict[str, Any] | None, ctx: Any) -> dict[str, Any]:
    if isinstance(case, dict):
        rk = _case_resource_key(case)
        if rk:
            return dict(rk)
        pre = str(case.get("precondition") or case.get("precondition_raw") or "").strip()
        if pre:
            from mino_nexus.resources.case_resource_claim import ensure_resource_key_on_case

            pkg = str(getattr(ctx, "target_package", "") or getattr(ctx, "package_id", "") or "")
            env = str(getattr(ctx, "env", "") or "test")
            return dict(ensure_resource_key_on_case(case, package=pkg, env=env) or {})
    return dict(_claim_from_case(case or {}) or {})


def _launch_hook_cap(ctx: Any, claim: dict[str, Any]) -> str:
    return "launch_app"


_EMPTY_PRE_VALUE = frozenset({"", "无", "没有", "暂无", "不需要", "无要求", "任意", "any", "none"})


def _pre_lines(pre: str) -> list[tuple[str, str]]:
    from mino_nexus.resources.case_resource_claim import _parse_precondition_lines

    return _parse_precondition_lines(pre)


def _session_want(val: str) -> str:
    from mino_nexus.resources.case_resource_claim import _required_session_from_value

    want = _required_session_from_value(val)
    return want if want in ("logged_in", "guest") else ""


def _device_session_clause(pre: str) -> tuple[str, str]:
    """设备/应用登录态。返回 (logged_in|guest, 展示文案)。"""
    from mino_nexus.resources.accounts.requirement_compile import (
        TITLE_DEVICE_LOGIN,
        classify_precondition_title,
    )

    for title, val in _pre_lines(pre):
        if classify_precondition_title(title) != TITLE_DEVICE_LOGIN:
            continue
        if str(val or "").strip().lower() in _EMPTY_PRE_VALUE:
            continue
        want = _session_want(val)
        if not want:
            continue
        label = f"{title}：{val}".strip("：") if title else str(val).strip()
        return want, label[:240]
    return "", ""


def _account_clause(pre: str, case: dict[str, Any] | None, scene: dict[str, Any] | None) -> str:
    """账号登录态与账号与数据。文案保留字段名和状态。"""
    from mino_nexus.resources.accounts.requirement_compile import (
        TITLE_ACCOUNT_DATA,
        TITLE_ACCOUNT_LOGIN,
        classify_precondition_title,
    )

    bits: list[str] = []
    for title, val in _pre_lines(pre):
        kind = classify_precondition_title(title)
        if kind not in (TITLE_ACCOUNT_DATA, TITLE_ACCOUNT_LOGIN):
            continue
        text = str(val or "").strip()
        if text.lower() in _EMPTY_PRE_VALUE:
            continue
        bits.append(f"{title}：{text}".strip("：") if title else text)
    if bits:
        return "；".join(bits)[:240]
    if _claim_needs_lease(case, scene):
        return "筛选账号"
    return ""


def build_prep_program_plan(
    *,
    claim: dict[str, Any],
    scene: dict[str, Any] | None,
    precondition: str,
    ctx: Any,
    case: dict[str, Any] | None,
) -> dict[str, Any]:
    """与 catalog prep_flow 对齐的可执行步骤列表（程序真源，非 LLM 发明）。"""
    flow = (catalog_payload().get("prep_flow") or {}).get("steps") or []
    steps_out: list[dict[str, Any]] = []
    need_lease = bool(_account_clause(precondition, case, scene))
    device_want, device_label = _device_session_clause(precondition)
    if not device_want and scene and scene.get("required_session") in {"logged_in", "guest"}:
        device_want = scene["required_session"]
        device_label = "确认编译前置要求的应用登录态"
    need_clear = claim_requires_clear_cache(claim, scene, precondition)
    # 2026-10-22：安卓非 root 没有跨应用的登录态 API，`pm clear` 是唯一确定性的未登录重置。
    # 前置要求未登录时，清数据本身就是准备动作，不再等用例显式写「清缓存」。
    if device_want == "guest" and str(getattr(ctx, "platform", "") or "").strip().lower() == "android":
        need_clear = True
    env_label = _env_clear_clause(precondition) if need_clear else ""
    launch_cap = _launch_hook_cap(ctx, claim)

    for row in flow:
        if not isinstance(row, dict):
            continue
        fid = str(row.get("id") or "").strip()
        label = str(row.get("label") or fid).strip()
        if fid == "pick_account" and not need_lease:
            continue
        if fid == "pick_account":
            steps_out.append(
                {
                    "id": "prep_lease_account",
                    "prep_flow_id": fid,
                    "title": _account_clause(precondition, case, scene) or label or "筛选账号",
                    "kind": "hook",
                    "hook_cap": "lease_account",
                    "optional": False,
                }
            )
        elif fid == "pick_device":
            # 设备渠道由 Nexus 按用例标签/平台派单，不进 LLM 里程碑。
            continue
        elif fid == "env_cleanup":
            clear_step = {
                "id": "prep_clear_cache",
                "prep_flow_id": fid,
                "title": env_label or label or "清除应用数据与缓存",
                "kind": "hook",
                "hook_cap": "clear_app_cache",
                "optional": False,
            }
            if not need_clear:
                clear_step["status"] = "skipped"
                clear_step["skip_reason"] = "not_required"
            steps_out.append(clear_step)

    if not any(str(step.get("id") or "") == "prep_clear_cache" for step in steps_out):
        clear_step = {
            "id": "prep_clear_cache",
            "prep_flow_id": "env_cleanup",
            "title": env_label or "清除应用数据与缓存",
            "kind": "hook",
            "hook_cap": "clear_app_cache",
            "optional": False,
        }
        if not need_clear:
            clear_step["status"] = "skipped"
            clear_step["skip_reason"] = "not_required"
        steps_out.append(clear_step)

    steps_out.append(
        {
            "id": "prep_launch_app",
            "prep_flow_id": "launch",
            "title": "打开被测应用",
            "kind": "hook",
            "hook_cap": launch_cap,
            "optional": False,
        }
    )
    # 首启协议弹窗：block id 由用例编译期给出（fb.global.privacy.accept / .decline），
    # 运行期不做意图判断；未声明就不插，避免在正常首页上乱点“同意”。
    _scene = getattr(ctx, "case_scene", None) or {}
    consent_block = str((_scene or {}).get("consent_block_id") or "").strip()
    if consent_block and str(getattr(ctx, "platform", "") or "").strip().lower() == "android":
        steps_out.append(
            {
                "id": "prep_consent",
                "prep_flow_id": "consent",
                "title": "处理首启用户协议弹窗",
                "kind": "block",
                "block_id": consent_block,
                "app_id": str(getattr(ctx, "app_id", "") or ""),
                "optional": False,
            }
        )
    if device_want == "guest" and str(getattr(ctx, "platform", "") or "").strip().lower() != "android":
        # web / mac / win 没有 pm clear 这条确定性重置，走应用自己的退出登录链（fb.global.logout）。
        steps_out.append(
            {
                "id": "prep_logout",
                "prep_flow_id": "logout",
                "title": "退出登录到未登录态",
                "kind": "block",
                "block_id": "fb.global.logout",
                "app_id": str(getattr(ctx, "app_id", "") or ""),
                "optional": False,
            }
        )
    if device_want:
        steps_out.append(
            {
                "id": "prep_device_session",
                "prep_flow_id": "device_session",
                "title": device_label or "确认应用登录态",
                "kind": "hook",
                "hook_cap": "confirm_login_state",
                "optional": False,
                "required_session": device_want,
            }
        )

    used_caps = {
        str(step.get("hook_cap") or "")
        for step in steps_out
        if isinstance(step, dict) and str(step.get("hook_cap") or "").strip()
    }
    for extra in _extra_prep_catalog_steps(precondition, used_caps):
        steps_out.append(extra)

    da = claim.get("device_app") if isinstance(claim.get("device_app"), dict) else {}
    for step in steps_out:
        if isinstance(step, dict) and not step.get("ui_status"):
            step["ui_status"] = "pending"
    return {
        "schema": "mino.prep_program_plan.v1",
        "steps": steps_out,
        "spans": [],
        "flags": {
            "need_lease": need_lease,
            "need_clear_cache": need_clear,
            "need_device_session": bool(device_want),
            "device_session": device_want,
            "launch_cap": launch_cap,
        },
        "claim_digest": {
            "device_app_session": str(da.get("required_session") or ""),
            "platform": str(claim.get("platform") or ""),
        },
    }


def _extra_prep_catalog_steps(pre: str, used_caps: set[str]) -> list[dict[str, Any]]:
    """编号前置行里目录能认出、固定模板又没收的事件，按 hook 追加。"""
    from mino_nexus.catalog.skill_channel import capability_dispatch_ok, canonical_cap
    from mino_nexus.catalog.registry import get_capability
    from mino_nexus.resources.accounts.requirement_compile import TITLE_OTHER, classify_precondition_title
    from mino_nexus.resources.case_resource_key_catalog import catalog_payload

    out: list[dict[str, Any]] = []
    known = {str(x) for x in (used_caps or set())}
    entries = [
        e
        for e in (catalog_payload().get("entries") or [])
        if isinstance(e, dict) and str(e.get("key_layer") or "") == "precondition"
    ]
    for title, val in _pre_lines(pre):
        if classify_precondition_title(title) != TITLE_OTHER:
            continue
        text = str(val or "").strip()
        if text.lower() in _EMPTY_PRE_VALUE:
            continue
        blob = f"{title}{text}"
        hit: dict[str, Any] | None = None
        best = 0
        for ent in entries:
            cat = str(ent.get("write_category") or "").strip()
            examples = [str(x).strip() for x in (ent.get("write_examples") or []) if str(x).strip()]
            score = 0
            if cat and cat in blob:
                score = max(score, len(cat))
            for ex in examples:
                if ex in blob or blob in ex:
                    score = max(score, len(ex))
            if score > best:
                best = score
                hit = ent
        if not hit or best < 2:
            continue
        caps = [str(x).strip() for x in (hit.get("config_keys") or []) if str(x).strip()]
        # config_keys also contains resource fields such as session/profile.
        # Only registered executable events may become extra prep hooks;
        # resource claims retain their own preflight and session-gate checks.
        cap = next((canonical_cap(c) for c in caps
                    if get_capability(canonical_cap(c)) is not None
                    and capability_dispatch_ok(c)), "")
        if not cap or cap in known:
            continue
        known.add(cap)
        label = f"{title}：{text}".strip("：") if title else text
        out.append(
            {
                "id": f"prep_extra_{cap}"[:64],
                "prep_flow_id": "catalog",
                "title": label[:240],
                "kind": "hook",
                "hook_cap": cap,
                "optional": False,
                "key_ref": str(hit.get("key_ref") or ""),
                "source_clause": label[:240],
            }
        )
    return out


def _env_clear_clause(pre: str) -> str:
    from mino_nexus.resources.accounts.requirement_compile import (
        TITLE_ENV_PERM,
        classify_precondition_title,
    )

    for title, val in _pre_lines(pre):
        if classify_precondition_title(title) != TITLE_ENV_PERM:
            continue
        text = str(val or "").strip()
        if not text or text.lower() in _EMPTY_PRE_VALUE:
            continue
        label = f"{title}：{text}".strip("：") if title else text
        return label[:240]
    return ""


def _claim_needs_lease(case: dict[str, Any] | None, scene: dict[str, Any] | None) -> bool:
    """与 ensure_case_account 近似：仅有设备/环境前置时不种 lease 里程碑。"""
    need = account_need_from_case(case or {}, scene if isinstance(scene, dict) else {})
    req = dict(need.get("requirements") or {})
    if need.get("need_account") or req.get("all") or req.get("prefer"):
        return True
    if str(need.get("session") or "").strip():
        return True
    if need.get("profile") or need.get("address"):
        return True
    if str(need.get("template_id") or "").strip():
        return True
    claim = resolve_prep_claim(case, None) if isinstance(case, dict) else {}
    acc = claim.get("account") if isinstance(claim.get("account"), dict) else {}
    ar = acc.get("requirements") if isinstance(acc.get("requirements"), dict) else {}
    return bool(ar.get("all") or ar.get("prefer"))
