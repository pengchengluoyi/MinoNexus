"""恢复规则：Agent 调用 recover_<id> 时按 actions 执行。规则不会自己跑。"""
from __future__ import annotations

import re
import time as _t
from dataclasses import dataclass, field
from typing import Any, Optional

from mino_nexus.catalog import registry as catalog
from mino_nexus.core.log import SLog
from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import PlanEvent

TAG = "Recovery"

_FOREGROUND_RE = re.compile(r"(?:topResumedActivity|mResumedActivity)=\S+\s+\S+\s+([\w.]+)/")


def _trace_recovery(ctx: Any, event_type: str, payload: dict[str, Any]) -> None:
    """只落匹配事实/动作结果，不写 params、设备凭证或原始 dumpsys。"""
    from mino_nexus.records.eventlog.writer import active_writer

    writer = active_writer()
    run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
    if writer is not None and writer.session_id == run_id:
        writer.append(event_type, payload)


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
    execution_metadata: dict[str, Any] = field(default_factory=dict)
    executor_used: str = ""
    stop_run: bool = False

    @property
    def raw_response(self) -> dict[str, Any]:
        return self.execution_metadata

    def summary(self) -> str:
        if self.stop_run:
            return "恢复动作已停止，原写动作未结算；设备与账号资源仍隔离"
        if self.mode == "advise":
            if self.advice:
                return f"{self.rule_id}: {self.advice}"
            return f"{self.rule_id}: 给出处置建议"
        state = "已恢复" if self.recovered else "未恢复"
        return f"{self.rule_id}: 执行 {len(self.actions)} 个动作，{state}"


def _write_stop(ctx) -> RecoveryOutcome | None:
    if getattr(ctx, "recovery_write_stopped", False):
        return RecoveryOutcome(mode="execute", stop_run=True, error="run_write_stopped",
            execution_metadata={"execution_control": {"stop_run": True,
                "resource_isolated": True, "reason_code": "run_write_stopped"}})
    return None


