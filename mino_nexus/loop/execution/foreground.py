"""每回合的前台/启动保障：该拉起就拉起，该恢复就恢复，前台不对就回到目标应用。

三个出口：拉起成功（记一条 launch_app 并结束本轮）、环境不满足（整体阻塞）、
恢复后继续（不结束本轮）。返回 `FgFlow` 交给主循环。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mino_nexus.loop.channel_program_recovery import maybe_recover_foreground
from mino_nexus.loop.recovery import ensure_target_app_foreground
from mino_nexus.loop.execution.page_ready import _program_executor
from mino_nexus.loop.execution.turn_log import _log_program_tool, _log_turn_end
from mino_nexus.loop.launch_grace import stamp_launch_grace


@dataclass
class FgFlow:
    cont: bool = False
    leave: dict[str, Any] | None = None


def ensure_foreground(
    *,
    ctx: Any,
    proxy: Any,
    cursor: Any,
    nav: Any,
    clear_before_launch: bool,
    target_pkg: str,
    scout_run_id: str,
    case_seq: int,
    seq: int,
    thumb: Any,
    writer: Any,
    shot: Any,
    rec: Callable[..., Any],
    emit: Callable[..., None],
    leave: Callable[..., dict[str, Any]],
) -> FgFlow:
    if cursor.phase in ("prep", "do"):
        hier_nodes: list[Any] = []
        if nav is not None:
            snap = getattr(nav, "snapshot", None)
            hier_nodes = list(getattr(snap, "nodes", None) or []) if snap is not None else []
        launched = None
        if not (
            cursor.phase == "prep"
            and clear_before_launch
            and not bool(getattr(ctx, "prep_clear_done", False))
        ):
            launched = maybe_recover_foreground(
                proxy,
                ctx,
                run_id=scout_run_id,
                case_seq=case_seq,
                nodes=hier_nodes,
            )
        if launched:

            stamp_launch_grace(ctx)
            st = str(launched.get("status") or "pass")
            summ = str(launched.get("summary") or "")
            _log_program_tool(
                writer,
                capability_id="launch_app",
                params={"package": str(launched.get("package") or target_pkg)},
                status=st,
                summary=summ,
                executor_used=_program_executor(ctx),
            )
            rec(
                seq,
                capability_id="launch_app",
                status=str(launched.get("status") or "pass"),
                summary=str(launched.get("summary") or ""),
                thought=str(launched.get("summary") or ""),
                thumb=thumb,
                executor_used=_program_executor(ctx),
            )
            emit(
                "result",
                thought=str(launched.get("summary") or ""),
                step=seq,
                capability_id="launch_app",
                status=str(launched.get("status") or "pass"),
                summary=str(launched.get("summary") or ""),
                thumb=thumb,
            )
            _log_turn_end(
                writer,
                cap="launch_app",
                status=str(launched.get("status") or "pass"),
            )
            return FgFlow(cont=True)

        fg_turn = ensure_target_app_foreground(
            ctx,
            proxy,
            target_package=target_pkg,
            shot=shot,
        )
        if (
            clear_before_launch
            and cursor.phase == "prep"
            and not bool(getattr(ctx, "prep_clear_done", False))
        ):
            fg_turn = None
        if fg_turn and fg_turn.stop_run:
            return FgFlow(leave=leave(status="blocked", summary=fg_turn.summary(), reason_code="environment_precondition_unmet"))
        if fg_turn and fg_turn.applied:

            stamp_launch_grace(ctx)
            fg_st = "pass" if fg_turn.recovered else "fail"
            fg_summ = fg_turn.summary()
            _log_program_tool(
                writer,
                capability_id="launch_app",
                params={"package": target_pkg},
                status=fg_st,
                summary=fg_summ,
                executor_used=fg_turn.executor_used or _program_executor(ctx),
                execution_metadata=fg_turn.execution_metadata,
            )
            rec(
                seq,
                capability_id="launch_app",
                status=fg_st,
                summary=fg_summ,
                thought="程序将被测 App 带到前台",
                thumb=thumb,
                executor_used=_program_executor(ctx),
            )
            emit(
                "result",
                thought=fg_summ,
                step=seq,
                capability_id="launch_app",
                status=fg_st,
                summary=fg_summ,
                thumb=thumb,
            )
            _log_turn_end(
                writer,
                cap="launch_app",
                status=fg_st,
            )
            return FgFlow(cont=True)
    return FgFlow()
