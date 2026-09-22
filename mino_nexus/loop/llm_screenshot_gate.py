"""LLM 送图准入：前台非被测 App 时默认 withhold，模型可显式申请本回合送图。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.ai.schemas import AgentDecision

WITHHOLD_HINT = (
    "【送图准入】当前 probe 显示前台不是被测 App，本回合默认不向模型发送截图（避免微信/设置页误导决策）。"
    "若本步必须分析当前第三方/系统屏，请在 JSON 根字段设 "
    "`allow_foreign_foreground_llm_image`: true，系统将立即用同屏截图重试决策一次；"
    "分析完成后请设回 false 或勿再申请。"
)


def reset_case_llm_image_gate(ctx: Any) -> None:
    setattr(ctx, "allow_foreign_foreground_llm_image", False)


def case_foreign_image_policy(ctx: Any) -> str:
    scene = dict(getattr(ctx, "case_scene", None) or {})
    pol = str(scene.get("foreign_foreground_llm_image") or "deny").strip().lower()
    return pol if pol in ("deny", "allow_always") else "deny"


def foreign_foreground_active(ctx: Any) -> bool:
    """仅 probe/hierarchy 明确「不是被测 App 前台」时 withhold；unknown 不当作 foreign。"""
    fg = str(getattr(ctx, "app_foreground", "") or "").strip().lower()
    return fg == "no"


def should_withhold_llm_image(ctx: Any) -> bool:
    if case_foreign_image_policy(ctx) == "allow_always":
        return False
    if not foreign_foreground_active(ctx):
        return False
    return not bool(getattr(ctx, "allow_foreign_foreground_llm_image", False))


def execution_line(ctx: Any) -> str:
    pol = case_foreign_image_policy(ctx)
    transient = "allow" if bool(getattr(ctx, "allow_foreign_foreground_llm_image", False)) else "deny"
    fg = str(getattr(ctx, "app_foreground", "") or "unknown")
    return (
        f"【执行上下文·送图】case_policy={pol} transient={transient} app_foreground={fg}"
    )


def parse_allow_foreign_flag(decision: AgentDecision) -> Optional[bool]:
    raw = decision.raw_llm if isinstance(decision.raw_llm, dict) else {}
    if "allow_foreign_foreground_llm_image" not in raw:
        return None
    val = raw.get("allow_foreign_foreground_llm_image")
    if val is True or str(val).strip().lower() in ("true", "1", "yes", "allow"):
        return True
    if val is False or str(val).strip().lower() in ("false", "0", "no", "deny"):
        return False
    return None


def apply_allow_foreign_flag(ctx: Any, decision: AgentDecision) -> None:
    parsed = parse_allow_foreign_flag(decision)
    if parsed is True:
        setattr(ctx, "allow_foreign_foreground_llm_image", True)
    elif parsed is False:
        setattr(ctx, "allow_foreign_foreground_llm_image", False)


def clear_transient_llm_image_gate(ctx: Any) -> None:
    setattr(ctx, "allow_foreign_foreground_llm_image", False)
