"""用例指针的骨架：按步骤编号对齐 do/check（编译期与资源域共用，不碰设备）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from mino_nexus.core.case_text import parse_numbered_items_rules, spec_lines

_OBSERVE_PREFIXES = ("查看", "观察", "确认屏", "检查屏", "目视", "看", "等待")
_WAIT_THEN_ACT_RE = re.compile(r"等待.{0,16}(后|再).{0,12}(点击|输入|滑动|打开|进入|勾选)")



@dataclass
class SeqNode:
    n: int
    instruction: str
    expected: str
    observe_only: bool = False


_NUMBER_PREFIX = re.compile(r"^\s*\d+[.、．)\）]")


def _numbered_column_map(case: dict[str, Any], key: str, raw_key: str) -> dict[int, str]:
    """步骤/预期按编号对齐；缺号留空，禁止用「下一行预期」顶替中间步骤。"""
    raw = str(case.get(raw_key) or "").strip()
    if raw:
        parsed = parse_numbered_items_rules(raw)
        if parsed:
            out: dict[int, str] = {}
            for it in parsed:
                n = int(it.get("num") or 0)
                t = str(it.get("text") or "").strip()
                if n and t:
                    out[n] = t
            if out:
                return out

    items = spec_lines(case.get(key))
    if not items:
        return {}

    by_n: dict[int, str] = {}
    pos = 1
    for line in items:
        text = str(line or "").strip()
        if not text:
            continue
        if _NUMBER_PREFIX.match(text):
            for it in parse_numbered_items_rules(text):
                n = int(it.get("num") or 0)
                t = str(it.get("text") or "").strip()
                if n and t:
                    by_n[n] = t
        else:
            by_n[pos] = text
            pos += 1
    return by_n


CASE_WALL_SEC_PER_UNIT = 30
# 实测一个操作步 = 点击决策 + 回看确认（各一次模型调用）≈ 40–90s，校验步 ≈ 45s。
CASE_DO_STEP_WEIGHT = 2
CASE_CHECK_STEP_WEIGHT = 1.5


def count_precondition_units(precondition: str) -> int:
    pre = str(precondition or "").strip()
    if not pre:
        return 0
    parsed = parse_numbered_items_rules(pre)
    if len(parsed) > 1:
        return sum(1 for it in parsed if str(it.get("text") or "").strip())
    if parsed and _NUMBER_PREFIX.search(pre):
        return sum(1 for it in parsed if str(it.get("text") or "").strip())
    return 1


def build_seq_nodes(case: dict[str, Any]) -> list[SeqNode]:
    """按用例步骤编号建指针：步骤 n 做完才验同号预期。"""
    steps_map = _numbered_column_map(case, "steps", "steps_raw")
    expected_map = _numbered_column_map(case, "expected", "expected_raw")
    if not steps_map and not expected_map:
        name = str(case.get("name") or case.get("case_id") or "").strip()
        if name:
            steps_map = {1: name}
        else:
            return []
    nums = sorted(set(steps_map) | set(expected_map))
    nodes: list[SeqNode] = []
    for n in nums:
        inst = (steps_map.get(n) or "").strip()
        exp = (expected_map.get(n) or "").strip()
        if not inst and not exp:
            continue
        nodes.append(
            SeqNode(
                n=n,
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


def compute_case_wall_budget_sec(case: dict[str, Any]) -> int:
    """墙钟预算：前置条数×30 + 操作步号×60 + 校验步数×45（与操作步数同号）。"""
    prep = count_precondition_units(str(case.get("precondition") or ""))
    nodes = build_seq_nodes(case)
    do_units = max((node.n for node in nodes), default=0)
    check_units = do_units
    units = prep + do_units * CASE_DO_STEP_WEIGHT + check_units * CASE_CHECK_STEP_WEIGHT
    if units <= 0:
        units = 1
    try:
        override = int(case.get("wall_budget_sec") or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        return max(CASE_WALL_SEC_PER_UNIT, override)
    return int(round(units * CASE_WALL_SEC_PER_UNIT))


def is_observe_only_step(instruction: str) -> bool:
    text = str(instruction or "").strip()
    if not text:
        return False
    if _WAIT_THEN_ACT_RE.search(text):
        return False
    return any(text.startswith(prefix) for prefix in _OBSERVE_PREFIXES)