def _note_write_stop(ctx, out: RecoveryOutcome, result) -> bool:
    from mino_nexus.loop.observe.execution_diagnostics import execution_result_metadata
    raw = result.raw_response if isinstance(getattr(result, "raw_response", None), dict) else {}
    control = raw.get("execution_control") if isinstance(raw.get("execution_control"), dict) else {}
    if raw.get("resource_isolated") is True or control.get("stop_run") is True or control.get("resource_isolated") is True:
        out.stop_run = True
        out.recovered = False
        out.error = str(control.get("reason_code") or "run_write_stopped")
        out.execution_metadata = execution_result_metadata(result)
        out.execution_metadata.setdefault("execution_control", {}).update(stop_run=True, resource_isolated=True)
        ctx.recovery_write_stopped = True
        _trace_recovery(ctx, "recovery/stopped", {"rule_id": out.rule_id,
            "reason_code": out.error, **out.execution_metadata})
        return True
    return False


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
    # 当前取证优先；上回合 ctx/hierarchy 仅补未知，不能覆盖刚读到的前台。
    if ev.app_foreground in ("yes", "no"):
        return
    nodes = getattr(ctx, "nav_hierarchy_nodes", None)
    if not isinstance(nodes, list) or not nodes:
        return
    from mino_nexus.navigation.atlas.nav_capture_store import infer_screen_package

    inf = infer_screen_package(
        nodes,
        target_package=pkg,
        platform=str(getattr(ctx, "platform", "") or ""),
    )
    fpkg = str(inf.get("foreground_package") or inf.get("foreground_id") or "").strip()
    kind = str(inf.get("screen_kind") or "").strip()
    from mino_nexus.navigation.atlas.nav_target_scope import _looks_like_url, web_page_matches_target

    if fpkg and not _looks_like_android_pkg(fpkg) and not _looks_like_url(fpkg):
        fpkg = ""
    if kind == "system_dialog":
        # 系统弹窗挡屏：既不是「异应用」（不该判死/触发拉起），也不能算「应用在前台」。
        if fpkg:
            ev.foreground_pkg = fpkg
            ev.top_window_pkg = fpkg
        return
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
            ev.app_foreground = _yes_no(fpkg == pkg)
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
    diagnostic_purpose: str = "recovery_evidence",
) -> None:
    if ev.foreground_pkg and ev.app_foreground in ("yes", "no"):
        return
    pkg = str(target_package or "").strip()
    # 同一拍内的前台结论复用：guard / 取证 / 启动核验会在一回合里各问一次，
    # 每次都是一个 Scout 往返（cr-a390541e5dd2 平均每条 20.7 次，占总设备动作 68%）。
    # 任何界面写入动作都会在 RouterProxy.dispatch 里清掉这份缓存。
    cache = getattr(ctx, "_fg_probe_cache", None)
    if isinstance(cache, dict) and (not pkg or not cache.get("pkg")
                                    or str(cache.get("pkg")) == pkg):
        # 取证、guard、启动核验在一回合里会对同一个事实各问一次（实测 4 次/回合）。
        # 失效靠 RouterProxy.dispatch：任何界面写入动作都会把这份缓存清掉，
        # 所以窗口用到 20 秒也不会拿旧结论顶替刚点过的界面。
        fresh = (_t.time() - float(cache.get("stamp") or 0)) <= 20.0
        if fresh:
            ev.app_foreground = str(cache.get("app_fg") or "unknown")
            ev.foreground_pkg = str(cache.get("fg_pkg") or "")
            ev.top_window_pkg = str(cache.get("top_pkg") or "")
            ev.raw = {"package": str(cache.get("fg_pkg") or ""), "cached": True}
            return
    event = PlanEvent(
        seq=0,
        capability_id="get_foreground_app",
        event_kind="get_foreground_app",
        params={"package": pkg},
        ai_reasoning="L0 取证 fallback",
        label="读前台包名",
    )
    try:
        result = _dispatch(router, ctx, event, agent_turn=0, action_idx=0, diagnostic_purpose=diagnostic_purpose)
    except Exception:
        return
    raw = _payload_dict(result)
    if getattr(result.status, "value", result.status) != "pass":
        ev.error = result.error or result.summary
        return
    if str(getattr(ctx, "platform", "")) in {"desktop", "mac", "win"}:
        ev.raw = raw
        matched = raw.get("target_active")
        foreground = str(raw.get("foreground_package") or "").strip()
        if matched is True and pkg:
            ev.app_foreground = "yes"
            ev.foreground_pkg = pkg
            ev.top_window_pkg = pkg
            ev.target_alive = "yes"
        elif matched is False and foreground:
            ev.app_foreground = "no"
            ev.foreground_pkg = foreground
            ev.top_window_pkg = foreground
        return
    fg_pkg = str(raw.get("package") or "").strip()
    if not _looks_like_android_pkg(fg_pkg):
        return
    ev.foreground_pkg = fg_pkg
    ev.top_window_pkg = fg_pkg
    from mino_nexus.navigation.atlas.nav_target_scope import is_system_dialog_pkg

    if is_system_dialog_pkg(fg_pkg):
        # 系统弹窗在前台时 app_foreground 只能是 unknown：算 no 会触发
        # bring_target_app_foreground 去反复重启应用（cr-9f7f478af02c 的 214 连拉两次）。
        ev.app_foreground = "unknown"
        try:
            ctx._fg_probe_cache = {"pkg": pkg, "stamp": _t.time(), "app_fg": "unknown",
                "fg_pkg": ev.foreground_pkg, "top_pkg": ev.top_window_pkg,
                "turn": getattr(ctx, "turn", None)}
        except Exception:  # noqa: BLE001
            pass
        return
    if pkg:
        ev.app_foreground = _yes_no(fg_pkg == pkg)
    try:
        ctx._fg_probe_cache = {"pkg": pkg, "stamp": _t.time(), "app_fg": ev.app_foreground,
            "fg_pkg": ev.foreground_pkg, "top_pkg": ev.top_window_pkg,
            "turn": getattr(ctx, "turn", None)}
    except Exception:  # noqa: BLE001
        pass


_EVIDENCE_TTL_SEC = 1.5


def invalidate_evidence_cache(ctx) -> None:
    """设备状态可能被动作改变：丢弃本 run 缓存的取证。"""
    try:
        ctx._evidence_cache = None
    except Exception:  # noqa: BLE001
        pass


def collect_evidence(ctx, router, *, target_package: str = "") -> Evidence:
    """短 TTL 去重：同一回合内多处（显示守卫/恢复/中断检测）重复取证只打一次设备。

    动作执行后由 agent_loop 调 invalidate_evidence_cache；取证失败的结果不缓存。
    """
    import copy
    import time as _t

    hit = getattr(ctx, "_evidence_cache", None)
    key = target_package or str(getattr(ctx, "target_package", "") or "")
    if isinstance(hit, tuple) and len(hit) == 3 and hit[1] == key and (_t.time() - hit[0]) < _EVIDENCE_TTL_SEC:
        return copy.deepcopy(hit[2])
    ev = _collect_evidence_uncached(ctx, router, target_package=target_package)
    if not getattr(ev, "error", ""):
        try:
            ctx._evidence_cache = (_t.time(), key, copy.deepcopy(ev))
        except Exception:  # noqa: BLE001
            pass
    return ev


