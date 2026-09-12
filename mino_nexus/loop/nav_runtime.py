"""NavFSM 在跑批循环里的编排入口。

为什么单独一层
--------------
`agent_loop._run_loop` 已经上千行，CLAUDE.md §2.2 要求循环代码保持干净（那里连「网络存不存在」
都不该感知）。NavFSM 涉及 hierarchy 采集、effect_assert 回判、localize、选边、编译 assist、
守卫判定、遥测七件事 —— 全铺进主循环会把它彻底冲垮。所以主循环只出现几个调用点：

    nav = NavRuntime.for_run(ctx=..., case=..., run_type=...)   # 关着就是 None
    nav.observe(proxy, turn_id=seq, slot_sink=inspect_slots, session_block=..., screenshot=shot)
    blocked = nav.preflight_block()   # 跑前租号初态（§11.2），mismatch 时主循环应 blocked 退出
    verdict = nav.check(cap_id=..., params=...)
    nav.after_execute(cap_id=..., params=..., status=...)
    nav.shutdown(writer=...)          # 预留；v2.5 无 walkthrough 收工

开关全关时 `for_run` 直接返回 `None`，主循环一行都不会走进来。

设计稿 docs/NAVIGATION_ATLAS.md §10.0.2、§19（被动采集）。
"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.core.log import SLog
from mino_nexus.loop.guard_gate import ACTION_ALLOW, GuardGate, Verdict
from mino_nexus.loop.hierarchy_slots import HierarchySnapshot, capture, observe_hierarchy_enabled
from mino_nexus.services import nav_compiler, nav_localize, nav_telemetry
from mino_nexus.services import nav_fsm as F
from mino_nexus.services import nav_fsm_store as store

TAG = "NavRuntime"


class NavRuntime:
    """一条用例一个实例。计数（GuardGate 阶梯、连续命中）随实例存活。"""

    def __init__(
        self,
        *,
        app_id: str,
        project_id: str,
        case: dict[str, Any],
        run_type: str,
        fsm: Optional[dict[str, Any]],
        fsm_reason: str = "",
        provider_id: str = "",
        session_id: str = "",
        account_id: str = "",
        target_package: str = "",
        platform: str = "android",
    ) -> None:
        self.app_id = app_id
        self.project_id = project_id
        self.case = dict(case or {})
        self.run_type = str(run_type or "manual").lower()
        self.fsm = fsm
        self.fsm_reason = fsm_reason
        self.provider_id = str(provider_id or "")
        self.session_id = str(session_id or "")
        self.account_id = str(account_id or "")
        self.target_package = str(target_package or "")
        self.platform = str(platform or "android")
        self.gate = GuardGate(run_type=self.run_type)

        self._captures_recorded: int = 0
        self._preflight_done: bool = False
        self._preflight_block: dict[str, Any] | None = None

        self.snapshot = HierarchySnapshot()
        self.plan: Optional[F.NavPlan] = None
        self.localized: dict[str, Any] = {}
        self._pending_edge: dict[str, Any] | None = None
        self._last_edge_to: str = ""
        self._last_edge_passed: bool = False
        self._turn_id: int = 0
        self._run_id: str = ""

    # ---------------- 构造 ----------------

    @classmethod
    def for_run(
        cls,
        *,
        ctx: Any,
        case: dict[str, Any],
        run_type: str = "manual",
        run_id: str = "",
        provider_id: str = "",
    ) -> Optional["NavRuntime"]:
        """开关关着就返回 None。hierarchy 通道是 NavFSM 的**前置**（§10.0.2 依赖图）。"""
        if not observe_hierarchy_enabled(ctx):
            return None
        from mino_nexus.loop.inspections import resolve_knowledge_scope

        app_id, project_id = resolve_knowledge_scope(ctx)
        account_id = str((getattr(ctx, "picked_account", None) or {}).get("id") or "")
        fsm, reason = store.load_with_reason(app_id, expected_account_id=account_id)
        if reason:
            level = SLog.w if "没有 nav_fsm 配置" not in reason else SLog.d
            level(TAG, f"NavFSM 不可用 app_id={app_id}: {reason}")
        inst = cls(
            app_id=app_id,
            project_id=project_id,
            case=case,
            run_type=run_type,
            fsm=fsm,
            fsm_reason=reason,
            provider_id=provider_id,
            session_id=str(run_id or ""),
            account_id=account_id,
            target_package=str(getattr(ctx, "target_package", "") or ""),
            platform=str(getattr(ctx, "platform", "") or "android"),
        )
        inst._run_id = str(run_id or "")
        return inst

    @property
    def active(self) -> bool:
        """有可用配置才谈得上导航；否则只是个 hierarchy 采集器。"""
        return bool(self.fsm)

    # ---------------- 每 turn ----------------

    def observe(
        self,
        proxy: Any,
        *,
        turn_id: int,
        slot_sink: dict[str, str],
        session_block: str = "",
        screenshot_turn_id: int | None = None,
        writer: Any = None,
        screenshot: Any = None,
    ) -> HierarchySnapshot:
        """采层级 → 回判上一条边 → localize → 选边 → 编译 assist。写两个槽。"""
        self._turn_id = int(turn_id)
        snap = capture(proxy, turn_id=turn_id, screenshot_turn_id=screenshot_turn_id)
        self.snapshot = snap
        slot_sink["hierarchy_text"] = snap.text if snap.ok else ""
        if writer is not None:
            try:
                writer.append("observe/hierarchy", snap.brief())
            except Exception:  # noqa: BLE001
                pass

        nodes = snap.nodes if snap.usable() else []
        if not self.active:
            slot_sink["nav_assist"] = ""
            self._record_capture(snap, writer=writer, localized={}, screenshot=screenshot)
            return snap

        self.localized = nav_localize.localize(
            states=self.fsm.get("states") or [],
            nodes=nodes,
            session_block=session_block,
            last_edge_to=self._last_edge_to,
            last_edge_passed=self._last_edge_passed,
        )

        if not self._preflight_done and nodes:
            self._run_preflight(nodes, writer=writer)

        state_id = str(self.localized.get("chosen") or "")
        state = F.state_by_id(self.fsm or {}, state_id)
        hits = F.evaluate_guards(state, nodes)
        hits = self._maybe_vlm_enrich(state, hits, screenshot)
        nav_telemetry.localize(
            turn_id=turn_id,
            run_id=self._run_id,
            case_id=str(self.case.get("case_id") or ""),
            state_id=self.localized.get("chosen") or "",
            confidence=self.localized.get("confidence") or 0.0,
            hierarchy_stale=snap.stale,
            observe_hierarchy=True,
            result="pass" if self.localized.get("chosen") else "fail",
            band=self.localized.get("band"),
            ambiguous=self.localized.get("ambiguous"),
            degraded=self.localized.get("degraded"),
        )

        self._settle_pending_edge(nodes)

        self.plan = F.build_plan(
            self.fsm,
            state_id=state_id,
            confidence=float(self.localized.get("confidence") or 0.0),
            band=str(self.localized.get("band") or "recover"),
            nodes=nodes,
            case=self.case,
            recover_caps=[],
            hits=hits,
        )
        self.gate.observe(self.plan.guards, hierarchy_ok=bool(nodes))
        slot_sink["nav_assist"] = nav_compiler.compile_assist(
            self.plan,
            app_id=self.app_id,
            project_id=self.project_id,
            run_type=self.run_type,
        )
        self._record_capture(snap, writer=writer, localized=self.localized, screenshot=screenshot)
        return snap

    def attach_turn_layout(self, turn_id: int, layout_vision: dict[str, Any] | None) -> None:
        """agent-decide / assert-vision 产出屏面布局后写入同 turn 采集。"""
        if not self.session_id or not layout_vision:
            return
        from mino_nexus.services import nav_capture_store as cap_store

        try:
            cap_store.patch_turn_layout(
                self.app_id,
                self.session_id,
                int(turn_id),
                layout_vision=layout_vision,
            )
        except Exception as exc:  # noqa: BLE001
            SLog.w(TAG, f"写入 VLM 布局失败（跑批继续）：{type(exc).__name__}: {exc}")

    def _run_preflight(self, nodes: list[dict[str, Any]], *, writer: Any = None) -> None:
        """跑前租号初态（§11.2）。只跑一次；mismatch 记到 `_preflight_block` 供主循环退出。"""
        self._preflight_done = True
        if not self.fsm:
            return
        result = F.check_precondition(
            self.fsm,
            state_id=str(self.localized.get("chosen") or ""),
            nodes=nodes,
        )
        if writer is not None and result.get("result") != "skip":
            try:
                writer.append("nav/precondition", result)
            except Exception:  # noqa: BLE001
                pass
        if result.get("result") == "mismatch":
            self._preflight_block = result

    def preflight_block(self) -> dict[str, Any] | None:
        """主循环在 observe 后调用：若租号初态不对，返回 blocked 原因。"""
        return self._preflight_block

    def _maybe_vlm_enrich(
        self,
        state: dict[str, Any] | None,
        hits: list[F.GuardHit],
        screenshot: Any,
    ) -> list[F.GuardHit]:
        """hierarchy 判不出 widget 态时，可选 VLM 兜底（§2.2）。VLM 结论不可 block。"""
        if not self.fsm or not screenshot:
            return hits
        cfg = F.vlm_fallback_cfg(self.fsm)
        if not cfg.get("enabled"):
            return hits
        image_b64 = ""
        image_mime = "image/png"
        if hasattr(screenshot, "has_image") and screenshot.has_image():
            image_b64 = str(getattr(screenshot, "image_base64", "") or "")
            image_mime = str(getattr(screenshot, "image_mime", "") or "image/png")
        if not image_b64:
            return hits

        from mino_nexus.ai.planner import judge_widget_state

        max_calls = int(cfg.get("max_calls_per_turn") or 1)
        min_conf = float(cfg.get("min_confidence") or 0.6)
        for cand in F.vlm_candidates(state, hits)[:max_calls]:
            judged = judge_widget_state(
                widget=str(cand.get("widget") or ""),
                candidate_states=list(cand.get("states") or []),
                image_base64=image_b64,
                image_mime=image_mime,
                provider_id=self.provider_id or None,
            )
            if not judged.get("ok"):
                continue
            conf = float(judged.get("confidence") or 0.0)
            if conf < min_conf:
                continue
            state_name = str(judged.get("state") or "")
            if F.apply_vlm_state(hits, widget=str(cand.get("widget") or ""), widget_state=state_name, confidence=conf):
                nav_telemetry.vlm_widget_state(
                    turn_id=self._turn_id,
                    run_id=self._run_id,
                    widget=cand.get("widget"),
                    state=state_name,
                    confidence=conf,
                )
        return hits

    def _record_capture(
        self,
        snap: HierarchySnapshot,
        *,
        writer: Any = None,
        localized: dict[str, Any] | None = None,
        cap_id: str = "",
        screenshot: Any = None,
    ) -> None:
        """v2.5 被动采集：每 turn 落盘，不依赖 walkthrough 批次。"""
        if not self.session_id or not snap.ok or not snap.nodes:
            return
        from mino_nexus.services import nav_capture_store as cap_store

        image_b64 = ""
        if screenshot is not None and hasattr(screenshot, "has_image") and screenshot.has_image():
            image_b64 = str(getattr(screenshot, "image_base64", "") or "")
        try:
            from mino_nexus.services.nav_target_scope import resolve_app_target_scope, scope_from_values

            scope = scope_from_values(self.platform, self.target_package) or resolve_app_target_scope(
                self.app_id,
                platform=self.platform,
                target_package=self.target_package,
            )
            meta = cap_store.append_turn(
                self.app_id,
                self.session_id,
                turn_id=self._turn_id,
                project_id=self.project_id,
                account_id=self.account_id,
                run_id=self._run_id,
                case_id=str(self.case.get("case_id") or ""),
                run_type=self.run_type,
                nodes=snap.nodes,
                hierarchy_stale=snap.stale,
                localized=localized or {},
                layout_hierarchy={},
                target_package=scope.target_id if scope else self.target_package,
                platform=scope.platform if scope else self.platform,
                target_scope=scope.as_dict() if scope else None,
                cap_id=cap_id,
                error=snap.error,
                screenshot_b64=image_b64,
            )
            if meta:
                self._captures_recorded += 1
                try:
                    from mino_nexus.services import nav_live_graph

                    nav_live_graph.sync_on_new_capture(
                        self.app_id,
                        project_id=self.project_id,
                        updated_by="capture",
                    )
                except Exception as sync_exc:  # noqa: BLE001
                    SLog.w(TAG, f"采集后导航合成失败（跑批继续）：{type(sync_exc).__name__}: {sync_exc}")
        except Exception as exc:  # noqa: BLE001
            SLog.w(TAG, f"被动采集失败（跑批继续）：{type(exc).__name__}: {exc}")
            return
        if writer is not None and meta:
            try:
                writer.append(
                    "nav/capture_turn",
                    {
                        "session_id": self.session_id,
                        "turn_id": self._turn_id,
                        "node_count": meta.get("node_count"),
                        "state_id": str((localized or {}).get("chosen") or ""),
                    },
                )
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self, *, writer: Any = None, status: str = "", summary: str = "") -> dict[str, Any] | None:
        """跑批结束摘要（v2.5 无 walkthrough 收工）。"""
        if self._captures_recorded <= 0:
            return None
        payload = {
            "session_id": self.session_id,
            "captures_recorded": self._captures_recorded,
            "run_status": str(status or ""),
        }
        if writer is not None:
            try:
                writer.append("nav/capture_finish", payload)
            except Exception:  # noqa: BLE001
                pass
        return payload

    def _recover_caps(self) -> list[str]:
        caps = F.recover_capabilities(self.fsm or {})
        if caps:
            return caps
        for edge in F.recover_edges_for_case(self.fsm or {}, self.case):
            caps.extend(str(s) for s in ((edge.get("execute") or {}).get("steps") or []))
        return [c for c in caps if c]

    def _settle_pending_edge(self, nodes: list[dict[str, Any]]) -> None:
        pending = self._pending_edge
        if not pending:
            return
        if self.snapshot.stale:
            return
        state_id = str(self.localized.get("chosen") or "")
        kind = ""
        st = F.state_by_id(self.fsm or {}, state_id)
        if st:
            kind = str(st.get("kind") or "")
        result, reason = F.evaluate_effect_assert(
            pending.get("effect_assert"),
            nodes=nodes,
            chosen_state=state_id,
            state_kind=kind,
        )
        self._last_edge_to = str(pending.get("to") or "")
        self._last_edge_passed = result == "pass"
        nav_telemetry.edge_attempt(
            turn_id=self._turn_id,
            run_id=self._run_id,
            case_id=str(self.case.get("case_id") or ""),
            state_id=state_id,
            edge_id=str(pending.get("id") or ""),
            cap_id=str(pending.get("cap_id") or ""),
            result=result,
            hierarchy_stale=self.snapshot.stale,
            observe_hierarchy=True,
            reason=reason,
        )
        self._pending_edge = None

    def check(self, *, cap_id: str, params: dict[str, Any] | None) -> Verdict:
        if not self.active or self.plan is None:
            return Verdict()
        hierarchy_ok = bool(self.snapshot.usable())
        verdict = self.gate.check(
            cap_id=cap_id,
            params=params,
            hits=self.plan.guards,
            hierarchy_ok=hierarchy_ok,
            allowed=self.plan.allow,
            enforce_allowed=bool(self.plan.band == "high" and self.plan.edge),
        )
        self._log_verdict(verdict, cap_id=cap_id, params=params)
        return verdict

    def _log_verdict(self, verdict: Verdict, *, cap_id: str, params: dict[str, Any] | None) -> None:
        base = {
            "turn_id": self._turn_id,
            "run_id": self._run_id,
            "case_id": str(self.case.get("case_id") or ""),
            "state_id": self.plan.state_id if self.plan else "",
            "confidence": self.plan.confidence if self.plan else 0.0,
            "guard_id": verdict.guard_id,
            "strength": verdict.strength,
            "cap_id": cap_id,
            "strong_text_consecutive_hits": verdict.consecutive_hits,
            "hierarchy_stale": self.snapshot.stale,
            "observe_hierarchy": True,
        }
        if verdict.action != ACTION_ALLOW:
            nav_telemetry.guard_hit(**base, action=verdict.action, result="skip", reason=verdict.reason)
            return
        if verdict.guard_id:
            nav_telemetry.guard_miss_candidate(
                **base,
                action="",
                result="pass",
                llm_tapped=cap_id,
                selector_text=str((params or {}).get("selector_text") or ""),
                hierarchy_snippet=self.snapshot.text[:600],
            )

    def after_execute(self, *, cap_id: str, params: dict[str, Any] | None, status: str, writer: Any = None) -> None:
        if not self.active or self.plan is None or not self.plan.edge:
            return
        if str(status or "").lower() != "pass":
            return
        steps = [str(s) for s in ((self.plan.edge.get("execute") or {}).get("steps") or [])]
        if steps and str(cap_id or "") not in steps:
            return
        self._pending_edge = {
            "id": str(self.plan.edge.get("id") or ""),
            "to": str(self.plan.edge.get("to") or ""),
            "effect_assert": self.plan.edge.get("effect_assert") or {},
            "cap_id": str(cap_id or ""),
        }

    def steer_line(self, verdict: Verdict) -> str:
        bits = [f"【导航守卫】{verdict.reason}"]
        if self.plan and self.plan.edge:
            bits.append(f"改走 {self.plan.edge.get('id')}")
        elif self.plan and self.plan.scroll_hint:
            bits.append("先滑动把目标滚进屏幕")
        return "；".join(b for b in bits if b)
