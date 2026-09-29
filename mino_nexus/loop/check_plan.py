"""步骤 expected → CheckPlan（校验点草稿 + 歧义标记）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_AMBIGUOUS_MARKERS = (
    "登录成功",
    "进入",
    "跳转",
    "生成中",
    "3D空间",
    "3D预览",
    "提单",
    "详情页",
    "已登录",
    "未登录",
    "弹窗",
)
_PAGE_STATE_HINTS = re.compile(r"进入|跳转|页面|Tab|详情|空间|预览|拍摄|登录页", re.I)
_SESSION_HINTS = re.compile(r"登录成功|已登录|未登录|退出登录|guest", re.I)
_HIERARCHY_SPLIT = re.compile(r"[、,/；;|｜]")


@dataclass
class CheckPoint:
    id: str
    kind: str
    required: bool = True
    natural_language: str = ""
    weight: float = 1.0
    delta: int = 0
    baseline_key: str = ""


@dataclass
class CheckPlan:
    points: list[CheckPoint] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    raw_expected: str = ""

    @property
    def has_ambiguity(self) -> bool:
        return bool(self.ambiguities)


def _terms(expected: str) -> list[str]:
    out: list[str] = []
    for chunk in _HIERARCHY_SPLIT.split(str(expected or "").strip()):
        val = chunk.strip()
        if len(val) >= 2 and val not in out:
            out.append(val)
    if not out and str(expected or "").strip():
        out.append(str(expected).strip())
    return out[:12]


def _plan_from_checkpoints_v1(exp: str) -> CheckPlan | None:
    """扩展包 / expected 内嵌 mino.checkpoints.v1（P5）。"""
    import json

    if not exp.startswith("{"):
        return None
    try:
        doc = json.loads(exp)
    except json.JSONDecodeError:
        return None
    if str(doc.get("schema") or "") != "mino.checkpoints.v1":
        return None
    plan = CheckPlan(raw_expected=exp)
    for cp in doc.get("checkpoints") or []:
        if not isinstance(cp, dict):
            continue
        cid = str(cp.get("id") or "").strip() or f"cp{len(plan.points) + 1}"
        kind = str(cp.get("type") or "element_exists").strip().lower()
        sel = str(cp.get("selector") or cp.get("title") or cp.get("text") or "").strip()
        plan.points.append(
            CheckPoint(
                id=cid,
                kind=kind,
                required=bool(cp.get("required", True)),
                natural_language=sel or cid,
                delta=int(cp.get("delta") or 0),
                baseline_key=str(cp.get("baseline_key") or cid),
            )
        )
    return plan if plan.points else None


def build_check_plan(expected: str, *, instruction: str = "") -> CheckPlan:
    exp = str(expected or "").strip()
    v1 = _plan_from_checkpoints_v1(exp)
    if v1 is not None:
        return v1
    plan = CheckPlan(raw_expected=exp)
    if not exp:
        return plan
    for marker in _AMBIGUOUS_MARKERS:
        if marker in exp:
            plan.ambiguities.append(marker)
    idx = 0
    if _SESSION_HINTS.search(exp):
        idx += 1
        plan.points.append(
            CheckPoint(
                id=f"cp{idx}",
                kind="session",
                natural_language=exp,
            )
        )
    if _PAGE_STATE_HINTS.search(exp):
        idx += 1
        plan.points.append(
            CheckPoint(
                id=f"cp{idx}",
                kind="page_state",
                natural_language=exp,
            )
        )
    for term in _terms(exp):
        idx += 1
        plan.points.append(
            CheckPoint(
                id=f"cp{idx}",
                kind="hierarchy",
                natural_language=term,
            )
        )
    if "弹窗" in exp or "权限" in exp or "系统" in exp:
        idx += 1
        plan.points.append(
            CheckPoint(id=f"cp{idx}", kind="overlay", natural_language=exp)
        )
    blob = f"{exp} {instruction}"
    if not plan.points or ("生成中" not in exp and len(exp) > 24):
        plan.points.append(
            CheckPoint(
                id="cp_visual",
                kind="visual",
                required=False,
                natural_language=exp,
            )
        )
    if "详情" in blob and "3D" in blob:
        plan.ambiguities.append("page_wording")
    return plan


def format_check_plan_brief(plan: CheckPlan) -> str:
    if not plan.raw_expected:
        return ""
    kinds = "、".join(sorted({p.kind for p in plan.points})) or "visual"
    amb = "、".join(plan.ambiguities[:6])
    lines = [
        "【校验计划】本步 expected 将拆为校验点（pass 可自证；fail 需工具背书，见校验方案文档）。",
        f"建议能力：{kinds}。",
    ]
    if amb:
        lines.append(f"歧义/业务点：{amb} — 不确定时请 need_tools，禁止靠猜。")
    return "\n".join(lines)