def _collect_evidence_uncached(ctx, router, *, target_package: str = "") -> Evidence:
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
    if str(getattr(ctx, "platform", "")) in {"desktop", "mac", "win"}:
        ev = Evidence()
        _fill_foreground_from_get_app(ev, router, ctx, target_package=pkg)
        return ev
    event = PlanEvent(
        seq=0,
        capability_id="probe_device_state",
        event_kind="probe_device_state",
        params={"package": pkg},
        ai_reasoning="L0 取证",
        label="设备取证",
    )
    try:
        result = _dispatch(router, ctx, event, diagnostic_purpose="recovery_evidence")
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
    if showing is None:
        showing = keyguard.get("isStatusBarKeyguard") or keyguard.get("mShowingLockscreen")
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

    if router is not None:
        _fill_foreground_from_get_app(ev, router, ctx, target_package=pkg)
    _enrich_evidence_from_run_context(ev, ctx, target_package=pkg)
    _trace_recovery(ctx, "recovery/evidence", {
        **ev.as_match_dict(), "foreground_package": ev.foreground_pkg,
        "probe_failed": bool(ev.error),
    })
    SLog.i(TAG, f"evidence: {ev.brief()}")
    return ev


def resolve_app_foreground_guard(
    ctx,
    router,
    *,
    nodes: list[Any] | None,
    target_package: str = "",
) -> dict[str, Any]:
    """hierarchy / DOM guard + 原生前台查询，减少 app_foreground=unknown。"""
    pkg = str(target_package or getattr(ctx, "target_package", "") or "").strip()
    plat = str(getattr(ctx, "platform", "") or "")
    from mino_nexus.runtime.run_context import is_web_slot
    from mino_nexus.navigation.atlas.nav_capture_store import run_guard_foreground

    fg = run_guard_foreground(
        list(nodes or []),
        target_package=pkg,
        platform=plat,
    )
    af = str(fg.get("app_foreground") or "").strip().lower()
    if is_web_slot(str(getattr(ctx, "sn", "") or ""), plat):
        ctx.app_foreground = str(fg.get("app_foreground") or "")
        return fg
    if af != "unknown" or router is None or not pkg:
        ctx.app_foreground = str(fg.get("app_foreground") or "")
        return fg
    ev = Evidence()
    _fill_foreground_from_get_app(ev, router, ctx, target_package=pkg, diagnostic_purpose="foreground_guard")
    probed = str(ev.app_foreground or "").strip().lower()
    if probed in ("yes", "no"):
        fg = {**fg, "app_foreground": probed, "foreground_package": ev.foreground_pkg,
              "foreground_source": "get_foreground_app", "hierarchy_app_foreground": af}
    ctx.app_foreground = str(fg.get("app_foreground") or "")
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
    diagnostic_purpose: str = "recovery_action",
):
    from mino_nexus.loop.web.web_env import recovery_operation_step_idx

    scout_run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
    step_idx = recovery_operation_step_idx()
    res = router.dispatch(event, run_id=scout_run_id, step_idx=step_idx, ctx=ctx, diagnostic_purpose=diagnostic_purpose)
    # 恢复动作里也会有 launch_app（例如 clear 后回桌面）。成功即算「应用已启动」，
    # 否则紧接着模型自己再派一次 launch_app 时，skip_repeat_launch_app 看不到事实，
    # 会把同一个动作做两遍（cr-7cdcfaa93757 seq74 + seq77）。
    try:
        cap = str(getattr(event, "capability_id", "") or "")
        st = str(getattr(getattr(res, "status", None), "value", getattr(res, "status", "")) or "")
        if cap in ("launch_app", "open_app", "open_url") and st == "pass":
            ctx.app_launch_confirmed = True
        elif cap in ("close_app", "kill_app", "clear_app_cache") and st == "pass":
            # 重启块 = close_app + launch_app；关了之后必须允许再启动。
            ctx.app_launch_confirmed = False
    except Exception:  # noqa: BLE001
        pass
    return res


