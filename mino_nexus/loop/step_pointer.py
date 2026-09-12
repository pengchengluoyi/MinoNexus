"""用例步骤指针：prep → do → check。不含原文关键字分流。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Optional

from mino_nexus.ai.case_text import parse_numbered_items_rules
from mino_nexus.loop.action_fuse import ProgressGate
from mino_nexus.services.run_store import spec_lines

_POINTER_TEMPLATES: dict[str, str] = {
    "prep_header": "【执行纪律：先完成前置检查，禁止进入操作步骤，禁止校验预期。】",
    "prep_current": "【当前只做前置】{precondition}",
    "prep_tail": (
        "每任务仅需一次 check_run_env（history 已有 env= 即满足）；"
        "禁止重复 read_device_data（history 已有 sim=READY 即满足）。"
        "前置满足后立刻 signal_done。锁屏/黑屏用 recover_*，不要用 prep 工具唤醒。"
        "登录态未确认时不要 signal_done。禁止进步骤、禁止验预期。"
    ),
    "do_header": "【执行纪律：严格按步骤编号。禁止跳到后面的步骤，禁止提前验后面的预期。】",
    "do_current": "【当前只做步骤 {n}】{instruction}",
    "do_tail": (
        "做完本步操作后调 signal_done，表示本步操作结束（不是整案结束）。"
        "目标已在屏上达成时也须 signal_done，禁止用 signal_give_up 表示本步完成。"
        "业务入口弹出登录弹窗时先完成登录，禁止只关弹窗反复点同一入口。禁止去做后面步骤。"
    ),
    "check_current": "【当前只验步骤 {n}】{expected}",
    "check_tail": "只看当前截图判断预期。调 assert_visual；通过后再 signal_done。禁止点击、滑动、输入来改界面凑绿。",
    "step_future": "[ ] 步骤 {n} 未到，禁止执行：{instruction}",
    "step_done": "[x] 步骤 {n} 已完成：{instruction}",
    "step_active_do": "[>] 步骤 {n} 操作中：{instruction}",
    "step_active_check": "[>] 步骤 {n} 校验中：{instruction} ｜ 预期：{expected}",
    "step_pending_check": "[ ] 步骤 {n} 未到，禁止执行：{instruction} ｜ 做完后校验",
    "all_done": "全部步骤已完成。不要再操作设备。",
}


def _pointer_templates() -> dict[str, str]:
    return _POINTER_TEMPLATES


def _pt(key: str, **kwargs: Any) -> str:
    tpl = _pointer_templates().get(key) or key
    try:
        return str(tpl).format(**kwargs)
    except (KeyError, ValueError):
        return str(tpl)

# agent 决策坐标是 0–1000 千分比；10‰ ≈ 1280 宽屏 13px，仅视为同一触点。
TAP_REPEAT_TOL_MILLI = 10
_OBSERVE_PREFIXES = ("查看", "观察", "确认屏", "检查屏", "目视", "看")


def enrich_assert_expectation(instruction: str, expected: str) -> str:
    """登录类步骤：收紧 VLM 校验措辞，减少密码页/验证码页混判。"""
    exp = str(expected or "").strip()
    instr = str(instruction or "").strip()
    blob = f"{instr}\n{exp}"
    if not exp:
        return exp
    if "验证码" in exp or "发送验证码" in blob:
        if "密码" not in exp:
            return (
                f"{exp}（必须已进入短信验证码输入/发送流程；"
                f"仍停留在账号密码登录页、仅填手机号未发码 → 不通过）"
            )
    if re.search(r"手机号登录|短信登录|验证码登录", blob) and re.search(
        r"切换|进入.*登录页|登录方式", exp
    ):
        if "密码" not in exp and "验证码" not in exp:
            return (
                f"{exp}（须为手机号/短信验证码登录入口或流程；"
                f"主流程为账号+密码登录页 → 不通过）"
            )
    return exp


_GUEST_STEP_RE = re.compile(r"游客|访客")
_GUEST_ENTRY_RE = re.compile(r"游客|访客")
_LOGIN_ENTRY_RE = re.compile(r"手机号|微信|登录|一键|账号密码|苹果|Apple", re.I)


def is_guest_entry_step(instruction: str) -> bool:
    return bool(_GUEST_STEP_RE.search(str(instruction or "")))


def tap_summary_is_guest_entry(summary: str) -> bool:
    return bool(_GUEST_ENTRY_RE.search(str(summary or "")))


def tap_summary_is_login_entry(summary: str) -> bool:
    text = str(summary or "")
    if _GUEST_ENTRY_RE.search(text):
        return False
    return bool(_LOGIN_ENTRY_RE.search(text))


def compile_guest_entry_hint(instruction: str, *, entry_tapped: bool) -> str:
    if not is_guest_entry_step(instruction):
        return ""
    if entry_tapped:
        return (
            "【游客入口】已点击访客/游客入口；若屏上已是 App 首页或内容页，"
            "请 signal_done 结束本步，勿再点「我的」或登录方式入口。"
        )
    return (
        "【游客入口】本步要点登录页右上角「游客/访客浏览」进入 App；"
        "进入首页后即 signal_done，不要在登录方式之间来回点。"
    )


def is_observe_only_step(instruction: str) -> bool:
    text = str(instruction or "").strip()
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in _OBSERVE_PREFIXES)


@dataclass
class SeqNode:
    n: int
    instruction: str
    expected: str
    observe_only: bool = False


def _column_lines(case: dict[str, Any], key: str, raw_key: str) -> list[str]:
    items = spec_lines(case.get(key))
    if items:
        return items
    raw = str(case.get(raw_key) or "").strip()
    if not raw:
        return []
    parsed = parse_numbered_items_rules(raw)
    if not parsed:
        return [raw]
    by_n: dict[int, str] = {}
    for it in parsed:
        t = str(it.get("text") or "").strip()
        n = int(it.get("num") or 0)
        if t and n:
            by_n[n] = t
    if not by_n:
        return [raw]
    return [by_n.get(i, "") for i in range(1, max(by_n) + 1)]


def build_seq_nodes(case: dict[str, Any]) -> list[SeqNode]:
    """按用例步骤编号建指针：步骤 n 做完才验同号预期。"""
    steps = _column_lines(case, "steps", "steps_raw")
    expected = _column_lines(case, "expected", "expected_raw")
    n = max(len(steps), len(expected), 1)
    if not steps and not expected:
        name = str(case.get("name") or case.get("case_id") or "").strip()
        if name:
            steps = [name]
        else:
            return []
    while len(steps) < n:
        steps.append("")
    while len(expected) < n:
        expected.append("")
    nodes: list[SeqNode] = []
    for i in range(n):
        inst = (steps[i] or "").strip()
        exp = (expected[i] or "").strip()
        if not inst and not exp:
            continue
        nodes.append(
            SeqNode(
                n=i + 1,
                instruction=inst,
                expected=exp,
                observe_only=is_observe_only_step(inst),
            )
        )
    return nodes


_NAV_CLEAR_CAPS = frozenset({
    "press_key",
    "swipe_direction",
    "swipe_element_to_element",
    "launch_app",
    "close_app",
})


def cap_clears_repeat_tap(cap_id: str) -> bool:
    """导航类操作后允许同坐标重试（误点进子页、返回后重勾等）。"""
    cid = str(cap_id or "").strip()
    if not cid:
        return False
    if cid in _NAV_CLEAR_CAPS or cid.startswith("recover_"):
        return True
    return False


def screen_fingerprint(
    *,
    hierarchy_text: str = "",
    image_base64: str = "",
    width: int = 0,
    height: int = 0,
) -> str:
    """粗粒度界面指纹：用于判断点击后页面是否切换。"""
    parts = [f"{int(width or 0)}x{int(height or 0)}"]
    hier = str(hierarchy_text or "").strip()
    if hier:
        parts.append(hier[:2400])
    elif image_base64:
        blob = str(image_base64)[:8192].encode("utf-8", errors="ignore")
        parts.append(hashlib.sha1(blob).hexdigest()[:16])
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def format_skip_repeat_read_device_message(last_summary: str = "") -> str:
    """写入 history，让模型在前置满足后 signal_done。"""
    hint = f"（上次结果：{last_summary[:80]}）" if last_summary else ""
    return (
        f"已拒绝重复 read_device_data{hint}：前置数据已读过且满足要求，"
        f"请直接 signal_done 结束前置；锁屏/黑屏请用 recover_screen_asleep_or_locked，不要用 read_device_data。"
    )


def last_read_device_summary(history_lines: list[str]) -> str:
    prefix = "read_device_data"
    for line in reversed(history_lines or []):
        text = str(line or "")
        if prefix not in text:
            continue
        if "→ pass:" in text:
            return text.split("→ pass:", 1)[-1].strip()
        if "→ skipped:" in text:
            return text.split("→ skipped:", 1)[-1].strip()
    return ""


def read_device_prep_satisfied(summary: str) -> bool:
    blob = str(summary or "").upper()
    if not blob:
        return False
    if "SIM=READY" in blob or "SIM=ABSENT" in blob:
        return True
    if blob.startswith("SIM=") or "SIM_STATE=" in blob:
        return True
    return False


def format_skip_repeat_tap_message(
    last: Optional[dict[str, Any]],
    params: Optional[dict[str, Any]],
) -> str:
    """写入 history，让模型知道同位置再点无效。"""
    p = params or {}
    last = last or {}
    sel = str(p.get("selector_text") or last.get("selector_text") or "").strip()
    if sel:
        target = f"「{sel}」"
    else:
        target = f"({p.get('x')},{p.get('y')})"
    return (
        f"已拒绝重复点击{target}：本步在此位置已点过一次且界面未变，"
        f"再点同位置不会推进步骤；请换元素/坐标，或先处理挡屏（返回、关弹窗、进登录页）。"
    )


def repeats_last_tap(
    last: Optional[dict[str, Any]],
    params: Optional[dict[str, Any]],
    *,
    tol: int = TAP_REPEAT_TOL_MILLI,
    tap_epoch: int = 0,
) -> bool:
    if not last or not params:
        return False
    if int(last.get("epoch") or 0) != int(tap_epoch or 0):
        return False
    try:
        lx, ly = int(last.get("x")), int(last.get("y"))
        nx, ny = int(params.get("x")), int(params.get("y"))
    except (TypeError, ValueError):
        return False
    return abs(lx - nx) <= tol and abs(ly - ny) <= tol


class StepCursor:
    """prep → do → check → 下一步。前置原文不并进步骤指令。"""

    def __init__(self, nodes: list[SeqNode], *, precondition: str = ""):
        self.nodes = list(nodes)
        self.precondition = str(precondition or "").strip()
        self.index = 0
        self.phase = "prep" if self.precondition else "do"
        self.saw_assert = False
        self.step_checked = False
        self.last_tap: Optional[dict[str, Any]] = None
        self.tap_epoch: int = 0
        self.step_ops: int = 0
        self.advise_recovery_counts: dict[str, int] = {}
        self.recovery_fail_counts: dict[str, int] = {}
        self.login_session_hint: str = ""
        self.otp_prep_hint: str = ""
        self.progress_gate: ProgressGate = ProgressGate()
        self.guest_entry_tapped: bool = False
        if self.phase != "prep":
            self._sync()

    def current(self) -> Optional[SeqNode]:
        if 0 <= self.index < len(self.nodes):
            return self.nodes[self.index]
        return None

    @property
    def done(self) -> bool:
        return self.phase == "done" or self.current() is None

    def all_uncheckable(self) -> bool:
        return bool(self.nodes) and all(not (n.expected or "").strip() for n in self.nodes)

    def finish_prep(self) -> None:
        self.index = 0
        self.step_checked = False
        self.step_ops = 0
        self.reset_guest_entry()
        self.progress_gate.reset_milestone("do", 1 if self.nodes else 0)
        self._sync()

    def record_step_op(self) -> None:
        self.step_ops += 1

    def bump_advise_recovery(self, rule_id: str) -> int:
        rid = str(rule_id or "").strip()
        n = int(self.advise_recovery_counts.get(rid, 0)) + 1
        self.advise_recovery_counts[rid] = n
        return n

    def bump_recovery_fail(self, rule_id: str) -> int:
        rid = str(rule_id or "").strip()
        n = int(self.recovery_fail_counts.get(rid, 0)) + 1
        self.recovery_fail_counts[rid] = n
        return n

    def mark_guest_entry(self, summary: str = "") -> None:
        if tap_summary_is_guest_entry(summary):
            self.guest_entry_tapped = True

    def reset_guest_entry(self) -> None:
        self.guest_entry_tapped = False

    def _sync(self) -> None:
        cur = self.current()
        if not cur:
            self.phase = "done"
            return
        if not cur.instruction and cur.expected:
            self.phase = "check"
            self.step_checked = False
            return
        if cur.observe_only and cur.expected:
            self.phase = "check"
            self.step_checked = False
            return
        if not cur.instruction and not cur.expected:
            self._skip_empty()
            return
        self.phase = "do"
        self.step_checked = False
        self.step_ops = 0

    def _skip_empty(self) -> str:
        if not self.advance():
            return "done"
        return self.phase

    def advance(self) -> bool:
        self.index += 1
        self.step_checked = False
        self.step_ops = 0
        self.reset_guest_entry()
        if self.index >= len(self.nodes):
            self.phase = "done"
            return False
        n = self.nodes[self.index].n if self.nodes else 0
        self.progress_gate.reset_milestone("do", n)
        self._sync()
        return True

    def enter_check(self) -> None:
        cur = self.current()
        if not cur:
            self.phase = "done"
            return
        if not cur.expected:
            self.advance()
            return
        self.phase = "check"
        self.step_checked = False
        self.progress_gate.reset_milestone("check", cur.n if cur else 0)

    def mark_checked(self) -> None:
        self.step_checked = True
        self.saw_assert = True

    def remember_tap(
        self,
        params: dict[str, Any],
        *,
        screen_fp: str = "",
        selector_text: str = "",
    ) -> None:
        try:
            self.last_tap = {
                "x": int(params.get("x")),
                "y": int(params.get("y")),
                "epoch": self.tap_epoch,
                "screen_fp": str(screen_fp or "").strip(),
                "selector_text": str(
                    selector_text or params.get("selector_text") or ""
                ).strip(),
            }
        except (TypeError, ValueError):
            return

    def clear_repeat_tap(self) -> None:
        """界面已导航离开（返回、滑动等）后，同坐标点击不算重复入口。"""
        self.tap_epoch += 1
        self.last_tap = None

    def prompt_block(self) -> str:
        if self.phase == "prep":
            lines = [
                _pt("prep_header"),
                f"[>] 前置（进行中）：{self.precondition}",
            ]
            for node in self.nodes:
                lines.append(_pt("step_future", n=node.n, instruction=node.instruction or "（无操作）"))
            lines.append(_pt("prep_current", precondition=self.precondition))
            if self.otp_prep_hint:
                lines.append(self.otp_prep_hint)
            warn = str(getattr(self.progress_gate, "warning_hint", "") or "").strip()
            if warn:
                lines.append(warn)
            lines.append(_pt("prep_tail"))
            return "\n".join(lines)

        lines = [_pt("do_header")]
        if self.precondition:
            lines.append(f"[x] 前置（已完成）：{self.precondition}")
        for i, node in enumerate(self.nodes):
            if i < self.index:
                bit = _pt("step_done", n=node.n, instruction=node.instruction or "（无操作）")
            elif i == self.index and self.phase == "do":
                bit = _pt("step_active_do", n=node.n, instruction=node.instruction or "（无操作）")
            elif i == self.index:
                bit = _pt(
                    "step_active_check",
                    n=node.n,
                    instruction=node.instruction or "（无操作）",
                    expected=node.expected,
                )
            else:
                bit = _pt("step_pending_check", n=node.n, instruction=node.instruction or "（无操作）")
            if not node.expected:
                bit += " ｜ 本步无预期（做完即过）" if not self.all_uncheckable() else " ｜ 本步无预期（无法校验）"
            elif i < self.index:
                bit += " ｜ 已校验"
            lines.append(bit)
        cur = self.current()
        if not cur:
            lines.append(_pt("all_done"))
            return "\n".join(lines)
        if self.phase == "do":
            lines.append(_pt("do_current", n=cur.n, instruction=cur.instruction))
            if self.login_session_hint:
                lines.append(self.login_session_hint)
            guest_hint = compile_guest_entry_hint(
                cur.instruction,
                entry_tapped=self.guest_entry_tapped,
            )
            if guest_hint:
                lines.append(guest_hint)
            warn = str(getattr(self.progress_gate, "warning_hint", "") or "").strip()
            if warn:
                lines.append(warn)
            lines.append(_pt("do_tail"))
        else:
            lines.append(_pt("check_current", n=cur.n, expected=cur.expected or "（无预期，无法执行校验）"))
            lines.append(_pt("check_tail"))
        return "\n".join(lines)

    def decide_goal(self) -> str:
        if self.phase == "prep":
            return f"完成前置检查：{self.precondition}"
        cur = self.current()
        if not cur:
            return "完成本步操作"
        if self.phase == "check":
            return f"校验步骤 {cur.n} 的预期：{cur.expected}"
        return (cur.instruction or "").strip() or "完成本步操作"

    def decide_success(self) -> str:
        if self.phase == "prep":
            return (
                f"前置已满足：{self.precondition}。"
                "不要去做步骤里的操作，不要校验预期。"
            )
        cur = self.current()
        if not cur:
            return "全部步骤已完成"
        if self.phase == "check":
            return (
                f"步骤 {cur.n} 的预期成立：{cur.expected}。"
                "用 assert_visual 判断当前屏。不要操作设备。"
            )
        return (
            f"完成步骤 {cur.n} 的操作：{cur.instruction}。"
            "不要用整案成功标准，也不要为了后面的字去改界面。"
        )
