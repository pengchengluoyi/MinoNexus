"""用例步骤指针：prep → do → check。不含原文关键字分流。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from mino_nexus.ai.case_text import parse_numbered_items_rules
from mino_nexus.services.run_store import spec_lines

_POINTER_TEMPLATES: dict[str, str] = {
    "prep_header": "【执行纪律：先完成前置检查，禁止进入操作步骤，禁止校验预期。】",
    "prep_current": "【当前只做前置】{precondition}",
    "prep_tail": "前置满足后调 signal_done。禁止去做步骤里的操作，也不要校验预期。",
    "do_header": "【执行纪律：严格按步骤编号。禁止跳到后面的步骤，禁止提前验后面的预期。】",
    "do_current": "【当前只做步骤 {n}】{instruction}",
    "do_tail": "做完本步操作后调 signal_done，表示本步操作结束（不是整案结束）。禁止去做后面步骤。",
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

# agent 决策坐标是 0–1000 千分比；约等于 1280 宽屏上 48px 容差。
TAP_REPEAT_TOL_MILLI = 40
_OBSERVE_PREFIXES = ("查看", "观察", "确认屏", "检查屏", "目视", "看")


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


def repeats_last_tap(
    last: Optional[dict[str, Any]],
    params: Optional[dict[str, Any]],
    *,
    tol: int = TAP_REPEAT_TOL_MILLI,
) -> bool:
    if not last or not params:
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
        self._sync()

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

    def _skip_empty(self) -> str:
        if not self.advance():
            return "done"
        return self.phase

    def advance(self) -> bool:
        self.index += 1
        self.step_checked = False
        if self.index >= len(self.nodes):
            self.phase = "done"
            return False
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

    def mark_checked(self) -> None:
        self.step_checked = True
        self.saw_assert = True

    def remember_tap(self, params: dict[str, Any]) -> None:
        try:
            self.last_tap = {"x": int(params.get("x")), "y": int(params.get("y"))}
        except (TypeError, ValueError):
            return

    def prompt_block(self) -> str:
        if self.phase == "prep":
            lines = [
                _pt("prep_header"),
                f"[>] 前置（进行中）：{self.precondition}",
            ]
            for node in self.nodes:
                lines.append(_pt("step_future", n=node.n, instruction=node.instruction or "（无操作）"))
            lines.append(_pt("prep_current", precondition=self.precondition))
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
