"""用例预算与收尾：耗时、结束、概览。"""
from __future__ import annotations

import time

from typing import Any

from mino_nexus.core.case_text import line_text

def _elapsed(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def _finish(emit, *, status: str, summary: str, steps: list, t0: float, pack=None) -> dict[str, Any]:
    extra = pack(status=status, summary=summary) if callable(pack) else {}
    emit("done", overall=status, status=status, summary=summary)
    from mino_nexus.loop.observe.session_project import _stamp_ui_status

    _stamp_ui_status(steps)
    out = {"status": status, "summary": summary, "steps": steps, "elapsed_ms": _elapsed(t0)}
    if extra:
        out.update({k: extra[k] for k in ("skill_id", "view_id", "slots") if k in extra})
    return out


def _finish_case(emit, *, cursor: StepCursor, steps: list, t0: float, pack=None) -> dict[str, Any]:
    if cursor.all_uncheckable() or not cursor.saw_assert:
        summary = "未写预期，无法校验"
        return _finish(emit, status="unverifiable", summary=summary, steps=steps, t0=t0, pack=pack)
    last = next((s for s in reversed(steps) if s.get("capability_id") == "assert_visual"), None)
    summary = (last or {}).get("summary") or "全部步骤已校验"
    return _finish(emit, status="pass", summary=summary, steps=steps, t0=t0, pack=pack)


def _case_overview(case: dict[str, Any]) -> str:
    name = str(case.get("name") or case.get("case_id") or "用例")
    steps = case.get("steps") if isinstance(case.get("steps"), list) else []
    body = "\n".join(t for t in (line_text(x) for x in steps) if t) or str(case.get("steps_raw") or "")
    bits = [name]
    if body:
        bits.append(f"步骤：{body}")
    pre = str(case.get("precondition") or "").strip()
    if pre:
        bits.append(f"前置：{pre}")
    return "\n".join(bits)
