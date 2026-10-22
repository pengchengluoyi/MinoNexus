"""Scout step_idx 编号：框架槽位与 agent 回合互不重叠。

纯算术，`loop` 与 `resources` 都要用；原住在 `loop/web/web_env.py`。
"""
from __future__ import annotations

# 框架 step_idx：与 agent 步骤 1..N 错开，且每个 case 独占一段。
FRAME_STEP = 10_000
# 每个 case 内 slot 0/1/9 留给 reset / launch / cleanup；agent 回合从 10 起。
FRAME_AGENT_SLOT_BASE = 10


def frame_step(case_seq: int, slot: int) -> int:
    return FRAME_STEP + max(0, int(case_seq)) * 10 + int(slot)


def agent_step_idx(case_seq: int, turn: int) -> int:
    """Agent 第 turn 回合（1..N）对应的 Scout step_idx，与框架 slot 0/1/9 错开。"""
    slot = FRAME_AGENT_SLOT_BASE + max(1, int(turn))
    return frame_step(case_seq, slot)
