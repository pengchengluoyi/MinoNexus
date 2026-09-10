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
            return f"{self.rule_id}: 给出处置建议"
        state = "已恢复" if self.recovered else "未恢复"
        return f"{self.rule_id}: 执行 {len(self.actions)} 个动作，{state}"


def _yes_no(cond: Optional[bool]) -> str:
    if cond is None:
        return "unknown"
    return "yes" if cond else "no"


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


def collect_evidence(ctx, router, *, target_package: str = "") -> Evidence:
    pkg = target_package or str(getattr(ctx, "target_package", "") or "")
    event = PlanEvent(
        seq=0,
        capability_id="probe_device_state",
        event_kind="probe_device_state",
        params={"package": pkg},
        ai_reasoning="L0 取证",
        label="设备取证",
    )
    try:
        from mino_nexus.loop.web_env import frame_step

        scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
        case_seq = int(getattr(ctx, "case_seq", 0) or 0)
        result = router.dispatch(
            event,
            run_id=scout_run_id,
            step_idx=frame_step(case_seq, 2),
        )
    except Exception as exc:
        return Evidence(error=f"取证 dispatch 异常: {exc}")

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

    SLog.i(TAG, f"evidence: {ev.brief()}")
    return ev


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


def _dispatch(router, ctx, event: PlanEvent):
    if is_local_cap(event.capability_id):
        return dispatch_local(event)
    from mino_nexus.loop.web_env import agent_step_idx, frame_step

    scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
    case_seq = int(getattr(ctx, "case_seq", 0) or 0)
    turn = int(event.seq or 0)
    step_idx = agent_step_idx(case_seq, turn) if turn > 0 else frame_step(case_seq, 3)
    return router.dispatch(event, run_id=scout_run_id, step_idx=step_idx)


def apply_rule(match: RuleMatch, ctx, router, *, target_package: str = "") -> RecoveryOutcome:
    rule = match.rule
    out = RecoveryOutcome(rule_id=rule.id, mode=rule.mode)
    if rule.mode == "advise":
        out.advice = str(rule.prompt_snippet or "").strip()
        return out

    max_attempts = max(1, int(rule.max_attempts or 1))
    for attempt in range(1, max_attempts + 1):
        out.attempts = attempt
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
            res = _dispatch(router, ctx, event)
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

    return out


def recover_if_needed(
    ctx,
    router,
    *,
    target_package: str = "",
    shot: Any = None,
    execute_only: bool = False,
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
    """Case 开环：前台不是被测 App 时 launch 目标包（不处理锁屏/黑屏 advise）。"""
    try:
        from mino_nexus.runtime.run_context import is_web_slot

        if is_web_slot(str(getattr(ctx, "sn", "") or ""), str(getattr(ctx, "platform", "") or "")):
            return None
    except Exception:
        pass
    pkg = target_package or str(getattr(ctx, "target_package", "") or "")
    if not pkg:
        return None
    ev = collect_evidence(ctx, router, target_package=pkg)
    if shot is not None:
        from mino_nexus.loop.screen_capture import merge_shot_evidence

        merge_shot_evidence(ev, shot)
    if ev.app_foreground != "no" or ev.screen_blocked == "yes":
        return None
    rule = catalog.get_recovery_rule("bring_target_app_foreground")
    if rule is None:
        return None
    out = apply_rule(RuleMatch(rule=rule, reasons=["preflight_fg"]), ctx, router, target_package=pkg)
    out.evidence = ev.brief()
    return out