def _rule_actions(rule, ctx) -> list[Any]:
    """恢复规则的动作步骤来自逻辑块（nav_flow_block_catalog）。

    L0 规则只保留「什么时候触发」和「怎么验证」；「具体做什么」统一由逻辑块声明，
    避免规则里再写一份动作、和块里的步骤各说各话。块缺失时退回规则自带动作并留警告，
    不让调度因为少一条块而彻底走不通。
    """
    from mino_nexus.catalog.flow_block_seed import RECOVERY_RULE_BLOCKS

    block_id = RECOVERY_RULE_BLOCKS.get(str(getattr(rule, "id", "") or ""))
    if not block_id:
        return list(rule.actions)
    app_id = str(getattr(ctx, "app_id", "") or "")
    try:
        from mino_nexus.navigation.atlas.nav_flow_block_catalog import resolve_effective_steps

        steps = resolve_effective_steps(app_id=app_id, block_id=block_id) or []
    except Exception as exc:  # noqa: BLE001
        SLog.w(TAG, f"恢复块 {block_id} 解析失败：{exc!r}；退回规则动作")
        return list(rule.actions)
    from mino_nexus.catalog.models import RecoveryAction

    actions: list[Any] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        cap = str(step.get("hook_cap") or step.get("cap") or "").strip()
        if not cap:
            continue
        params = dict(step.get("params") or {})
        actions.append(RecoveryAction(capability=cap, params=params))
    if not actions:
        SLog.w(TAG, f"恢复块 {block_id} 没有可执行步骤；退回规则动作")
        return list(rule.actions)
    return actions


def apply_rule(
    match: RuleMatch,
    ctx,
    router,
    *,
    target_package: str = "",
    agent_turn: int = 0,
) -> RecoveryOutcome:
    from mino_nexus.loop.screen_capture import merge_shot_evidence
    stopped = _write_stop(ctx)
    if stopped is not None:
        return stopped
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
                    f"请按当前导航计划使用 tap_element / press_key，勿再调用 recover_{rule.id}。"
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
        out.error = ""
        _trace_recovery(ctx, "recovery/attempt", {"rule_id": rule.id, "attempt": attempt})
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
        for idx, action in enumerate(_rule_actions(rule, ctx), 1):
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
            # 恢复块里的 launch_app 也要去重：同一 prep 里 bring_target_app_foreground
            # 会连续触发多次，每条都重发一次启动，屏幕被反复拉回首屏/首启弹窗
            # （cr-a390541e5dd2 的 api-1-211 一次 prep 起了 3 次）。
            if action.capability in ("launch_app", "open_app") and bool(getattr(ctx, "app_launch_confirmed", False)):
                out.actions.append({"capability": action.capability, "skipped": "app_already_launched"})
                continue
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
            raw_reason = ""
            raw_resp = getattr(res, "raw_response", None)
            if isinstance(raw_resp, dict):
                raw_reason = str(raw_resp.get("local_reason") or "")
            _trace_recovery(ctx, "recovery/action", {
                "rule_id": rule.id, "capability": action.capability, "status": str(status),
                "summary": str(res.summary or "")[:240], "reason": raw_reason,
            })
            out.actions.append({
                "capability": action.capability,
                "status": str(status),
                "summary": res.summary,
            })
            out.applied = True
            if _note_write_stop(ctx, out, res):
                return out
            if res.status not in (EventStatus.PASS, EventStatus.SKIPPED):
                out.error = res.error or f"{action.capability} 执行失败"
                if raw_reason == "capability_unregistered" or "能力目录里没有" in str(res.summary or ""):
                    out.error = f"capability_unregistered: {action.capability}"

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
        out.evidence = ev.brief()
        _trace_recovery(ctx, "recovery/verify", {
            "rule_id": rule.id, "attempt": attempt, "recovered": ok,
            **ev.as_match_dict(),
        })
        if ok:
            out.recovered = True
            out.error = ""
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
    stopped = _write_stop(ctx)
    if stopped is not None:
        return stopped
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
    _trace_recovery(ctx, "recovery/match", {
        **ev.as_match_dict(), "matched_rules": [h.rule_id for h in hits],
        "selected_rule": hits[0].rule_id if hits else "",
        "mode": str(hits[0].rule.mode) if hits else "", "execute_only": execute_only,
    })
    if not hits:
        return None
    out = apply_rule(hits[0], ctx, router, target_package=target_package)
    if not out.evidence:
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
    stopped = _write_stop(ctx)
    if stopped is not None:
        return stopped
    from mino_nexus.contract.execution.ui import UiChannel, ui_channel_from_ctx

    if ui_channel_from_ctx(ctx) == UiChannel.DESKTOP:
        return None
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
    from mino_nexus.navigation.atlas.nav_target_scope import _looks_like_url, web_page_matches_target

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
    from mino_nexus.loop.observe.execution_diagnostics import execution_result_metadata
    out = RecoveryOutcome(rule_id="bring_target_app_foreground", mode="execute",
                          executor_used=res.executor_used, execution_metadata=execution_result_metadata(res))
    out.applied = True
    if _note_write_stop(ctx, out, res):
        return out
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
