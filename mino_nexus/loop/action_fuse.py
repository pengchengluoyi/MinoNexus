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
FUSE_BLOCK_STOP_THRESHOLD = 3
STATE_WINDOW = 10
STATE_MAX_UNIQUE = 2
STATE_MIN_SAMPLES = 6
STATE_CYCLE_MIN_LEN = 6
MILESTONE_BUDGET = {"prep": 12, "do": 10, "check": 8}

_FUSE_CAPS = frozenset({
    "input_text",
    "tap_element",
    "multi_tap",
    "relogin",
    "read_device_data",
    "lease_account",
    "press_key",
    "swipe_direction",
    "long_press_element",
})

_SMS_RE = re.compile(r"验证码|短信|OTP|sms", re.I)
_TAP_BUCKET_MILLI = 50


def fuseable_cap(cap_id: str) -> bool:
    cid = str(cap_id or "").strip()
    if not cid or cid.startswith("recover_"):
        return False
    return cid in _FUSE_CAPS


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
    return cid


def fuse_hint(
    cap_id: str,
    params: dict[str, Any] | None,
    *,
    has_get_otp: bool = False,
    leased: bool = False,
    reason: str = "stagnation",
) -> str:
    p = dict(params or {})
    if reason == "milestone":
        return "本阶段步数已用尽：若目标已达成请 signal_done，否则 signal_ask_human 或 signal_give_up。"
    if reason == "state_cycle":
        return "界面在少数状态间循环：换路径（返回/重进登录/重启 App）或 signal_ask_human。"
    if reason == "state_domination":
        return "困在同一界面过久：确认前置是否已满足、是否走错登录分支，勿再盲试；可 signal_ask_human。"
    if reason == "no_progress":
        return "连续操作后界面无变化：换元素/策略，或 signal_ask_human / signal_give_up。"
    if cap_id == "input_text":
        field = str(p.get("field") or "")
        text = str(p.get("text") or "")
        if _SMS_RE.search(f"{field}{text}") or field in ("sms_code", "验证码"):
            if leased and has_get_otp:
                return "已租号且在验证码流程：请 get_otp 取码，勿盲填固定码；仍失败用 signal_ask_human。"
            if has_get_otp:
                return "验证码勿重复盲填：优先 get_otp，失败再 signal_ask_human。"
            return "验证码勿重复盲填：用 signal_ask_human 取码。"
        return "换字段/文案，或 signal_ask_human / signal_give_up。"
    if cap_id == "tap_element":
        return "换元素或先处理挡屏（返回/关弹窗），勿同点循环；可 signal_give_up。"
    if cap_id == "relogin":
        return "登录未完成：继续租号/登录流程，验证码用 get_otp，勿反复 relogin。"
    return "换策略：signal_ask_human 或 signal_give_up。"


def _fps_equal(a: str, b: str) -> bool:
    return str(a or "").strip() == str(b or "").strip() and bool(str(a or "").strip())


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
    seq = [str(x) for x in keys if str(x).strip()]
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

    def __init__(self) -> None:
        self.no_progress_streak: int = 0
        self.fuse_block_streak: int = 0
        self._post_states: list[str] = []
        self._coarse_actions: list[str] = []
        self._milestone: str = ""
        self.milestone_turns: int = 0
        self.warning_hint: str = ""
        self.last_intervention: str = ""

    def reset_milestone(self, phase: str, step: int = 0) -> None:
        key = f"{phase}:{step}"
        if key == self._milestone:
            return
        self._milestone = key
        self.milestone_turns = 0
        self.no_progress_streak = 0
        self.fuse_block_streak = 0
        self.warning_hint = ""
        self.last_intervention = ""

    def record_fuse_block(self, reason: str) -> Optional[str]:
        """连续 block 后升级 stop，避免「熔断本身」形成空转循环。"""
        text = str(reason or "")
        if "【熔断" not in text:
            self.fuse_block_streak = 0
            return None
        self.fuse_block_streak += 1
        self.last_intervention = "block"
        n = self.fuse_block_streak
        if n >= 2:
            self.warning_hint = (
                f"【熔断·强制转向】已连续 {n} 次同类操作被拒绝；"
                f"禁止再 swipe/tap/input，必须 signal_ask_human 或 signal_give_up。"
            )
        if n >= FUSE_BLOCK_STOP_THRESHOLD:
            self.last_intervention = "stop"
            return (
                f"连续 {n} 次熔断拦截后仍重复尝试，判定陷入死循环。"
                f"请人工介入。最近：{text[:160]}"
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
    ) -> Optional[str]:
        if not fuseable_cap(cap_id):
            return None

        budget = int(MILESTONE_BUDGET.get(str(phase or "do"), 10))
        if self.milestone_turns >= budget:
            hint = fuse_hint(cap_id, params, has_get_otp=has_get_otp, leased=leased, reason="milestone")
            return f"【熔断·里程碑】{phase} 阶段已用 {self.milestone_turns} 步仍未收工。{hint}"

        pre_fp = str(screen_fp or "").strip() or "_"

        states = self._post_states
        if len(states) >= STATE_MIN_SAMPLES:
            recent = states[-STATE_WINDOW:]
            uniq = len(set(recent))
            if uniq <= STATE_MAX_UNIQUE:
                hint = fuse_hint(cap_id, params, has_get_otp=has_get_otp, leased=leased, reason="state_domination")
                return (
                    f"【熔断·困局】近 {len(recent)} 步仅在 {uniq} 个界面状态间打转（非实质进展）。"
                    f"{hint}"
                )

        cycle = _detect_state_cycle(states)
        if cycle is not None:
            period, _pat = cycle
            hint = fuse_hint(cap_id, params, has_get_otp=has_get_otp, leased=leased, reason="state_cycle")
            return (
                f"【熔断·状态循环】界面在 {period} 个状态间循环重复。{hint}"
            )

        if _detect_action_pattern_cycle(self._coarse_actions):
            hint = fuse_hint(cap_id, params, has_get_otp=has_get_otp, leased=leased, reason="state_domination")
            return f"【熔断·动作模式】近几步动作类型反复组合仍无进展。{hint}"

        # 连续 N 次操作后界面指纹未变 → 第 N+1 次前熔断（先于动作级规则）
        if self.no_progress_streak >= NO_PROGRESS_THRESHOLD - 1:
            hint = fuse_hint(cap_id, params, has_get_otp=has_get_otp, leased=leased, reason="no_progress")
            return (
                f"【熔断·无进展】连续 {self.no_progress_streak} 次操作后界面指纹未变。"
                f"{hint}"
            )

        if self.no_progress_streak >= NO_PROGRESS_THRESHOLD - 2:
            self.warning_hint = (
                f"【进展告警】已连续 {self.no_progress_streak} 次操作后界面无变化；"
                f"下一步若仍无进展将熔断，请换策略或 signal_ask_human。"
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
    ) -> None:
        if not fuseable_cap(cap_id):
            return
        self.milestone_turns += 1
        pre = str(pre_fp or "").strip() or "_"
        post = str(post_fp or "").strip() or "_"
        if _fps_equal(pre, post):
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
]
