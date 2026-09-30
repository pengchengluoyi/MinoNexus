"""恢复规则：Agent 调用 recover_<id> 时按 actions 执行。规则不会自己跑。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from mino_nexus.catalog import registry as catalog
from mino_nexus.core.log import SLog
from mino_nexus.loop.local_executors import dispatch_local, is_local_cap
from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import PlanEvent

TAG = "Recovery"

_FOREGROUND_RE = re.compile(r"(?:topResumedActivity|mResumedActivity)=\S+\s+\S+\s+([\w.]+)/")


@dataclass
class Evidence:
    awake: str = "unknown"
    locked: str = "unknown"
    foreground_pkg: str = ""
    target_alive: str = "unknown"
    anr: str = "unknown"
    ime_shown: str = "unknown"
    top_window_pkg: str = ""
    screen_blocked: str = "unknown"
    app_foreground: str = "unknown"
    capture_ok: str = "unknown"
    capture_black: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_match_dict(self) -> dict[str, str]:
        return {
            "awake": self.awake,
            "locked": self.locked,
            "target_alive": self.target_alive,
            "anr": self.anr,
            "ime_shown": self.ime_shown,
            "screen_blocked": self.screen_blocked,
            "app_foreground": self.app_foreground,
            "capture_ok": self.capture_ok,
            "capture_black": self.capture_black,
        }

    def brief(self) -> str:
        return (
            f"awake={self.awake} locked={self.locked} fg={self.foreground_pkg or '-'} "
            f"blocked={self.screen_blocked} capture_ok={self.capture_ok} "
            f"capture_black={self.capture_black}"
        )


@dataclass
class RuleMatch:
    rule: Any
    reasons: list[str] = field(default_factory=list)

    @property
    def rule_id(self) -> str:
        return str(getattr(self.rule, "id", "") or "")


@dataclass
class RecoveryOutcome:
    rule_id: str = ""
    mode: str = ""
    applied: bool = False
    recovered: bool = False
    attempts: int = 0
    actions: list[dict] = field(default_factory=list)
    advice: str = ""
    error: str = ""
    evidence: str = ""

    def summary(self) -> str:
        if self.mode == "advise":
            if self.advice:
                return f"{self.rule_id}: {self.advice}"
            return f"{self.rule_id}: 给出处置建议"
        state = "已恢复" if self.recovered else "未恢复"
        return f"{self.rule_id}: 执行 {len(self.actions)} 个动作，{state}"


def _yes_no(cond: Optional[bool]) -> str:
    if cond is None:
        return "unknown"
    return "yes" if cond else "no"


def _looks_like_android_pkg(name: str) -> bool:
    s = str(name or "").strip()
    if not s or s in ("android", "system"):
        return False
    return "." in s and len(s) >= 5


def _payload_dict(result: Any) -> dict[str, Any]:
    """Scout RESULT：raw_response / extra / data 里都可能带着 low_level。"""
    raw = dict(getattr(result, "raw_response", None) or {})
    extra = raw.get("extra")
    if isinstance(extra, dict):
        raw = {**extra, **raw}
    data = raw.get("data")
    if isinstance(data, dict):
        raw = {**data, **raw}
    low = raw.get("low_level")
    if isinstance(low, dict):
        return low
    return raw


def _enrich_evidence_from_run_context(
    ev: Evidence,
    ctx: Any,
    *,
    target_package: str = "",
) -> None:
    """probe_device_state 在部分机型上 dumpsys/grep 为空时，用 hierarchy / 本 turn 前台结论补全。"""
    pkg = str(target_package or getattr(ctx, "target_package", "") or "").strip()
    menu_fg = str(getattr(ctx, "app_foreground", "") or "").strip().lower()
    if menu_fg == "yes" and pkg:
        ev.app_foreground = "yes"
        if not ev.foreground_pkg:
            ev.foreground_pkg = pkg
            ev.top_window_pkg = pkg
    elif menu_fg == "no":
        ev.app_foreground = "no"
    if ev.foreground_pkg and ev.app_foreground != "unknown":
        return
    nodes = getattr(ctx, "nav_hierarchy_nodes", None)
    if not isinstance(nodes, list) or not nodes:
        return
    from mino_nexus.services.nav_capture_store import infer_screen_package

    inf = infer_screen_package(
        nodes,
        target_package=pkg,
        platform=str(getattr(ctx, "platform", "") or ""),
    )
    fpkg = str(inf.get("foreground_package") or inf.get("foreground_id") or "").strip()
    kind = str(inf.get("screen_kind") or "").strip()
    from mino_nexus.services.nav_target_scope import _looks_like_url, web_page_matches_target

    if fpkg and not _looks_like_android_pkg(fpkg) and not _looks_like_url(fpkg):
        fpkg = ""
    if kind in ("launcher", "foreign"):
        ev.app_foreground = "no"
        if fpkg:
            ev.foreground_pkg = fpkg
            ev.top_window_pkg = fpkg
        return
    if pkg and fpkg:
        ev.foreground_pkg = fpkg
        ev.top_window_pkg = fpkg
        if _looks_like_url(pkg) or _looks_like_url(fpkg):
            ev.app_foreground = _yes_no(web_page_matches_target(pkg, fpkg))
        else:
            ev.app_foreground = _yes_no(fpkg == pkg or pkg in fpkg)
    elif kind == "app" and pkg and fpkg == pkg:
        ev.app_foreground = "yes"
        ev.foreground_pkg = pkg
        ev.top_window_pkg = pkg


def _fill_foreground_from_get_app(
    ev: Evidence,
    router: Any,
    ctx: Any,
    *,
    target_package: str,
) -> None:
    if ev.foreground_pkg and ev.app_foreground in ("yes", "no"):
        return
    pkg = str(target_package or "").strip()
    event = PlanEvent(
        seq=0,
        capability_id="get_foreground_app",
        event_kind="get_foreground_app",
        params={"package": pkg},
        ai_reasoning="L0 取证 fallback",
        label="读前台包名",
    )
    try:
        result = _dispatch(router, ctx, event, agent_turn=0, action_idx=0)
    except Exception:
        return
    raw = _payload_dict(result)
    fg_pkg = str(raw.get("package") or "").strip()
    if not _looks_like_android_pkg(fg_pkg):
        return
    ev.foreground_pkg = fg_pkg
    ev.top_window_pkg = fg_pkg
    if pkg:
        ev.app_foreground = _yes_no(fg_pkg == pkg)


def collect_evidence(ctx, router, *, target_package: str = "") -> Evidence:
    pkg = target_package or str(getattr(ctx, "target_package", "") or "")
    try:
        from mino_nexus.runtime.run_context import is_web_slot

        if is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
            ev = Evidence()
            _enrich_evidence_from_run_context(ev, ctx, target_package=pkg)
            if not ev.foreground_pkg and pkg:
                ev.foreground_pkg = pkg
                ev.top_window_pkg = pkg
            if str(ev.app_foreground or "").strip().lower() in ("", "unknown") and pkg:
                if bool(getattr(ctx, "app_launch_confirmed", False)):
                    ev.app_foreground = "yes"
            SLog.i(TAG, f"evidence(web): {ev.brief()}")
            return ev
    except Exception:
        pass
    event = PlanEvent(
        seq=0,
        capability_id="probe_device_state",
        event_kind="probe_device_state",
        params={"package": pkg},
        ai_reasoning="L0 取证",
        label="设备取证",
    )
    try:
        from mino_nexus.loop.web.web_env import frame_step

        scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
        case_seq = int(getattr(ctx, "case_seq", 0) or 0)
        result = router.dispatch(
            event,
            run_id=scout_run_id,
            step_idx=frame_step(case_seq, 2),
        )
    except Exception as exc:
        ev = Evidence(error=f"取证 dispatch 异常: {exc}")
        _enrich_evidence_from_run_context(ev, ctx, target_package=pkg)
        return ev

    low = _payload_dict(result)
    ev = Evidence(raw=low)
    status = getattr(result, "status", None)
    ok = status in (EventStatus.PASS,) or str(getattr(status, "value", status) or "") == "pass"
    if not ok:
        ev.error = getattr(result, "error", "") or "取证失败"

    power = low.get("power") if isinstance(low.get("power"), dict) else {}
    keyguard = low.get("keyguard") if isinstance(low.get("keyguard"), dict) else {}
    ime = low.get("ime") if isinstance(low.get("ime"), dict) else {}

    wake = str(power.get("mWakefulness") or "")
    if wake:
        ev.awake = _yes_no(wake.lower() == "awake")

    showing = keyguard.get("isKeyguardShowing")
    if showing is None:
        showing = keyguard.get("mDreamingLockscreen")
    if showing is not None:
        ev.locked = _yes_no(str(showing).lower() == "true")

    fg = low.get("foreground")
    if isinstance(fg, list) and fg:
        m = _FOREGROUND_RE.search(" ".join(str(x) for x in fg))
        if m:
            ev.foreground_pkg = m.group(1)
    if ev.foreground_pkg:
        ev.top_window_pkg = ev.foreground_pkg
        if pkg:
            ev.app_foreground = _yes_no(ev.foreground_pkg == pkg)

    pid = low.get("target_pid")
    if isinstance(pid, str):
        ev.target_alive = _yes_no(bool(pid.strip()))

    anr = low.get("anr_window")
    if isinstance(anr, str) and anr.strip().isdigit():
        ev.anr = _yes_no(int(anr.strip()) > 0)

    shown = ime.get("mInputShown")
    if shown is not None:
        ev.ime_shown = _yes_no(str(shown).lower() == "true")

    if ev.awake == "no" or ev.locked == "yes":
        ev.screen_blocked = "yes"
    elif ev.awake == "yes" and ev.locked == "no":
        ev.screen_blocked = "no"

    _enrich_evidence_from_run_context(ev, ctx, target_package=pkg)
    if router is not None:
        _fill_foreground_from_get_app(ev, router, ctx, target_package=pkg)
    SLog.i(TAG, f"evidence: {ev.brief()}")
    return ev


def resolve_app_foreground_guard(
    ctx,
    router,
    *,
    nodes: list[Any] | None,
    target_package: str = "",
) -> dict[str, str]:
    """hierarchy / DOM guard +（仅安卓）get_foreground_app，减少 app_foreground=unknown。"""
    pkg = str(target_package or getattr(ctx, "target_package", "") or "").strip()
    plat = str(getattr(ctx, "platform", "") or "")
    from mino_nexus.runtime.run_context import is_web_slot
    from mino_nexus.services.nav_capture_store import run_guard_foreground

    launch_ok = bool(getattr(ctx, "app_launch_confirmed", False))
    fg = run_guard_foreground(
        list(nodes or []),
        target_package=pkg,
        platform=plat,
        launch_confirmed=launch_ok,
    )
    af = str(fg.get("app_foreground") or "").strip().lower()
    if is_web_slot(str(getattr(ctx, "sn", "") or ""), plat):
        if af == "unknown" and launch_ok and pkg:
            fg = {**fg, "app_foreground": "yes", "screen_kind": "app"}
        setattr(ctx, "app_foreground", str(fg.get("app_foreground") or ""))
        return fg
    if af != "unknown" or router is None or not pkg:
        setattr(ctx, "app_foreground", str(fg.get("app_foreground") or ""))
        return fg
    ev = Evidence()
    _fill_foreground_from_get_app(ev, router, ctx, target_package=pkg)
    probed = str(ev.app_foreground or "").strip().lower()
    if probed in ("yes", "no"):
        fg = {**fg, "app_foreground": probed}
    setattr(ctx, "app_foreground", str(fg.get("app_foreground") or ""))
    return fg


def _match_conditions(match, evidence: Evidence, screen_texts: list[str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    facts = evidence.as_match_dict()
    cond = match or None
    evid = dict(getattr(cond, "evidence", None) or {})
    branches = list(getattr(cond, "evidence_any", None) or [])
    prefixes = list(getattr(cond, "top_window_pkg_prefix", None) or [])
    texts = list(getattr(cond, "screen_text_any", None) or [])

    if evid:
        for key, want in evid.items():
            got = facts.get(key, "unknown")
            if got != str(want):
                return False, []
            reasons.append(f"{key}={got}")
    elif branches:
        hit_branch = False
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            ok = all(facts.get(str(k), "unknown") == str(v) for k, v in branch.items())
            if ok:
                hit_branch = True
                reasons.extend(f"{k}={facts.get(str(k), 'unknown')}" for k in branch)
                break
        if not hit_branch:
            return False, []
    elif not (prefixes or texts):
        return False, []

    if prefixes:
        pkg = evidence.top_window_pkg or ""
        if not any(pkg.startswith(p) for p in prefixes):
            return False, []
        reasons.append(f"top_window={pkg}")

    if texts:
        blob = " ".join(screen_texts)
        hit = next((t for t in texts if t and t in blob), None)
        if hit is None:
            return False, []
        reasons.append(f"screen_text~{hit}")

    return True, reasons


def match_rules(
    evidence: Evidence,
    screen_texts: Optional[list[str]] = None,
    *,
    platform: str = "",
) -> list[RuleMatch]:
    plat = str(platform or "android").strip().lower() or "android"
    hits: list[RuleMatch] = []
    for rule in catalog.list_recovery_rules(enabled_only=True):
        plats = [str(p).lower() for p in (getattr(rule, "platforms", None) or [])]
        if plats and plat not in plats:
            continue
        ok, reasons = _match_conditions(rule.match, evidence, screen_texts or [])
        if ok:
            hits.append(RuleMatch(rule=rule, reasons=reasons))
    if hits:
        SLog.i(TAG, f"matched rules: {[(h.rule_id, h.reasons) for h in hits]}")
    return hits


def _forbidden(rule, action) -> str:
    banned = [t for t in (getattr(getattr(rule, "forbid", None), "text_any", None) or []) if t]
    if not banned:
        return ""
    probe = " ".join(str(v) for v in (getattr(action, "target", None) or {}).values())
    probe += " " + " ".join(str(v) for v in (getattr(action, "params", None) or {}).values())
    for b in banned:
        if b and b in probe:
            return b
    return ""


def _dispatch(
    router,
    ctx,
    event: PlanEvent,
    *,
    agent_turn: int = 0,
    action_idx: int = 0,
):
    if is_local_cap(event.capability_id):
        return dispatch_local(event, ctx=ctx, router=router)
    from mino_nexus.loop.web.web_env import frame_step, recovery_action_step_idx

    scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
    case_seq = int(getattr(ctx, "case_seq", 0) or 0)
    if action_idx > 0:
        step_idx = recovery_action_step_idx(case_seq, agent_turn, action_idx)
    else:
        step_idx = frame_step(case_seq, 3)
    return router.dispatch(event, run_id=scout_run_id, step_idx=step_idx)


def apply_rule(
    match: RuleMatch,
    ctx,
    router,
    *,
    target_package: str = "",
    agent_turn: int = 0,
) -> RecoveryOutcome:
    rule = match.rule
    out = RecoveryOutcome(rule_id=rule.id, mode=rule.mode)
    if rule.mode == "advise":
        out.advice = str(rule.prompt_snippet or "").strip()
        if router is not None:
            ev = collect_evidence(ctx, router, target_package=target_package)
            if hasattr(router, "observe"):
                try:
                    from mino_nexus.loop.screen_capture import merge_shot_evidence

                    merge_shot_evidence(ev, router.observe("screenshot", force_fresh=False))
                except Exception:
                    pass
            out.evidence = ev.brief()
            facts = ev.as_match_dict()
            mismatch = False
            evid = dict(getattr(rule.match, "evidence", None) or {})
            if evid:
                for key, want in evid.items():
                    got = facts.get(str(key), "unknown")
                    if got not in ("unknown", str(want)) and got != str(want):
                        mismatch = True
                        break
            if mismatch:
                out.error = "evidence_mismatch"
                out.advice = (
                    f"当前证据不匹配本恢复（{ev.brief()}）。"
                    f"请改用 tap_element / press_key / fsm_navigate，勿再调用 recover_{rule.id}。"
                )
                return out
        if not out.advice:
            out.advice = (
                f"{rule.id}：按规则提示处理当前异常，不要重复调用本恢复。"
            )
        return out

    max_attempts = max(1, int(rule.max_attempts or 1))
    verify = rule.verify
    if rule.id == "bring_target_app_foreground":
        ev_pre = collect_evidence(ctx, router, target_package=target_package)
        if ev_pre.app_foreground == "yes":
            out.recovered = True
            out.evidence = ev_pre.brief()
            return out
    for attempt in range(1, max_attempts + 1):
        out.attempts = attempt
        from mino_nexus.loop.system_dialog_recovery import (
            is_system_permission_rule,
            try_unified_system_permission_recovery,
        )

        if is_system_permission_rule(rule.id) and try_unified_system_permission_recovery(
            match,
            ctx=ctx,
            router=router,
            target_package=target_package,
            agent_turn=agent_turn,
            rule=rule,
            out=out,
        ):
            return out
        for idx, action in enumerate(rule.actions, 1):
            banned = _forbidden(rule, action)
            if banned:
                out.actions.append({"capability": action.capability, "skipped": f"forbid:{banned}"})
                continue
            params = dict(action.params or {})
            if action.target:
                params["target"] = dict(action.target)
            if action.fallback_xy and len(action.fallback_xy) == 2:
                params.setdefault("x", int(action.fallback_xy[0]))
                params.setdefault("y", int(action.fallback_xy[1]))
            if action.capability in ("launch_app", "close_app") and target_package:
                params.setdefault("package", target_package)
            event = PlanEvent(
                seq=idx,
                capability_id=action.capability,
                event_kind=action.capability,
                params=params,
                ai_reasoning=f"L0 恢复 {rule.id}",
                label=rule.title or rule.id,
            )
            res = _dispatch(router, ctx, event, agent_turn=agent_turn, action_idx=idx)
            status = getattr(res.status, "value", res.status)
            out.actions.append({
                "capability": action.capability,
                "status": str(status),
                "summary": res.summary,
            })
            out.applied = True
            if res.status not in (EventStatus.PASS, EventStatus.SKIPPED):
                out.error = res.error or f"{action.capability} 执行失败"

        verify = rule.verify
        if not (verify.evidence or verify.evidence_any or verify.screen_text_any or verify.top_window_pkg_prefix):
            out.recovered = not out.error
            return out
        ev = collect_evidence(ctx, router, target_package=target_package)
        if router is not None and hasattr(router, "observe"):
            verify_ev = dict(getattr(verify, "evidence", None) or {})
            branches = list(getattr(verify, "evidence_any", None) or [])
            needs_capture = any(k.startswith("capture_") for k in verify_ev)
            if not needs_capture:
                for branch in branches:
                    if isinstance(branch, dict) and any(str(k).startswith("capture_") for k in branch):
                        needs_capture = True
                        break
            if needs_capture:
                from mino_nexus.loop.screen_capture import merge_shot_evidence

                merge_shot_evidence(ev, router.observe("screenshot", force_fresh=True))
        ok, _ = _match_conditions(verify, ev, [])
        if ok:
            out.recovered = True
            SLog.i(TAG, f"[{rule.id}] 恢复成功（第 {attempt} 次）：{ev.brief()}")
            return out
        SLog.w(TAG, f"[{rule.id}] 第 {attempt}/{max_attempts} 次后仍未通过 verify：{ev.brief()}")
        out.evidence = ev.brief()
        if not out.error:
            out.error = f"verify 未通过：{ev.brief()}"

    if not out.recovered and out.evidence and out.error and out.evidence not in out.error:
        out.error = f"{out.error}; {out.evidence}"
    return out


def recover_if_needed(
    ctx,
    router,
    *,
    target_package: str = "",
    shot: Any = None,
    execute_only: bool = False,
    exclude_rule_ids: frozenset[str] | None = None,
) -> Optional[RecoveryOutcome]:
    """取证 → 可选合并截图信号 → 匹配 → 执行第一条命中规则。"""
    try:
        from mino_nexus.runtime.run_context import is_web_slot

        if is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
            if shot is None:
                return None
    except Exception:
        pass
    ev = collect_evidence(ctx, router, target_package=target_package)
    if shot is not None:
        from mino_nexus.loop.screen_capture import merge_shot_evidence

        merge_shot_evidence(ev, shot)
    plat = str(getattr(ctx, "platform", "") or "")
    hits = match_rules(ev, platform=plat)
    if execute_only:
        hits = [h for h in hits if str(getattr(h.rule, "mode", "") or "") != "advise"]
    from mino_nexus.loop.launch_grace import filter_foreground_recovery_hits

    hits = filter_foreground_recovery_hits(ctx, hits)
    if exclude_rule_ids:
        hits = [h for h in hits if h.rule_id not in exclude_rule_ids]
    if not hits:
        return None
    out = apply_rule(hits[0], ctx, router, target_package=target_package)
    out.evidence = ev.brief()
    return out


def ensure_target_app_foreground(
    ctx,
    router,
    *,
    target_package: str = "",
    shot: Any = None,
) -> Optional[RecoveryOutcome]:
    """Case 开环：前台不是被测 App 时 launch 目标包/网址（不处理锁屏/黑屏 advise）。"""
    pkg = target_package or str(getattr(ctx, "target_package", "") or "")
    web_slot = False
    try:
        from mino_nexus.runtime.run_context import is_web_slot

        web_slot = is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or ""))
    except Exception:
        web_slot = False
    if not pkg:
        return None
    ev = collect_evidence(ctx, router, target_package=pkg)
    if shot is not None:
        from mino_nexus.loop.screen_capture import merge_shot_evidence

        merge_shot_evidence(ev, shot)
    if ev.app_foreground == "yes":
        return None
    if ev.screen_blocked == "yes":
        return None
    from mino_nexus.loop.launch_grace import in_launch_grace

    if in_launch_grace(ctx):
        return None
    from mino_nexus.services.nav_target_scope import _looks_like_url, web_page_matches_target

    if web_slot and pkg and _looks_like_url(pkg):
        away = ev.app_foreground == "no" or (
            bool(ev.foreground_pkg) and not web_page_matches_target(pkg, ev.foreground_pkg)
        )
    else:
        away = ev.app_foreground == "no" or (
            bool(ev.foreground_pkg) and bool(pkg) and ev.foreground_pkg != pkg
        )
    if not away:
        return None
    launch_params = {"url": pkg} if web_slot and _looks_like_url(pkg) else {"package": pkg}
    event = PlanEvent(
        seq=0,
        capability_id="launch_app",
        event_kind="launch_app",
        params=launch_params,
        ai_reasoning="开环前台不是被测目标，程序 launch_app",
        label="程序启动被测目标",
    )
    res = _dispatch(router, ctx, event, agent_turn=0, action_idx=1)
    status = getattr(res.status, "value", res.status)
    ok = str(status) in (EventStatus.PASS.value, "pass")
    out = RecoveryOutcome(rule_id="bring_target_app_foreground", mode="execute")
    out.applied = True
    out.recovered = ok
    out.attempts = 1
    out.actions = [
        {
            "capability": "launch_app",
            "status": str(status),
            "summary": res.summary,
        }
    ]
    if not ok:
        out.error = res.error or "launch_app 执行失败"
    out.evidence = ev.brief()
    return out
