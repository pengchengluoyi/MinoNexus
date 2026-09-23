"""进展熔断：状态停滞 / 界面循环 / 里程碑预算（业界 UI Agent 通用做法）。

动作级重复检测仅作辅助；主判据是「操作后界面有没有变」「是否困在少数状态里打转」、
「本阶段步数是否耗尽」。参见 WebArena / AppAgent / MobileAgent 的 no-progress early stop。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

# --- 阈值（可后续抽到 sop）---
NO_PROGRESS_THRESHOLD = 3
INPUT_REPEAT_BLOCK = 3
FUSE_BLOCK_STOP_THRESHOLD = 2
STATE_WINDOW = 10
STATE_MAX_UNIQUE = 2
STATE_MIN_SAMPLES = 6
STATE_CYCLE_MIN_LEN = 6
MILESTONE_BUDGET = {"prep": 12, "do": 10, "check": 8}
WEB_LOGIN_REPEAT_TAP = 6

_FUSE_CAPS = frozenset({
    "input_text",
    "tap_element",
    "multi_tap",
    "relogin",
    "read_device_data",
    "lease_account",
    "get_otp",
    "press_key",
    "swipe_direction",
    "long_press_element",
    "fsm_navigate",
    "accept_legal_consent",
    "dismiss_ime",
    "request_sms_code",
})

# wait_ms 计入里程碑，但不走「同屏无进展」——生成/加载本来就会在同一屏空等几轮。
_WAIT_CAPS = frozenset({"wait_ms", "wait_screen_ready"})
# 本地/数据类 cap 不改变界面指纹仍算进展（OTP/发码/填码等同屏完成登录微步）。
_FP_NEUTRAL_CAPS = frozenset({
    "get_otp",
    "read_device_data",
    "lease_account",
    "request_sms_code",
})
_WAIT_WARN_AFTER = 4

_SMS_RE = re.compile(r"验证码|短信|OTP|sms", re.I)
_TAP_BUCKET_MILLI = 50


def fuseable_cap(cap_id: str) -> bool:
    cid = str(cap_id or "").strip()
    if not cid or cid.startswith("recover_"):
        return False
    return cid in _FUSE_CAPS or cid in _WAIT_CAPS


def action_key(cap_id: str, params: dict[str, Any] | None) -> str:
    """细粒度签名（兼容旧测试）。"""
    return coarse_action_key(cap_id, params)


def coarse_action_key(cap_id: str, params: dict[str, Any] | None) -> str:
    """粗粒度动作类：坐标分桶、输入不看具体文案，防止微调规避。"""
    cid = str(cap_id or "").strip()
    p = dict(params or {})
    if cid == "input_text":
        field = str(p.get("field") or p.get("target") or "text").strip()
        return f"input|{field}"
    if cid == "tap_element":
        sel = str(p.get("selector_text") or "").strip()[:24]
        try:
            x = int(p.get("x")) // _TAP_BUCKET_MILLI * _TAP_BUCKET_MILLI
            y = int(p.get("y")) // _TAP_BUCKET_MILLI * _TAP_BUCKET_MILLI
            return f"tap|{sel}|{x},{y}"
        except (TypeError, ValueError):
            return f"tap|{sel or 'coord'}"
    if cid == "swipe_direction":
        d = str(p.get("direction") or "").strip().lower() or "?"
        return f"swipe_dir|{d}"
    return cid


def _exit_clause(*, has_hitl: bool) -> str:
    return "signal_ask_human 或 signal_give_up" if has_hitl else "signal_give_up"


def fuse_hint(
    cap_id: str,
    params: dict[str, Any] | None,
    *,
    has_get_otp: bool = False,
    leased: bool = False,
    reason: str = "stagnation",
    has_hitl: bool = False,
) -> str:
    p = dict(params or {})
    exit_to = _exit_clause(has_hitl=has_hitl)
    if reason == "milestone":
        if str(cap_id or "").strip() in _WAIT_CAPS:
            return (
                "连续等待后请停止空等：若本步达成信号已在截图上，立刻 signal_done 进入校验；"
                "若仍是操作前的页面，重做本步点击。禁止继续 wait_ms。"
            )
        return f"本阶段步数已用尽：若目标已达成请 signal_done，否则 {exit_to}。"
    if reason == "state_cycle":
        return f"界面在少数状态间循环：换路径（返回/重进登录/重启 App）或 {exit_to}。"
    if reason == "state_domination":
        return f"困在同一界面过久：确认前置是否已满足、是否走错登录分支，勿再盲试；可 {exit_to}。"
    if reason == "no_progress":
        return f"连续操作后界面无变化：换元素/策略，或 {exit_to}。"
    if cap_id == "input_text":
        field = str(p.get("field") or "")
        text = str(p.get("text") or "")
        if _SMS_RE.search(f"{field}{text}") or field in ("sms_code", "验证码"):
            if leased and has_get_otp:
                return f"已租号且在验证码流程：请 get_otp 取码，勿盲填固定码；仍失败用 {exit_to}。"
            if has_get_otp:
                return f"验证码勿重复盲填：优先 get_otp，失败再 {exit_to}。"
            return f"验证码勿重复盲填：用 {exit_to}。"
        return f"换字段/文案，或 {exit_to}。"
    if cap_id == "tap_element":
        return "换元素或先处理挡屏（返回/关弹窗），勿同点循环；可 signal_give_up。"
    if cap_id == "relogin":
        return "登录未完成：继续租号/登录流程，验证码用 get_otp，勿反复 relogin。"
    return f"换策略：{exit_to}。"


def _fps_equal(a: str, b: str) -> bool:
    aa = str(a or "").strip()
    bb = str(b or "").strip()
    if not aa and not bb:
        # Web 等场景常无 hierarchy 指纹；双空仍视为同屏无进展。
        return True
    return aa == bb and bool(aa)


def fp_neutral_cap(
    cap_id: str,
    params: dict[str, Any] | None,
    *,
    intents_done: set[str] | None = None,
    login_flow_step: bool = False,
) -> bool:
    """登录链上常同屏的操作：不计入「指纹未变」熔断，但会写入合成进展指纹。"""
    cid = str(cap_id or "").strip()
    if cid in _FP_NEUTRAL_CAPS:
        return True
    done = set(intents_done or set())
    if cid == "input_text":
        p = dict(params or {})
        field = str(p.get("field") or p.get("target") or "").strip().lower()
        text = str(p.get("text") or "")
        if login_flow_step and field in ("phone", "sms_code", "验证码", "otp", "password", "密码"):
            return True
        if field in ("sms_code", "验证码", "otp"):
            return True
        if field in ("password", "密码"):
            return "login_password" not in done
        if _SMS_RE.search(f"{field}{text}"):
            return True
        if login_flow_step and field in ("phone",) and "login_phone" not in done:
            return True
        if login_flow_step and field in ("email", "login_email") and "login_email" not in done:
            return True
    if cid == "tap_element":
        sel = str((params or {}).get("selector_text") or (params or {}).get("text") or "")
        if login_flow_step:
            # 勿用 login_entry 意图：instruction 含「点击登录」时首次 tap 就会打上该意图，导致 Log in/Email 无法中性。
            if "login_email" not in done and "login_phone" not in done:
                if re.search(r"log\s*in|sign\s*in|登录", sel, re.I):
                    return True
                if re.search(r"email|邮箱|e-mail", sel, re.I):
                    return True
                if re.search(r"手机|phone|mobile", sel, re.I):
                    return True
            if "login_email" in done and "sms_send" not in done and re.search(
                r"发送|验证码|send|code|verify|获取", sel, re.I
            ):
                return True
            if "sms_send" in done and "otp_fill" not in done:
                return True
            if "sms_send" in done and "otp_fill" in done:
                return True
        if login_flow_step and "sms_send" in done and "otp_fill" in done:
            return True
        if re.search(r"登录|立即登录", sel):
            if "sms_send" in done and ("otp_fill" in done or "input_fill" in done):
                return True
    return False


def _detect_state_cycle(fps: list[str]) -> Optional[tuple[int, list[str]]]:
    """检测末尾是否出现 period=2..4 的状态循环（至少重复 3 轮）。"""
    seq = [str(x) for x in fps if str(x).strip()]
    if len(seq) < STATE_CYCLE_MIN_LEN:
        return None
    for period in (2, 3, 4):
        need = period * 3
        tail = seq[-need:]
        if len(tail) < need:
            continue
        pat = tail[:period]
        if all(tail[i : i + period] == pat for i in range(0, need, period)):
            return period, pat
    return None


def _detect_action_pattern_cycle(keys: list[str]) -> bool:
    """粗动作序列末尾是否呈 AAABBB 式或 ABCABC 式循环（防「换一点规避」）。"""
    seq = [
        str(x)
        for x in keys
        if str(x).strip() and str(x) not in _WAIT_CAPS
    ]
    if len(seq) < 6:
        return False
    tail = seq[-9:]
    uniq = len(set(tail))
    if uniq > 4:
        return False
    for period in (2, 3):
        need = period * 3
        if len(tail) < need:
            continue
        chunk = tail[-need:]
        pat = chunk[:period]
        if all(chunk[i : i + period] == pat for i in range(0, need, period)):
            return True
    counts = Counter(tail)
    if len(tail) >= 6 and len(counts) <= 2:
        top2 = sum(v for _, v in counts.most_common(2))
        if top2 >= len(tail) - 1:
            return True
    return False


class ProgressGate:
    """状态进展熔断器 + 分级干预（block → steer → stop）。"""

    def __init__(self, *, profile: str = "case") -> None:
        self.profile = str(profile or "case").strip().lower() or "case"
        self.no_progress_streak: int = 0
        self.fuse_block_streak: int = 0
        self.total_fuse_blocks: int = 0
        self._post_states: list[str] = []
        self._coarse_actions: list[str] = []
        self._web_coarse_actions: list[str] = []
        self._last_web_focus_fp: str = ""
        self._milestone: str = ""
        self.milestone_turns: int = 0
        self.warning_hint: str = ""
        self.last_intervention: str = ""
        self._milestone_exhausted: bool = False

    def reset_milestone(self, phase: str, step: int = 0) -> None:
        key = f"{phase}:{step}"
        if key == self._milestone:
            return
        self._milestone = key
        self.milestone_turns = 0
        self.no_progress_streak = 0
        self.fuse_block_streak = 0
        self.total_fuse_blocks = 0
        self.warning_hint = ""
        self.last_intervention = ""
        self._milestone_exhausted = False
        self._post_states = []
        self._coarse_actions = []
        self._web_coarse_actions = []
        self._last_web_focus_fp = ""

    def record_fuse_block(self, reason: str) -> Optional[str]:
        """连续 block 后升级 stop，避免「熔断本身」形成空转循环。"""
        text = str(reason or "")
        if "【熔断" not in text:
            self.fuse_block_streak = 0
            return None
        self.fuse_block_streak += 1
        self.total_fuse_blocks += 1
        if "【熔断·里程碑】" in text:
            self._milestone_exhausted = True
        self.last_intervention = "block"
        n = self.fuse_block_streak
        if n >= 1:
            self.warning_hint = (
                f"【熔断·强制转向】已连续 {n} 次同类操作被拒绝；"
                f"禁止再 swipe/tap/input，必须 signal_give_up（勿再盲点）。"
            )
        if n >= FUSE_BLOCK_STOP_THRESHOLD or self.total_fuse_blocks >= 6:
            self.last_intervention = "stop"
            return (
                f"连续 {n} 次熔断拦截后仍重复尝试，判定陷入死循环。"
                f"请 signal_give_up 结束本步。最近：{text[:160]}"
            )
        return None

    def check(
        self,
        *,
        phase: str,
        cap_id: str,
        params: dict[str, Any] | None,
        screen_fp: str,
        has_get_otp: bool = False,
        leased: bool = False,
        has_hitl: bool = False,
        profile_shape_completion: bool = False,
        intents_done: set[str] | None = None,
        login_flow_step: bool = False,
        web_channel: bool = False,
    ) -> Optional[str]:
        if not fuseable_cap(cap_id):
            return None

        kw = dict(has_get_otp=has_get_otp, leased=leased, has_hitl=has_hitl)
        exit_to = _exit_clause(has_hitl=has_hitl)
        done = set(intents_done or set())

        if not web_channel and fp_neutral_cap(
            cap_id,
            params,
            intents_done=done,
            login_flow_step=login_flow_step,
        ):
            return None

        if self.fuse_block_streak >= FUSE_BLOCK_STOP_THRESHOLD:
            return (
                f"【熔断·强制停止】已连续 {self.fuse_block_streak} 次被拦截，"
                f"禁止再尝试 {cap_id}；必须 signal_give_up。"
            )
        if self.total_fuse_blocks >= 6:
            return (
                f"【熔断·强制停止】本阶段已累计 {self.total_fuse_blocks} 次熔断，"
                f"必须 signal_give_up。"
            )

        explore = self.profile == "explore"
        budget = 80 if explore else int(MILESTONE_BUDGET.get(str(phase or "do"), 10))
        if self._milestone_exhausted:
            if cap_id in ("signal_done", "signal_give_up", "wait_ms"):
                return None
            return (
                "【熔断·里程碑】本阶段步数预算已用尽，禁止再下发设备 mutate。"
                "若目标已达成请 signal_done，否则 signal_give_up。"
            )
        if self.milestone_turns >= budget:
            self._milestone_exhausted = True
            hint = fuse_hint(cap_id, params, reason="milestone", **kw)
            return f"【熔断·里程碑】{phase} 阶段已用 {self.milestone_turns} 步仍未收工。{hint}"

        waiting = cap_id in _WAIT_CAPS
        if waiting:
            if self.milestone_turns >= _WAIT_WARN_AFTER:
                self.warning_hint = (
                    f"【进展告警】已连续 wait_ms {self.milestone_turns} 次。"
                    "若本步达成信号已在截图上，立刻 signal_done 进入校验，不要继续空等。"
                )
            else:
                self.warning_hint = ""
            return None

        if web_channel and cap_id == "tap_element":
            from mino_nexus.loop.web_progress import web_tap_coarse_key

            wk = web_tap_coarse_key(cap_id, params)
            tail_w = self._web_coarse_actions[-WEB_LOGIN_REPEAT_TAP:]
            if len(tail_w) >= WEB_LOGIN_REPEAT_TAP - 1 and all(
                x == wk for x in tail_w[-(WEB_LOGIN_REPEAT_TAP - 1) :]
            ):
                hint = fuse_hint(cap_id, params, reason="no_progress", **kw)
                return (
                    f"【熔断·Web 重复点击】同一选择器已连续点击 {len(tail_w)} 次；"
                    f"请换元素、先聚焦输入框再 input_text，或 signal_give_up。{hint}"
                )

        states = self._post_states
        if web_channel:
            states = []
        if not explore and len(states) >= STATE_MIN_SAMPLES:
            recent = states[-STATE_WINDOW:]
            uniq = len(set(recent))
            if uniq <= STATE_MAX_UNIQUE:
                skip_dom = False
                if profile_shape_completion and cap_id == "tap_element":
                    lab = str((params or {}).get("selector_text") or (params or {}).get("text") or "")
                    if re.search(r"完成|保存", lab):
                        skip_dom = True
                if not skip_dom:
                    hint = fuse_hint(cap_id, params, reason="state_domination", **kw)
                    return (
                        f"【熔断·困局】近 {len(recent)} 步仅在 {uniq} 个界面状态间打转（非实质进展）。"
                        f"{hint}"
                    )

        if not explore and not web_channel:
            cycle = _detect_state_cycle(states)
            if cycle is not None:
                period, _pat = cycle
                if profile_shape_completion and cap_id == "tap_element":
                    lab = str((params or {}).get("selector_text") or (params or {}).get("text") or "")
                    if re.search(r"完成|保存", lab):
                        cycle = None
                if cycle is not None:
                    period, _pat = cycle
                    hint = fuse_hint(cap_id, params, reason="state_cycle", **kw)
                    return (
                        f"【熔断·状态循环】界面在 {period} 个状态间循环重复。{hint}"
                    )

        if not web_channel and _detect_action_pattern_cycle(self._coarse_actions):
            skip_pat = False
            if profile_shape_completion and cap_id == "tap_element":
                lab = str((params or {}).get("selector_text") or (params or {}).get("text") or "")
                if re.search(r"完成|保存", lab):
                    skip_pat = True
            if not skip_pat:
                hint = fuse_hint(cap_id, params, reason="state_domination", **kw)
                return f"【熔断·动作模式】近几步动作类型反复组合仍无进展。{hint}"

        coarse_now = coarse_action_key(cap_id, params)
        if (
            not web_channel
            and login_flow_step
            and cap_id == "tap_element"
            and len(self._coarse_actions) >= 5
        ):
            tail_tap = [x for x in self._coarse_actions[-6:] if str(x).startswith("tap|")]
            if len(tail_tap) >= 5 and len(set(tail_tap[-5:])) == 1:
                hint = fuse_hint(cap_id, params, reason="no_progress", **kw)
                return (
                    f"【熔断·重复点击】登录流程已连续 5 次同位置/同文案 tap 仍无进展。"
                    f"{hint}"
                )
        if coarse_now.startswith("input|") and len(self._coarse_actions) >= INPUT_REPEAT_BLOCK:
            if not login_flow_step:
                tail = self._coarse_actions[-INPUT_REPEAT_BLOCK:]
                if len(tail) == INPUT_REPEAT_BLOCK and all(x == coarse_now for x in tail):
                    hint = fuse_hint(
                        cap_id,
                        params,
                        reason="no_progress",
                        **kw,
                    )
                    return (
                        f"【熔断·重复输入】同字段已连续输入 {INPUT_REPEAT_BLOCK} 次仍无实质进展。"
                        f"{hint}"
                    )
            elif "login_flow" not in done and not (
                {"sms_send", "otp_fill", "login_phone"}.issubset(done)
            ):
                tail = self._coarse_actions[-INPUT_REPEAT_BLOCK:]
                if len(tail) == INPUT_REPEAT_BLOCK and all(x == coarse_now for x in tail):
                    hint = fuse_hint(cap_id, params, reason="no_progress", **kw)
                    return (
                        f"【熔断·重复输入】同字段已连续输入 {INPUT_REPEAT_BLOCK} 次仍无实质进展。"
                        f"{hint}"
                    )

        # 连续 N 次操作后界面指纹未变 → 第 N+1 次前熔断（Web 不用 DOM/截图指纹，见 web_focus）
        if not web_channel and self.no_progress_streak >= NO_PROGRESS_THRESHOLD - 1:
            if not fp_neutral_cap(
                cap_id,
                params,
                intents_done=done,
                login_flow_step=login_flow_step,
            ):
                hint = fuse_hint(cap_id, params, reason="no_progress", **kw)
                return (
                    f"【熔断·无进展】连续 {self.no_progress_streak} 次操作后界面指纹未变。"
                    f"{hint}"
                )

        if web_channel:
            self.warning_hint = ""
            return None

        if self.no_progress_streak >= NO_PROGRESS_THRESHOLD - 2:
            self.warning_hint = (
                f"【进展告警】已连续 {self.no_progress_streak} 次操作后界面无变化；"
                f"下一步若仍无进展将熔断，请换策略或 {exit_to}。"
            )
        else:
            self.warning_hint = ""

        return None

    def record_pass(
        self,
        *,
        cap_id: str,
        params: dict[str, Any] | None,
        pre_fp: str,
        post_fp: str,
        intents_done: set[str] | None = None,
        login_flow_step: bool = False,
        web_channel: bool = False,
        web_focus_fp: str = "",
    ) -> None:
        if not fuseable_cap(cap_id):
            return
        self.milestone_turns += 1
        pre = str(pre_fp or "").strip() or "_"
        post = str(post_fp or "").strip() or "_"
        wff = str(web_focus_fp or "").strip()
        if web_channel:
            from mino_nexus.loop.web_progress import web_tap_coarse_key

            if wff and wff != self._last_web_focus_fp:
                self.fuse_block_streak = 0
                self._last_web_focus_fp = wff
            self._post_states.append(wff or post)
            if len(self._post_states) > 32:
                self._post_states = self._post_states[-32:]
            wk = web_tap_coarse_key(cap_id, params)
            self._web_coarse_actions.append(wk)
            if len(self._web_coarse_actions) > 24:
                self._web_coarse_actions = self._web_coarse_actions[-24:]
            self._coarse_actions.append(coarse_action_key(cap_id, params))
            if len(self._coarse_actions) > 24:
                self._coarse_actions = self._coarse_actions[-24:]
            return
        neutral = fp_neutral_cap(
            cap_id,
            params,
            intents_done=intents_done,
            login_flow_step=login_flow_step,
        )
        if neutral:
            self.no_progress_streak = 0
            if _fps_equal(pre, post):
                coarse = coarse_action_key(cap_id, params)
                post = f"{post}#neutral:{coarse}"
            if not self.warning_hint.startswith("【熔断·强制转向】"):
                self.warning_hint = ""
        elif _fps_equal(pre, post):
            self.no_progress_streak += 1
        else:
            self.no_progress_streak = 0
            self.fuse_block_streak = 0
            if not self.warning_hint.startswith("【熔断·强制转向】"):
                self.warning_hint = ""
        self._post_states.append(post)
        if len(self._post_states) > 32:
            self._post_states = self._post_states[-32:]
        coarse = coarse_action_key(cap_id, params)
        self._coarse_actions.append(coarse)
        if len(self._coarse_actions) > 24:
            self._coarse_actions = self._coarse_actions[-24:]


# 兼容旧名
ActionFuse = ProgressGate
FUSE_REPEAT_THRESHOLD = NO_PROGRESS_THRESHOLD


__all__ = [
    "ActionFuse",
    "ProgressGate",
    "FUSE_REPEAT_THRESHOLD",
    "NO_PROGRESS_THRESHOLD",
    "FUSE_BLOCK_STOP_THRESHOLD",
    "action_key",
    "coarse_action_key",
    "fuse_hint",
    "fuseable_cap",
    "fp_neutral_cap",
]
