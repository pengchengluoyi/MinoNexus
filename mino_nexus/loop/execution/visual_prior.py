"""do 阶段的视觉先验探针：这一屏到底是"新内容"还是"还在原地"。

探针失败必须静默（`except` 后只记日志）——它只是判定辅助，
挂在这里出错不该拖垮跑批。所以它被整段搬出来，而不是留在循环里。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.loop import step_effect as step_effect_mod
from mino_nexus.loop.execution.facts import _effect_nodes
from mino_nexus.loop.step_pointer import StepCursor, _expected_defers_to_check
from mino_nexus.navigation.atlas import nav_telemetry

TAG = "AgentLoop"


def probe_visual_prior(
    *,
    cursor: Any,
    screen_fp_turn: Any,
    nav: Any,
    ctx: Any,
    seq: int,
    cid: str,
    scout_run_id: str,
) -> None:
    if isinstance(cursor, StepCursor):
        if cursor.phase == "do" and not cursor.step_start_fp:
            cursor.refresh_step_start_fp(screen_fp_turn)
        cur_probe = cursor.current()
        from mino_nexus.loop.visual_review import has_visual_prior

        # 待回看的设备动作先按当前截图裁定；导航/文案命中不能绕过回看切阶段。
        if cursor.phase == "do" and cur_probe and not has_visual_prior(cursor) and (
            str(cur_probe.expected or "").strip() or str(cur_probe.instruction or "").strip()
        ):
            from mino_nexus.loop import step_effect as step_effect_mod
            from mino_nexus.navigation.atlas import nav_telemetry

            try:
                defer_chk = _expected_defers_to_check(str(cur_probe.expected or ""))
                probe_nodes = _effect_nodes(nav, ctx)
                loc_now = dict(getattr(nav, "localized", None) or {}) if nav is not None else {}
                loc_hit = step_effect_mod.localized_matches_step(
                    loc_now,
                    instruction=str(cur_probe.instruction or ""),
                    expected="" if defer_chk else str(cur_probe.expected or ""),
                )
                hit, keywords = False, []
                if probe_nodes:
                    hit, keywords = step_effect_mod.probe_expected_for_do(
                        str(cur_probe.expected or ""),
                        probe_nodes,
                        instruction=str(cur_probe.instruction or ""),
                        defer_expected_to_check=defer_chk,
                    )
                if loc_hit and not keywords:
                    keywords = [
                        str(loc_now.get("display_name") or loc_now.get("chosen") or "localized")
                    ]
                nav_telemetry.step_effect(
                    turn_id=seq,
                    run_id=scout_run_id,
                    case_id=cid,
                    step_n=cur_probe.n,
                    probe_hit=bool(hit or loc_hit),
                    model_done=False,
                    keywords=keywords,
                )
                from mino_nexus.loop.step_intent import instruction_required_intents

                if hit or loc_hit:
                    cursor.note_step_effect_hit(
                        keywords,
                        expected=str(cur_probe.expected or ""),
                    )
                    need_int_probe = instruction_required_intents(
                        str(cur_probe.instruction or "")
                    )
                    if "nav_tab" in need_int_probe and defer_chk:
                        cursor.step_intents_done.add("nav_tab")
                else:
                    cursor.reset_step_effect_streak()
                if probe_nodes and step_effect_mod.expected_profile_shape_config(
                    str(cur_probe.expected or "")
                ):
                    ps_hint = step_effect_mod.profile_shape_mismatch_hint(
                        str(cur_probe.expected or ""),
                        probe_nodes,
                    )
                    if ps_hint:
                        cursor.correction_hint = ps_hint
            except Exception as exc:  # noqa: BLE001 — 探针失败不能拖垮跑批
                SLog.w(TAG, f"step_effect probe failed: {type(exc).__name__}: {exc}")
