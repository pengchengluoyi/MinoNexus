"""应用探索指针：无 case 步骤，跟踪已发现屏面与 idle 终止。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from mino_nexus.loop.action_fuse import ProgressGate
from mino_nexus.loop.step_pointer import SeqNode
from mino_nexus.services.nav_screen_registry import explore_progress_block, explore_screen_identity


@dataclass
class ExploreCursor:
    """与 StepCursor 接口兼容，供 agent_loop / skill_trace 复用。"""

    goal: str
    success_criteria: str = ""
    max_steps: int = 80
    max_idle_steps: int = 15
    step: int = 0
    idle_steps: int = 0
    finished: bool = False
    known_screen_ids: list[str] = field(default_factory=list)
    _known_set: set[str] = field(default_factory=set)
    precondition: str = ""
    phase: str = "do"
    nodes: list[SeqNode] = field(default_factory=list)
    saw_assert: bool = False
    step_checked: bool = False
    last_tap: Optional[dict[str, Any]] = None
    tap_epoch: int = 0
    step_ops: int = 0
    advise_recovery_counts: dict[str, int] = field(default_factory=dict)
    recovery_fail_counts: dict[str, int] = field(default_factory=dict)
    login_session_hint: str = ""
    otp_prep_hint: str = ""
    progress_gate: ProgressGate = field(default_factory=lambda: ProgressGate(profile="explore"))
    guest_entry_tapped: bool = False
    known_screen_labels: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.nodes = [
            SeqNode(
                n=1,
                instruction=str(self.goal or "").strip() or "应用探索",
                expected="",
                observe_only=False,
            )
        ]
        self.progress_gate.reset_milestone("do", 0)

    @property
    def done(self) -> bool:
        return self.finished or self.step >= self.max_steps or self.idle_steps >= self.max_idle_steps

    def current(self) -> Optional[SeqNode]:
        if self.done:
            return None
        return self.nodes[0]

    def all_uncheckable(self) -> bool:
        return True

    def finish_prep(self) -> None:
        self.phase = "do"

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
        return

    def reset_guest_entry(self) -> None:
        self.guest_entry_tapped = False

    def clear_repeat_tap(self) -> None:
        self.tap_epoch += 1
        self.last_tap = None

    def remember_tap(self, params: dict[str, Any], *, screen_fp: str = "", selector_text: str = "") -> None:
        self.last_tap = dict(params or {})

    def enter_check(self) -> None:
        self.phase = "do"
        self.progress_gate.reset_milestone("do", max(1, self.step))

    def mark_checked(self) -> None:
        self.step_checked = True

    def advance(self) -> bool:
        return False

    def mark_explore_done(self) -> None:
        self.finished = True
        self.phase = "done"

    def note_observation(self, nodes: list[dict[str, Any]]) -> str:
        """每 turn observe 后登记屏面；返回与架构图同一套聚类键。"""
        self.step += 1
        if not nodes:
            self.idle_steps += 1
            return ""
        sid, label = explore_screen_identity(nodes)
        if sid in self._known_set:
            self.idle_steps += 1
        else:
            self._known_set.add(sid)
            self.known_screen_ids.append(sid)
            if label and label not in self.known_screen_labels:
                self.known_screen_labels.append(label)
            self.idle_steps = 0
            self.progress_gate.reset_milestone("do", self.step)
        return sid

    def progress_block(self) -> str:
        return explore_progress_block(
            known_screen_ids=self.known_screen_ids,
            known_screen_labels=self.known_screen_labels,
            step=self.step,
            max_steps=self.max_steps,
            idle_steps=self.idle_steps,
            max_idle_steps=self.max_idle_steps,
        )

    def prompt_block(self) -> str:
        lines = [
            "【应用探索】本任务无固定用例步骤，由你自由操作设备以发现更多页面。",
            f"目标：{self.goal}",
            self.progress_block(),
        ]
        return "\n".join(lines)

    def decide_goal(self) -> str:
        return str(self.goal or "应用探索")

    def decide_success(self) -> str:
        return str(self.success_criteria or "已尽量覆盖主要页面")

    def summary(self) -> str:
        n = len(self.known_screen_ids)
        if self.finished:
            return f"探索结束：发现 {n} 个屏面"
        if self.idle_steps >= self.max_idle_steps:
            return f"连续 {self.idle_steps} 步无新屏，探索结束（共 {n} 个屏面）"
        if self.step >= self.max_steps:
            return f"已达步数上限，探索结束（共 {n} 个屏面）"
        return f"探索完成（共 {n} 个屏面）"
