"""RouterProxy：签名兼容上游 `CapabilityRouter`，内部把动作发给 Scout。

这是本次拆分**最省力的一处**（CLAUDE.md §2.2）。上游 `agent_executor.py`(3,533 行)、
`recovery.py`、`orchestrator.py` 只依赖一个签名：

    result: EventResult = router.dispatch(event, ctx, ...)

本类提供同签名实现，于是那几千行循环代码搬过来**基本不用改** —— 改的是注入进去的对象。

职责（选路在 Nexus，执行在 Scout）：
  1. 按 **这台 sn** 的设备（platform + 已连通通道）定点选 executor，不是按节点上挂了哪些插件
  2. 查能力目录：这条 cap 在该设备上允许哪些 implementation
  3. 合成 executor_order：只含该设备适用的通道（Web 不含 adb，安卓不含 playwright）
  4. 从 catalog_entries 取 low_level / selected_impl，抄进 EXECUTE 给 Scout
  5. 注入 device_hint（sn → adb_serial / password）
  6. 发 EXECUTE，等 RESULT；超时按协议 §6 重发一次
  7. RESULT → EventResult（五态、attempts、executor_used 原样透传）

**循环代码不该知道 Scout 存在。** 网络、重试、节点选择都在这里。
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Optional

from mino_nexus.core import protocol as P
from mino_nexus.core.wire_identity import next_auxiliary_step_idx
from mino_nexus.catalog import registry as catalog
from mino_nexus.catalog.exec_classes import LOCAL_EXECUTOR_IDS as LOCAL_CAPS
from mino_nexus.services.device_secrets import get_lock_password
from mino_nexus.core.log import SLog
from mino_nexus.services.node_registry import NodeSession, get_registry
from mino_nexus.core.schemas import CapturedScreen, EventResult, EventStatus, PlanEvent

TAG = "RouterProxy"
_DIAGNOSTIC_PURPOSES = {
    "plan_action": "执行计划动作", "recovery_action": "恢复动作",
    "recovery_evidence": "恢复取证", "foreground_guard": "前台状态守护",
    "app_version": "记录应用版本", "preflight": "开跑截图检查",
    "loop_observation": "观察当前界面",
}


def _diagnostic_purpose(value: Any, default: str = "plan_action") -> str:
    # 不记录 label/ai_reasoning/params，避免把输入内容或账号凭证写进诊断。
    key = str(value or default)
    return key if key in _DIAGNOSTIC_PURPOSES else default


def _trace_device_result(
    req: P.Execute, res: P.Result | None, *, total_ms: int, purpose: str, logical_seq: int | None = None,
) -> None:
    status = getattr(res.status, "value", res.status) if res is not None else "timeout"
    fields = {
        "capability_id": req.capability_id, "step_idx": req.step_idx,
        "purpose": purpose, "status": status, "elapsed_ms": total_ms,
        "scout_ms": int(res.elapsed_ms or 0) if res is not None else 0,
        "executor_used": res.executor_used if res is not None else "",
    }
    if req.binding_id:
        fields["execution_binding"] = {key: getattr(req, key) for key in ("binding_id", "operation_ref",
            "node_connection_generation", "scout_process_generation", "capability_revision")}
    if req.step_idx > 0:
        fields["action_ref"] = _action_ref(req)
        fields["logical_seq"] = logical_seq
    raw = {**dict(res.extra or {}), **dict(res.data or {})} if res is not None else {}
    from mino_nexus.loop.observe.execution_diagnostics import execution_result_metadata
    fields.update(execution_result_metadata(raw))
    for key in ("target_active", "verification_required"):
        if isinstance(raw.get(key), bool):
            fields[key] = raw[key]
    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    for key in ("foreground_pid", "pid", "window_id"):
        value = raw.get(key, target.get(key))
        if isinstance(value, int) and not isinstance(value, bool):
            fields[key] = value
    action = raw.get("cua_action") if isinstance(raw.get("cua_action"), dict) else {}
    if action.get("effect") in ("confirmed", "unverifiable", "refused", "partial", "suspected_noop"):
        fields["effect"] = action["effect"]
    if raw.get("capture_scope") in ("window", "display", "screen"):
        fields["capture_scope"] = raw["capture_scope"]
    if res is not None and req.capability_id in ("screenshot", "capture_screen"):
        fields["width"] = res.width or int(raw.get("width") or 0)
        fields["height"] = res.height or int(raw.get("height") or 0)
        fields["has_image"] = bool(res.image_base64 or raw.get("image_base64"))
    launch = raw.get("launch") if isinstance(raw.get("launch"), dict) else {}
    if launch.get("stage") in ("launch_requested", "pid_verified", "window_ready", "window_binding"):
        fields["stage"] = launch["stage"]
    details = " ".join(f"{k}={v}" for k, v in fields.items() if k not in ("capability_id", "purpose", "step_idx"))
    message = f"[{req.run_id}] result cap={req.capability_id} step={req.step_idx} purpose={_DIAGNOSTIC_PURPOSES[purpose]} {details}"
    (SLog.i if status == "pass" else SLog.w)(TAG, message)
    if req.binding_id or req.step_idx > 0 or req.capability_id == "get_foreground_app" or purpose in ("recovery_evidence", "recovery_action"):
        from mino_nexus.loop.observe.session_log import active_writer

        writer = active_writer()
        if writer is not None and writer.session_id == req.run_id:
            writer.append("device/action" if req.step_idx > 0 or purpose == "recovery_action" else "device/probe", fields)


# case_runner 在后台线程跑同步 agent_loop，Scout WS 却在 FastAPI 主 loop。
# 若用 asyncio.run() 临时 loop 去 await node.send，RESULT 回在主 loop 上，
# waiter 永远等不到 → observe/action 每次都顶满 timeout+pad（17s / 35s）。
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
_OBSERVE_TIMEOUT_SEC = 8.0
_OBSERVE_PAD_SEC = 2.0
_MUTATE_TIMEOUT_SEC = 10.0
_DEFAULT_ACTION_TIMEOUT_SEC = 15.0
_WIRE_STEP_LOCK = threading.Lock()
_WIRE_STEPS: dict[str, int] = {}


def _next_read_idx(run_id: str) -> int:
    return next_auxiliary_step_idx(run_id)


def _next_wire_step_idx(run_id: str) -> int:
    with _WIRE_STEP_LOCK:
        next_step = _WIRE_STEPS.get(run_id, 0) + 1
        _WIRE_STEPS[run_id] = next_step
        return next_step


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop

# 本地 executor 不出网（CLAUDE.md §2.3）。名单在 exec_classes.LOCAL_EXECUTOR_IDS。
LOCAL_CAP_PREFIXES = ("human_", "recover_", "signal_")


def is_local_cap(capability_id: str) -> bool:
    return capability_id in LOCAL_CAPS or capability_id.startswith(LOCAL_CAP_PREFIXES)


class NoRouteError(Exception):
    """派不出去。原因必须说清是哪一种（NODE_REGISTRY.md §4：不要静默排队）。"""


class RouterProxy:
    def __init__(
        self,
        sn: str,
        *,
        run_id: str = "",
        task_id: str = "",
        timeout_pad_sec: float = 2.0,
        target_package: str = "",
        playwright_headless: bool = True,
        run_context: Any = None,
        execution_mode: str = "legacy",
        execution_control: Any = None,
    ):
        from mino_nexus.services.access_scope import require_owned_device
        require_owned_device(sn)
        self.sn = sn
        self.run_id = run_id
        self.task_id = str(task_id or "").strip()
        self.target_package = str(target_package or "")
        self.playwright_headless = bool(playwright_headless)
        self.run_context = run_context
        self.execution_mode = execution_mode
        self.execution_control = execution_control
        self.execution_binding = None
        self._binding_required = execution_mode == 'v5'
        self._binding_requirements = None
        self._binding_target = None
        self._binding_session_id = ''
        self._binding_phase_override = ''
        # 协议 §6：等 RESULT 用 payload.timeout_sec + 宽限
        self.timeout_pad_sec = timeout_pad_sec

    def bind_execution(self, requirements, *, session_id, env_profile='', env_surface='', probe_url='', deadline=None):
        """Enable the new contract only after its binding transaction commits."""
        from copy import deepcopy
        from mino_nexus.services.execution_binding_runtime import bind_attempt
        from mino_nexus.services.execution_binding import CONTRACT
        self._binding_required = True
        self.execution_binding = None
        self._binding_requirements = self._binding_target = None
        self._binding_session_id = ''
        if self.run_context is not None:
            self.run_context.execution_contract_version = CONTRACT
            self.run_context.execution_binding = None
        target = {'sn': self.sn, 'env_profile': env_profile, 'env_surface': env_surface,
                  'attempt_id': session_id, 'task_id': self.task_id, 'headless': self.playwright_headless}
        if probe_url:
            target['probe_url'] = probe_url
        binding = bind_attempt(session_id, requirements, target, deadline=deadline)
        self.execution_binding = deepcopy(binding)
        self._binding_requirements = deepcopy(requirements)
        self._binding_target = target
        self._binding_session_id = session_id
        if self.run_context is not None:
            self.run_context.execution_binding = deepcopy(binding)
            self.run_context.execution_binding_menu = self.binding_menu
        return deepcopy(binding)

    def _binding_projection(self, event, options):
        from mino_nexus.services.execution_binding_runtime import BindingAdmissionError, committed_projection
        from mino_nexus.loop.observe.session_log import active_writer
        writer = active_writer()
        phase = options.get('binding_phase') or self._binding_phase_override or (writer.phase if writer else '')
        if self.execution_binding is None or not self._binding_session_id:
            raise BindingAdmissionError('binding_not_committed')
        if options.get('binding_run_id', self.run_id) != self.run_id:
            raise BindingAdmissionError('binding_run_mismatch')
        combination = None
        if event.capability_id == 'input_text':
            from mino_nexus.action_space.assemble import stamp_scheme_params
            from mino_nexus.action_space.scheme import execution_scheme
            params = stamp_scheme_params(event.capability_id, event.params, execution_scheme(self.run_context or self))
            combination = {'contract_version': 2, 'target_mode': params.get('target_mode') or params.get('point_policy'),
                           'write_mode': params.get('mode') or 'replace'}
        return committed_projection(self._binding_session_id, self.execution_binding['binding_id'],
            self._binding_requirements, self._binding_target, event_id=event.capability_id,
            operation_ref=options.get('operation_ref') or getattr(event, 'operation_ref', ''), phase=phase,
            dynamic_target=(event.params or {}).get('url') or (event.params or {}).get('package') or self.target_package,
            input_combination=combination)

    def binding_menu(self, phase):
        """Menus use the same committed/fresh projection as dispatch; fail closed."""
        from mino_nexus.services.execution_binding_runtime import committed_projection, menu_projection
        binding = self.execution_binding
        if not binding or self._task_aborted():
            return set()
        rows = [row for row in binding['operations'] if row['phase'] == phase]
        if not rows:
            return set()
        row = rows[0]
        try:
            committed_projection(self._binding_session_id, binding['binding_id'],
                self._binding_requirements, self._binding_target, event_id=row['event_id'],
                operation_ref=row['operation_ref'], phase=phase)
        except Exception as exc:
            # Preserve the actual admission failure. An empty bound menu must
            # never erase an epoch/resource failure into a generic missing tool.
            from mino_nexus.loop.observe.session_log import active_writer
            writer = active_writer()
            if writer is not None:
                writer.append("binding/menu_blocked", {
                    "binding_id": binding['binding_id'], "phase": phase,
                    "reason_code": getattr(exc, 'reason_code', 'binding_menu_resolution_failed'),
                    "diagnostics": [{key: item[key] for key in ('reason_code', 'operation_ref', 'responsibility', 'missing_fields') if key in item}
                                    for item in getattr(exc, 'diagnostics', ())],
                    "execution_started": "no"})
            return set()
        return menu_projection(binding, phase)

    def _task_aborted(self) -> bool:
        tid = self.task_id
        if not tid:
            return False
        try:
            from mino_nexus.services import run_store

            return run_store.task_cancelled(tid)
        except Exception:
            return False

    # ---------------- 对外：与上游 dispatch 同签名 ----------------

    def dispatch(
        self,
        event: PlanEvent,
        *,
        run_id: str = "",
        step_idx: int = -1,
        **kwargs: Any,
    ) -> EventResult:
        """同步接口 —— 上游循环是同步代码，这里内部跑事件循环。"""
        return _run_sync(
            self.dispatch_async(
                event,
                run_id=run_id or self.run_id,
                step_idx=step_idx,
                **kwargs,
            )
        )

    def observe(
        self,
        kind: str = "screenshot",
        *,
        prefer: Optional[tuple[str, ...]] = None,
        force_fresh: bool = True,
        timeout_sec: float = _OBSERVE_TIMEOUT_SEC,
        compress_ratio: float | None = None,
        diagnostic_purpose: str = "loop_observation",
    ) -> CapturedScreen:
        """替代上游的 `capture_screen(ctx, ...)`，返回值保持 CapturedScreen 形状。"""
        plat = ""
        if kind == "screenshot":
            from mino_nexus.services.settings import (
                channel_screenshot_compress_ratio,
                note_channel_capture_ratio,
            )

            node, _why = get_registry().resolve(self.sn)
            plat = _device_platform(self.sn, node) if node is not None else ""
            if compress_ratio is None:
                compress_ratio = channel_screenshot_compress_ratio(plat)
            note_channel_capture_ratio(float(compress_ratio or 1.0), authoritative=plat in {"desktop", "mac", "win"})
        shot = _run_sync(
            self.observe_async(
                kind, prefer=prefer, force_fresh=force_fresh,
                timeout_sec=timeout_sec, compress_ratio=compress_ratio,
                diagnostic_purpose=diagnostic_purpose,
            )
        )
        if kind == "screenshot":
            note_channel_capture_ratio(
                float((shot.remote_detail or {}).get("compress_ratio") or compress_ratio or 1.0),
                authoritative=plat in {"desktop", "mac", "win"},
            )
        return shot

    def notify_scout_cancel_run(self, run_id: str = "") -> None:
        """通知 Scout 结束 run：清幂等缓存并回收 Web 环境（best-effort）。"""
        _run_sync(self.notify_scout_cancel_run_async(run_id))

    async def notify_scout_cancel_run_async(self, run_id: str = "") -> None:
        node, why = await asyncio.to_thread(get_registry().resolve, self.sn)
        if node is None:
            SLog.w(TAG, f"cancel_run 跳过 sn={self.sn}: {why}")
            return
        rid = run_id or self.run_id
        req = P.Execute(
            run_id=rid,
            step_idx=_next_read_idx(rid),
            sn=self.sn,
            capability_id="cancel_run",
            params={},
            executor_order=[],
            low_level={},
            selected_impl={},
            device_hint=self._device_hint(node),
            timeout_sec=10.0,
            device_id=self.sn,
            platform=_device_platform(self.sn, node),
        )
        await self._send_with_retry(node, P.MsgType.EXECUTE, req, req.timeout_sec)

    # ---------------- 异步实现 ----------------

    async def dispatch_async(
        self,
        event: PlanEvent,
        *,
        run_id: str = "",
        step_idx: int = -1,
        **kwargs: Any,
    ) -> EventResult:
        started = _now_iso()
        t0 = time.time()

        # N0 复用当前 session，后续 N2 在同一守卫上加入绑定事务。
        # 观察也可能带副作用前置（如开启浏览器），因此持久化失效后不再派发。
        from mino_nexus.loop.observe.session_log import active_writer

        session_writer = active_writer()
        if session_writer is not None:
            session_writer.require_persisted()

        if self.execution_mode == "v5":
            return await self._dispatch_v5(event, run_id or self.run_id, started, t0, kwargs)

        if await asyncio.to_thread(self._task_aborted):
            return _fail(
                event,
                started,
                t0,
                "任务已取消",
                executor_used="router_proxy",
                local_reason="task_cancelled",
            )

        from mino_nexus.catalog.skill_channel import CAP_ALIASES, canonical_cap, capability_dispatch_ok

        raw_cap = str(getattr(event, "capability_id", "") or "")
        if raw_cap in CAP_ALIASES:
            mapped = canonical_cap(raw_cap)
            event.capability_id = mapped
            if str(getattr(event, "event_kind", "") or "") == raw_cap:
                event.event_kind = mapped

        from mino_nexus.catalog.tool_schema import CONFIRM_ACTION_RESULT

        if event.capability_id == CONFIRM_ACTION_RESULT:
            return _fail(
                event, started, t0,
                "结果确认必须先由回看状态机消费，不能作为设备操作下发",
                executor_used="router_proxy", local_reason="review_confirmation_not_consumed",
            )

        if str(event.capability_id or "").startswith("plugin."):
            if self._binding_required:
                return await self._dispatch_bound_otp(event, started=started, t0=t0,
                    run_id=run_id or self.run_id, options=kwargs)
            return await self._dispatch_host_plugin(
                event, started=started, t0=t0, run_id=run_id, step_idx=step_idx,
            )

        if not capability_dispatch_ok(event.capability_id):
            return _fail(
                event,
                started,
                t0,
                f"能力未启用或当前不可覆盖: {event.capability_id}",
                executor_used="router_proxy",
                local_reason="capability_disabled",
            )

        if is_local_cap(event.capability_id):
            if self._binding_required:
                try:
                    local_projection = await asyncio.to_thread(self._binding_projection, event, {**kwargs, 'binding_run_id': run_id or self.run_id})
                    if local_projection['runs_on'] != 'nexus':
                        raise ValueError('binding_local_assembly_invalid')
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ValueError) else 'binding_store_unavailable'
                    return _fail(event, started, t0, reason, executor_used='router_proxy', local_reason=reason,
                        raw_response={'execution': {'execution_started': 'no', 'phase': 'binding', 'reason_code': reason}})
            ctx = kwargs.get("ctx") or self.run_context
            # 本地能力里还会再 dispatch（例如 relogin → read_web_auth）。
            # 放在线程里，事件循环才能接着把这次 EXECUTE 发给 Scout。
            local_result = await asyncio.to_thread(
                _dispatch_local_in_runtime,
                event,
                shot=kwargs.get("shot"),
                ctx=ctx,
                router=self,
                target_package=self.target_package,
            )
            if self._binding_required:
                local_result.raw_response = {**(local_result.raw_response or {}), "execution_binding": {
                    "binding_id": local_projection['binding_id'], "operation_ref": local_projection['operation_ref'],
                    "runs_on": "nexus"}}
            return local_result

        node, why = await asyncio.to_thread(get_registry().resolve, self.sn)
        if node is None:
            return _fail(event, started, t0, why, executor_used="router_proxy")

        if _device_platform(self.sn, node) in {"desktop", "mac", "win"}:
            from mino_nexus.services.device_admission import owns_desktop
            dev = node.devices.get(self.sn)
            hint = self._device_hint(node)
            task_id = self.task_id or (run_id or self.run_id).split("::", 1)[0]
            if not dev or dev.desktop_state != "ready" or hint.get("session_generation") != dev.session_generation or not owns_desktop(task_id, node.node_id):
                return _fail(event, started, t0, "桌面未就绪、会话已变化或没有独占租约", executor_used="router_proxy", local_reason="desktop_not_admitted")
        projection = None
        try:
            if self._binding_required:
                projection = await asyncio.to_thread(self._binding_projection, event, {**kwargs, 'binding_run_id': run_id or self.run_id})
                order, impl = projection['executor_order'], projection['selected_impl']
            else:
                order, impl = self._plan_route(event, node)
        except ValueError as exc:
            return _fail(event, started, t0, str(exc), executor_used='router_proxy',
                local_reason=str(exc), raw_response={'execution': {
                    'execution_started': 'no', 'phase': 'binding', 'reason_code': str(exc)}})
        except NoRouteError as exc:
            msg = str(exc)
            reason = "no_impl_for_device" if "无可用实现" in msg else "cap_not_in_catalog"
            return _fail(event, started, t0, msg, executor_used="router_proxy", local_reason=reason)
        except Exception:
            if not self._binding_required:
                raise
            return _fail(event, started, t0, 'binding_store_unavailable', executor_used='router_proxy',
                local_reason='binding_store_unavailable', raw_response={'execution': {
                    'execution_started': 'no', 'phase': 'binding', 'reason_code': 'binding_store_unavailable'}})

        plat = _device_platform(self.sn, node)
        family = set(executors_for_platform(plat))
        sanitized = [ex for ex in order if ex in family]
        if sanitized != order:
            SLog.w(
                TAG,
                f"[{(self.run_id or '')[:8]}] executor_order 与平台不符 sn={self.sn}"
                f" plat={plat} was={order} now={sanitized}",
            )
        order = sanitized
        if not order:
            return _fail(
                event,
                started,
                t0,
                f"cap={event.capability_id} 在 {self.sn}（{plat}）上无合法执行通道"
                f"（允许 {sorted(family)}）",
                executor_used="router_proxy",
                local_reason="executor_platform_family_empty",
            )

        from mino_nexus.catalog.tool_schema import fill_target_package

        params = fill_target_package(
            event.params,
            cap_id=event.capability_id,
            target_package=self.target_package,
            low_level=(impl or {}).get("low_level"),
        )
        from mino_nexus.action_space.assemble import stamp_scheme_params, visual_point_missing
        from mino_nexus.action_space.scheme import execution_scheme as action_scheme

        ctx = kwargs.get("ctx") or self.run_context
        scheme = action_scheme(ctx if ctx is not None else self)
        focus_ref = str(params.get("focus_ref") or "")
        params = stamp_scheme_params(event.capability_id, params, scheme)
        if scheme != "dom" and visual_point_missing(event.capability_id, params):
            return _fail(
                event, started, t0,
                "当前视觉操作缺少完整坐标，未派发到节点；普通步骤、逻辑块和恢复步骤均需提供有效操作参数",
                executor_used="router_proxy", local_reason="visual_coordinates_missing",
            )
        if event.capability_id == "input_text" and params.get("target_mode") == "current_focus" and scheme != "dom":
            dispatch_run_id = run_id or self.run_id
            prefix = f"focus:{dispatch_run_id}:"
            if not dispatch_run_id or not focus_ref.startswith(prefix) or not focus_ref[len(prefix):].isdigit():
                return _fail(
                    event, started, t0,
                    "当前焦点输入缺少本次运行的已绑定 focus_ref，未派发到节点",
                    executor_used="router_proxy",
                    local_reason="focus_reference_invalid",
                )
            params["focus_ref"] = focus_ref
        if _device_platform(self.sn, node) in {"desktop", "mac", "win"} and event.capability_id in ("tap_element", "tap_xy"):
            params["focus_ref"] = f"focus:{run_id or self.run_id}:{event.seq}"
        event.params = params
        if projection is not None and event.capability_id == 'input_text':
            combination = projection['input_combination']
            if ((params.get('target_mode') or params.get('point_policy')) != combination.get('target_mode')
                    or (params.get('mode') or 'replace') != combination.get('write_mode')):
                return _fail(event, started, t0, 'binding_input_combination_changed',
                    executor_used='router_proxy', local_reason='binding_input_combination_changed')

        req = P.Execute(
            run_id=run_id or self.run_id,
            step_idx=_next_wire_step_idx(run_id or self.run_id),
            sn=self.sn,
            capability_id=event.capability_id,
            params=params,
            executor_order=order,
            low_level=dict((impl or {}).get("low_level") or {}),
            selected_impl=dict(impl or {}),
            device_hint=self._device_hint(node),
            timeout_sec=_timeout_for(event),
            device_id=self.sn,
            platform=_device_platform(self.sn, node),
            **_binding_wire_fields(projection),
        )
        if projection is not None:
            req.device_hint = {**projection['device_hint_template'], **req.device_hint}

        purpose = _diagnostic_purpose(kwargs.get("diagnostic_purpose"))
        from mino_nexus.core.secret_redaction import register_inputs
        register_inputs(req.params)
        register_inputs(req.device_hint)
        SLog.i(TAG, f"[{req.run_id}] dispatch cap={req.capability_id} step={req.step_idx} purpose={_DIAGNOSTIC_PURPOSES[purpose]} order={order} impl={(impl or {}).get('id')}")
        from mino_nexus.services import run_write_safety
        # Unknown capabilities are conservatively writes; only known framework reads are exempt.
        from mino_nexus.services.execution_binding import effect_class
        is_write = effect_class(req.capability_id) != "read"
        try:
            ticket = run_write_safety.begin(self.task_id, req, node_id=node.node_id, scout_process_generation=node.scout_process_generation) if is_write and self.task_id else ""
        except run_write_safety.WriteAdmissionError as exc:
            if session_writer is not None:
                session_writer.append("guard/block", {"reason_code": exc.reason_code,
                    "capability_id": req.capability_id, "action_ref": _action_ref(req),
                    "blocking_actions": exc.blocking_actions, "execution_started": "no"})
                session_writer.require_persisted()
            result = _fail(event, started, t0, str(exc), executor_used="router_proxy",
                raw_response={"execution": {"execution_started": "no", "phase": "admission", "reason_code": exc.reason_code},
                              "execution_control": {"stop_run": True, "reason_code": exc.reason_code}})
            result.status = EventStatus.BLOCKED
            return result
        res = await self._send_with_retry(node, P.MsgType.EXECUTE, req, req.timeout_sec)
        await asyncio.to_thread(_trace_device_result, req, res, total_ms=int((time.time() - t0) * 1000), purpose=purpose, logical_seq=event.seq)
        if res is None:
            result = _fail(event, started, t0,
                f"scout 未返回 RESULT（node={node.node_id}）；动作结果未核验，停止未确认",
                executor_used="router_proxy", raw_response={"scout": _request_metadata(node, req),
                    "action_ref": _action_ref(req), "execution": {"execution_started": "unknown", "phase": "transport", "reason_code": "result_missing"}})
        else:
            result = _event_result_from(event, res, started, node=node, request=req)
        if ticket:
            if session_writer is not None:
                session_writer.require_persisted()
            isolated = run_write_safety.finish(self.task_id, ticket, result.raw_response, passed=result.status is EventStatus.PASS)
            if isolated:
                result.status = EventStatus.BLOCKED
                result.summary = "写动作结果未核验，已阻止后续写入并隔离设备与账号资源"
                result.raw_response["resource_isolated"] = True
        return result

    async def _dispatch_v5(self, event, run_id, started, t0, options):
        from mino_nexus.core.secret_redaction import register_inputs
        from mino_nexus.services.web_v5_contract import PROFILE
        reads = {"probe", "read_web_target_facts", "screenshot"}
        cleanup = {"cancel_run", "close_web_v5"}
        writes = {"open_web_v5", "tap_target", "input_target"}
        cap = event.capability_id
        monotonic_start = time.monotonic()
        control = self.execution_control
        reason = ""
        if cap not in reads | cleanup | writes:
            reason = "unsupported_v5_capability"
        elif cap in writes and (control.state != "running" or await asyncio.to_thread(self._task_aborted)):
            reason = control.reason_code or "task_cancelled"
        node, why = await asyncio.to_thread(get_registry().resolve, self.sn)
        if not node or self.sn != f"web{node.node_id}":
            reason = reason or "node_pin_mismatch"
        requested = float(options.get("timeout_sec", 6))
        remaining = control.remaining()
        reserve = float(options.get("reserve_sec", 0))
        if remaining < requested + reserve and cap in writes:
            reason = reason or "timeout_before_dispatch"
        available = min(requested, 1) if cap in cleanup else min(requested, max(0, remaining - .05))
        if available <= 0:
            reason = reason or "timeout_before_dispatch"
        if reason:
            stopped = control.stop(reason) if cap in writes else {}
            return _fail(event, started, t0, reason, executor_used="router_proxy",
                raw_response={"execution": {"execution_started": "no", "phase": "admission", "reason_code": reason},
                              "execution_control": stopped})
        try:
            projection = await asyncio.to_thread(self._binding_projection, event, {**options, 'binding_run_id': run_id})
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else 'binding_store_unavailable'
            return _fail(event, started, t0, reason, executor_used='router_proxy',
                raw_response={'execution': {'execution_started': 'no', 'phase': 'binding', 'reason_code': reason}})
        impl = projection['selected_impl']
        register_inputs(event.params)
        req = P.Execute(run_id=run_id, step_idx=_next_wire_step_idx(run_id) if cap in writes else _next_read_idx(run_id),
            sn=self.sn, capability_id=cap, params={**event.params, "timeout_profile": PROFILE},
            executor_order=projection['executor_order'], selected_impl=impl, low_level=projection['low_level'],
            device_hint={**projection['device_hint_template'], "execution_mode": "v5", "headless": self.playwright_headless, "node_id": node.node_id},
            timeout_sec=max(.001, available - min(1, available / 4)), device_id=self.sn, platform="web",
            **_binding_wire_fields(projection))
        try:
            res = await self._send_once(node, P.MsgType.EXECUTE, req, available, pad_sec=0)
        except Exception:
            res = None
        # A read can be retried once, only inside its original deadline/window.
        if res is None and cap in reads:
            left = min(control.remaining(), available - (time.monotonic() - monotonic_start))
            if left > .05:
                req = P.Execute(**{**req.__dict__, "step_idx": _next_read_idx(run_id), "timeout_sec": max(.001, left - .05)})
                try:
                    res = await self._send_once(node, P.MsgType.EXECUTE, req, left, pad_sec=0)
                except Exception:
                    res = None
        if res is None:
            reason = "effect_unknown" if cap in writes else "observation_missing"
            raw = {"execution": {"execution_started": "unknown", "phase": "transport", "reason_code": reason},
                   "action_ref": _action_ref(req)}
            if cap in writes:
                raw["execution_control"] = control.stop(reason, _action_ref(req))
            return _fail(event, started, t0, reason, executor_used="router_proxy", raw_response=raw)
        result = _event_result_from(event, res, started, node=node, request=req)
        if cap in writes and (control.state != "running" or await asyncio.to_thread(self._task_aborted)):
            result.raw_response["execution_control"] = control.stop(control.reason_code or "task_cancelled", _action_ref(req))
            result.status = EventStatus.FAIL
        execution = result.raw_response.get("execution") or {"execution_started": "unknown", "reason_code": "execution_metadata_missing"}
        if cap in writes and (execution.get("execution_started") != "yes" or result.status != EventStatus.PASS
                              or result.raw_response.get("execution_control", {}).get("stop_run")):
            reason = execution.get("reason_code") or "effect_unknown"
            result.raw_response["execution_control"] = control.stop(reason, _action_ref(req))
            result.status = EventStatus.FAIL
        return result

    async def _dispatch_bound_otp(self, event, *, started, t0, run_id, options):
        from mino_nexus.services.otp_binding import GMAIL_EVENT, GMAIL_EXECUTOR, OTP_CHILD
        from mino_nexus.services.local_tool_dependencies import evaluate
        ctx = options.get('ctx') or self.run_context
        try:
            child = OTP_CHILD.get()
            if event.capability_id != GMAIL_EVENT or child is None or child[:2] != (id(self), event.params):
                raise ValueError('binding_plugin_child_denied')
            facts = evaluate('get_otp', ctx=ctx, execution=True)
            if any(fact['status'] != 'ready' for fact in facts):
                raise ValueError(next(fact['reason_code'] for fact in facts if fact['status'] != 'ready'))
            opts = {**options, 'binding_run_id': run_id}
            parent = PlanEvent(seq=0, capability_id='get_otp', event_kind='get_otp', params={})
            await asyncio.to_thread(self._binding_projection, parent, opts)
            projection = await asyncio.to_thread(self._binding_projection, event, opts)
            if projection['runs_on'] != 'scout' or projection['executor_order'] != [GMAIL_EXECUTOR]:
                raise ValueError('binding_plugin_assembly_invalid')
            node, _ = await asyncio.to_thread(get_registry().resolve, self.sn)
            from mino_nexus.core.execution_user import require_user_id
            if node is None or node.owner_user_id != require_user_id():
                raise ValueError('plugin_node_unavailable')
            left = float(event.params['deadline_ts']) - time.time()
            timeout = min(left, float(event.params['max_wait_ms']) / 1000.0)
            if timeout <= 0 or await asyncio.to_thread(self._task_aborted):
                raise ValueError('otp_wait_stopped')
        except (ValueError, PermissionError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else 'otp_binding_invalid'
            return _fail(event, started, t0, reason, executor_used='router_proxy', local_reason=reason)
        req = P.Execute(run_id=run_id, step_idx=_next_read_idx(run_id), sn=self.sn,
            capability_id=GMAIL_EVENT, params=dict(event.params),
            executor_order=projection['executor_order'], selected_impl=projection['selected_impl'],
            low_level=projection['low_level'], device_hint=projection['device_hint_template'],
            timeout_sec=timeout, device_id=self.sn, platform=_device_platform(self.sn, node),
            **_binding_wire_fields(projection))
        try:
            res = await self._send_once(node, P.MsgType.EXECUTE, req, timeout, pad_sec=0)
        except Exception:
            res = None
        await asyncio.to_thread(_trace_device_result, req, res, total_ms=int((time.time() - t0) * 1000),
                             purpose='plan_action', logical_seq=event.seq)
        if await asyncio.to_thread(self._task_aborted) or time.time() >= float(event.params['deadline_ts']):
            return _fail(event, started, t0, 'otp_wait_stopped', executor_used='router_proxy')
        # Revalidate parent/child and generations before accepting a late result.
        try:
            await asyncio.to_thread(self._binding_projection, parent, opts)
            await asyncio.to_thread(self._binding_projection, event, opts)
        except Exception:
            return _fail(event, started, t0, 'otp_binding_changed', executor_used='router_proxy')
        if res is None:
            return _fail(event, started, t0, 'otp_transport_timeout', executor_used='router_proxy')
        if str(getattr(res.status, 'value', res.status)) == 'pass':
            import re
            code = str((res.data or {}).get('code') or (res.extra or {}).get('code') or '')
            if not re.fullmatch(r'[0-9]{4,8}', code):
                return _fail(event, started, t0, 'otp_result_invalid', executor_used='router_proxy')
            # Only the awaiting local parent receives the code; logs/model results do not.
            child[2]['code'] = code
            from mino_nexus.core.secret_redaction import remember
            remember(code)
        return _event_result_from(event, res, started, node=node, request=req)

    async def _dispatch_host_plugin(
        self,
        event: PlanEvent,
        *,
        started: str,
        t0: float,
        run_id: str,
        step_idx: int,
        projection: dict | None = None,
    ) -> EventResult:
        """具名插件能力。参数里没有密钥，密钥在执行节点自己的保险库。"""
        node, why = await asyncio.to_thread(get_registry().resolve, self.sn)
        if node is None:
            return _fail(event, started, t0, why, executor_used="router_proxy")
        from mino_nexus.core.execution_user import require_user_id
        uid = require_user_id()
        if node.owner_user_id != uid:
            return _fail(event, started, t0, "plugin_node_unavailable", executor_used="router_proxy")
        if event.capability_id == "plugin.gmail.fetch_otp":
            from mino_nexus.services.execution_dependencies import plugin_fact
            dependency = plugin_fact([node], {"kind": "plugin", "class": "mail",
                "plugin_id": "gmail", "node_id": node.node_id}, uid)
            if dependency["status"] != "ready":
                return _fail(event, started, t0, dependency["reason_code"], executor_used="router_proxy",
                             raw_response={"dependencies": [dependency]})
        params = dict(event.params or {})
        try:
            wait_ms = int(params.get("max_wait_ms") or 30_000)
        except (TypeError, ValueError):
            wait_ms = 30_000
        timeout = min(600.0, max(20.0, wait_ms / 1000.0 + 15.0))
        req = P.Execute(
            run_id=run_id or self.run_id,
            step_idx=_next_read_idx(run_id or self.run_id),
            sn=self.sn,
            capability_id=event.capability_id,
            params=params,
            executor_order=projection['executor_order'] if projection else [],
            low_level=projection['low_level'] if projection else {},
            selected_impl=projection['selected_impl'] if projection else {},
            device_hint=projection['device_hint_template'] if projection else {},
            timeout_sec=timeout,
            device_id=self.sn,
            platform=_device_platform(self.sn, node),
            **_binding_wire_fields(projection),
        )
        res = await self._send_with_retry(node, P.MsgType.EXECUTE, req, req.timeout_sec)
        await asyncio.to_thread(_trace_device_result, req, res, total_ms=int((time.time() - t0) * 1000),
                             purpose='plan_action', logical_seq=event.seq)
        if res is None:
            return _fail(
                event, started, t0,
                f"scout timeout（node={node.node_id}，重发一次仍无 RESULT）",
                executor_used="router_proxy",
                raw_response={"scout": _request_metadata(node, req)},
            )
        return _event_result_from(event, res, started, node=node, request=req)

    async def observe_async(
        self,
        kind: str = "screenshot",
        *,
        prefer: Optional[tuple[str, ...]] = None,
        force_fresh: bool = True,
        timeout_sec: float = _OBSERVE_TIMEOUT_SEC,
        compress_ratio: float | None = None,
        diagnostic_purpose: str = "loop_observation",
    ) -> CapturedScreen:
        from mino_nexus.loop.observe.session_log import active_writer
        session_writer = active_writer()
        if session_writer is not None:
            session_writer.require_persisted()
        t0 = time.monotonic()
        purpose = _diagnostic_purpose(diagnostic_purpose, "loop_observation")
        if await asyncio.to_thread(self._task_aborted):
            return CapturedScreen(ok=False, source="router_proxy", error="任务已取消")
        node, why = await asyncio.to_thread(get_registry().resolve, self.sn)
        if node is None:
            return CapturedScreen(ok=False, source="router_proxy", error=why)

        plat = _device_platform(self.sn, node)
        if compress_ratio is None and kind == "screenshot":
            from mino_nexus.services.settings import channel_screenshot_compress_ratio

            compress_ratio = channel_screenshot_compress_ratio(plat)
        elif compress_ratio is None:
            compress_ratio = 1.0
        if kind == "screenshot":
            from mino_nexus.services.settings import note_channel_capture_ratio

            note_channel_capture_ratio(float(compress_ratio or 1.0), authoritative=plat in {"desktop", "mac", "win"})
        family = set(executors_for_platform(plat))
        device_order = executors_for_device(self.sn, node)
        if prefer is None:
            prefer = tuple(device_order)
        else:
            prefer = tuple(ex for ex in prefer if ex in family)
        if not prefer:
            return CapturedScreen(
                ok=False,
                source="router_proxy",
                error=f"sn={self.sn} platform={plat} 没有适用的截图通道",
            )
        SLog.i(
            TAG,
            f"[{self.run_id}] observe {kind} purpose={_DIAGNOSTIC_PURPOSES[purpose]} sn={self.sn} plat={plat} order={list(prefer)}",
        )

        cap = P.OBSERVE_CAPS.get(kind, kind)
        projection = None
        if self._binding_required:
            try:
                projection = await asyncio.to_thread(self._binding_projection, PlanEvent(seq=0, capability_id=cap), {})
            except Exception as exc:
                return CapturedScreen(ok=False, source='router_proxy',
                    error=str(exc) if isinstance(exc, ValueError) else 'binding_store_unavailable')
        req = P.Execute(
            run_id=self.run_id,
            step_idx=_next_read_idx(self.run_id),
            sn=self.sn,
            capability_id=cap,
            params={
                "force_fresh": force_fresh,
                "compress_ratio": compress_ratio,
            },
            executor_order=projection['executor_order'] if projection else list(prefer),
            low_level=projection['low_level'] if projection else {},
            selected_impl=projection['selected_impl'] if projection else {},
            device_hint={**(projection['device_hint_template'] if projection else {}), **self._device_hint(node)},
            timeout_sec=timeout_sec,
            device_id=self.sn,
            platform=_device_platform(self.sn, node),
            **_binding_wire_fields(projection),
        )
        # 截图每轮都要走；observe 单独用更紧的 pad，且不重试（重试会把一步拖到 ~40s+）。
        res = await self._send_once(
            node, P.MsgType.EXECUTE, req, timeout_sec, pad_sec=_OBSERVE_PAD_SEC,
        )
        await asyncio.to_thread(_trace_device_result, req, res, total_ms=int((time.monotonic() - t0) * 1000), purpose=purpose)
        if res is None:
            return CapturedScreen(
                ok=False, source="router_proxy",
                error=f"scout timeout（node={node.node_id}，EXECUTE {cap}）",
            )
        if res.status is not EventStatus.PASS:
            return CapturedScreen(ok=False, source=res.source or "", error=res.error or res.summary)
        data = dict(res.data or {})
        shot = CapturedScreen(
            ok=True,
            source=res.source,
            image_base64=res.image_base64 or str(data.get("image_base64") or ""),
            image_mime=res.image_mime or str(data.get("image_mime") or ""),
            width=res.width or int(data.get("width") or 0),
            height=res.height or int(data.get("height") or 0),
            elapsed_ms=res.elapsed_ms,
            remote_detail={**dict(res.extra or {}), **data},
        )
        if kind == "screenshot":
            note_channel_capture_ratio(
                float((shot.remote_detail or {}).get("compress_ratio") or compress_ratio or 1.0),
                authoritative=plat in {"desktop", "mac", "win"},
            )
        return shot

    # ---------------- 选路 ----------------

    def _plan_route(self, event: PlanEvent, node: NodeSession) -> tuple[list[str], dict[str, Any]]:
        """算这台 sn 的 executor_order + 选中的 implementation。

        screenshot / hierarchy 是协议框架指令，不进能力目录，所以 observe 不走这里。
        选路函数仍是同一个：executors_for_device(sn) —— 只看这台设备，不看节点上其它插件。

        顺序：
          1. event.expected_executor（AI 选的）
          2. event.fallback_executors
          3. 该 cap 在目录里允许的其它 executor
        再按 **这台 sn 已连通且类型匹配的通道** 过滤。
        """
        plat = _device_platform(self.sn, node)
        cap = catalog.get_capability(event.capability_id)
        if plat in {"desktop", "mac", "win"}:
            from mino_nexus.services.desktop_catalog import CAPABILITY_IDS, capabilities

            if event.capability_id in CAPABILITY_IDS:
                dev = node.devices.get(self.sn)
                reported = list(getattr(dev, "capabilities", None) or [])
                os_name = plat if plat in {"mac", "win"} else str(getattr(dev, "os", "") or "")
                cap = next((candidate for candidate in capabilities(reported, os_name, platform=plat)
                            if candidate.id == event.capability_id), None)
        if cap is None:
            raise NoRouteError(
                f"能力目录里没有 cap={event.capability_id}"
                "（AI 选了菜单外的能力？或 catalog_entries 缺这条）"
            )

        by_executor: dict[str, Any] = {}
        for impl in cap.implementations or []:
            ex = str(getattr(impl, "executor", "") or "").strip()
            if ex and ex not in by_executor:
                by_executor[ex] = impl

        ordered: list[str] = []
        for ex in [event.expected_executor, *(event.fallback_executors or [])]:
            if ex and ex not in ordered and ex in by_executor:
                ordered.append(ex)
        for ex in by_executor:
            if ex not in ordered:
                ordered.append(ex)

        plat = _device_platform(self.sn, node)
        allowed = set(executors_for_device(self.sn, node))
        out: list[str] = []
        dropped: dict[str, str] = {}
        for ex in ordered:
            if ex not in allowed:
                dropped[ex] = f"不是 sn={self.sn}（{plat}）的执行通道"
                continue
            out.append(ex)

        if not out:
            raise NoRouteError(
                f"cap={event.capability_id} 对设备 {self.sn}（{plat}）无可用实现："
                f"{dropped or '该能力没有任何 implementation'}"
                f"（该设备通道={sorted(allowed)}）"
            )

        impl = by_executor[out[0]]
        impl_dict = impl.model_dump() if hasattr(impl, "model_dump") else dict(impl)
        return out, impl_dict

    def _device_hint(self, node: NodeSession) -> dict[str, Any]:
        """注入 Scout 执行时需要但不该回查的东西。

        消解了上游 `driver → server` 那 11 处 `DeviceService` 反查
        （MIGRATION.md §1 E1 附近）。
        """
        dev = node.devices.get(self.sn)
        plat = _device_platform(self.sn, node)
        hint: dict[str, Any] = {"platform": plat}
        if self.target_package:
            hint["target_package"] = self.target_package
        if plat in {"desktop", "mac", "win"}:
            from mino_nexus.services import run_store
            from mino_nexus.services.device_admission import owns_desktop
            task_id = self.task_id or self.run_id.split("::", 1)[0]
            task = run_store.get(task_id) if task_id else None
            target = (task or {}).get("device_targets", {}).get(self.sn) or {}
            hint["session_generation"] = target.get("session_generation") or (dev.session_generation if dev else "")
            if plat == "desktop":
                hint["os"] = dev.os if dev else ""
            hint["session_key"] = dev.session_key if dev else ""
            if task and owns_desktop(task_id, node.node_id):
                hint["desktop_lease"] = task_id
            return hint
        if plat in ("web", "browser", "playwright"):
            hint["headless"] = self.playwright_headless
            return hint
        if dev is not None:
            hint["model"] = dev.model
            serial = (dev.channels or {}).get("serial") or ""
            hint["adb_serial"] = serial or (self.sn if not self.sn.startswith("claw-") else "")
        else:
            hint["adb_serial"] = self.sn
        password = get_lock_password(self.sn)
        if password:
            hint["password"] = password
        return hint

    # ---------------- 发送与重试 ----------------

    async def _send_once(
        self,
        node: NodeSession,
        mtype: P.MsgType,
        payload: Any,
        timeout_sec: float,
        *,
        pad_sec: Optional[float] = None,
    ) -> Optional[P.Result]:
        from mino_nexus.services import run_store
        task_id = self.task_id or str(getattr(payload, "run_id", "") or self.run_id).split("::", 1)[0]
        task = await asyncio.to_thread(run_store.get, task_id) if task_id else None
        if task:
            target = (task.get("device_targets") or {}).get(self.sn) or {}
            allowed = task.get("allowed_node_ids")
            if (target.get("node_id") and target["node_id"] != node.node_id) or (allowed is not None and node.node_id not in allowed):
                raise NoRouteError("device_owner_drift: 设备已不属于准入时固定节点")
            from mino_nexus.services.execution_policy import remaining
            case = next((row for row in task.get("cases") or [] if row.get("sn") == self.sn and row.get("status") == "running"), None)
            left = remaining(task, case)
            if left is not None:
                if left <= 0:
                    raise NoRouteError("execution_deadline_exceeded")
                timeout_sec = min(timeout_sec, left)
                payload.timeout_sec = min(payload.timeout_sec, timeout_sec)
                pad_sec = min(self.timeout_pad_sec if pad_sec is None else pad_sec, max(0, left - timeout_sec))
        deadline = timeout_sec + (self.timeout_pad_sec if pad_sec is None else pad_sec)
        assert node.send is not None
        return await node.send(mtype, payload, timeout=deadline)

    async def _send_with_retry(
        self, node: NodeSession, mtype: P.MsgType, payload: Any, timeout_sec: float
    ) -> Optional[P.Result]:
        """协议 §6：超时后按同键重发一次；Scout 有幂等缓存，不会重复操作设备。"""
        res = await self._send_once(node, mtype, payload, timeout_sec)
        if res is not None:
            return res
        if isinstance(payload, P.Execute) and payload.step_idx > 0:
            return None
        SLog.w(TAG, f"[{node.node_id}] {mtype.value} 超时，按同键重发一次（Scout 侧幂等）")
        return await self._send_once(node, mtype, payload, timeout_sec)


# ---------------- 小工具 ----------------


def executors_for_platform(platform: str) -> tuple[str, ...]:
    """这台设备类型允许的通道。家族互斥：Web 没有 adb，安卓/iOS 没有 playwright。"""
    plat = str(platform or "").strip().lower()
    if plat in ("web", "browser", "playwright"):
        return ("playwright",)
    if plat in ("ios",):
        return ("ios_wda", "remote")
    if plat in {"desktop", "mac", "win"}:
        return ("desktop",)
    if plat == "android":
        return ("adb", "remote")
    return ()


def _binding_wire_fields(projection):
    if projection is None:
        return {}
    return {key: projection[key] for key in ('execution_contract_version', 'binding_id', 'binding_revision',
        'input_contract_version', 'operation_ref', 'node_connection_generation',
        'scout_process_generation', 'capability_revision')}


def _device_platform(sn: str, node: NodeSession) -> str:
    from mino_nexus.runtime.run_context import is_web_slot

    if is_web_slot(sn):
        return "web"
    dev = node.devices.get(sn)
    plat = str(getattr(dev, "platform", "") or "").strip().lower()
    if plat:
        return plat
    return "unknown"


_CHANNEL_EXEC = (("desktop", "desktop"), ("playwright", "playwright"), ("adb", "adb"), ("remote", "remote"), ("ios", "ios_wda"))
_ONLINE = frozenset({"connected", "online", "available", "authenticated"})


def _channel_state(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("state") or raw.get("status") or "").strip().lower()
    return str(raw or "").strip().lower()


def executors_for_device(sn: str, node: NodeSession) -> list[str]:
    """这台 sn 此刻能走的 executor。

    节点上 adb 插件在（哪怕手机全离线）不构成「给 Web 槽发 adb」的理由。
    """
    plat = _device_platform(sn, node)
    family = list(executors_for_platform(plat))
    avail = set(node.available_executors())
    ordered = [ex for ex in family if ex in avail]
    dev = node.devices.get(sn)
    connected: list[str] = []
    if dev is not None and isinstance(dev.channels, dict):
        for ch, ex in _CHANNEL_EXEC:
            if ex not in family:
                continue
            if _channel_state((dev.channels or {}).get(ch)) in _ONLINE:
                connected.append(ex)
    if plat in {"desktop", "mac", "win"}:
        return [ex for ex in ordered if ex in connected] if dev and dev.desktop_state == "ready" else []
    if connected:
        return [ex for ex in ordered if ex in set(connected)]
    return ordered


def _timeout_for(event: PlanEvent) -> float:
    params = event.params or {}
    cap = str(event.capability_id or "")
    if cap == "install_apk":
        return 300.0
    if cap in {"launch_app", "open_url", "open_app"}:
        return 30.0
    if cap in {"tap_element", "input_text", "tap_xy", "swipe", "scroll", "long_press"}:
        return _MUTATE_TIMEOUT_SEC
    ms = params.get("duration_ms") or params.get("ms")
    if ms:
        return max(30.0, float(ms) / 1000.0 + 10.0)
    return _DEFAULT_ACTION_TIMEOUT_SEC


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _fail(
    event: PlanEvent,
    started: str,
    t0: float,
    msg: str,
    *,
    executor_used: str,
    local_reason: str = "",
    raw_response: dict[str, Any] | None = None,
) -> EventResult:
    SLog.w(TAG, msg)
    raw: dict[str, Any] = dict(raw_response or {})
    if local_reason:
        raw["local_reason"] = local_reason
    return EventResult(
        seq=event.seq, capability_id=event.capability_id,
        event_kind=event.event_kind or event.capability_id,
        status=EventStatus.FAIL, executor_used=executor_used,
        elapsed_ms=int((time.time() - t0) * 1000),
        summary=msg, error=msg,
        ai_reasoning=event.ai_reasoning or "",
        plan_event=event.model_dump(exclude_none=True),
        started_at=started, finished_at=_now_iso(),
        raw_response=raw,
    )


def _request_metadata(node, request) -> dict[str, Any]:
    from mino_nexus.core.build_info import identity
    return {"node_id": node.node_id, "scout_version": node.scout_version,
            "nexus_identity": identity(),
            "node_connection_generation": node.node_connection_generation,
            "scout_process_generation": node.scout_process_generation,
            "sn": request.sn, "platform": request.platform,
            "os": str((request.device_hint or {}).get("os") or ""),
            "run_id": request.run_id, "step_idx": request.step_idx,
            **({key: getattr(request, key) for key in ("binding_id", "binding_revision", "operation_ref",
                "execution_contract_version", "node_connection_generation", "scout_process_generation", "capability_revision")}
               if request.binding_id else {})}


def _action_ref(request: P.Execute) -> dict[str, Any]:
    return {"run_id": request.run_id, "step_idx": request.step_idx}


def _event_result_from(event: PlanEvent, res: P.Result, started: str, *, node=None, request=None) -> EventResult:
    from mino_nexus.core.secret_redaction import sanitize, remember
    raw = dict(res.extra or {})
    if res.data:
        raw = {**raw, **dict(res.data), "data": dict(res.data)}
    if raw.get("sensitive"):
        remember(event.params.get("text"))
        event.params["sensitive"] = True
    if node is not None and request is not None:
        raw["scout"] = _request_metadata(node, request)
        receipt = (res.extra or {}).get("execution_identity")
        if isinstance(receipt, dict):
            raw["scout"]["execution_identity"] = receipt
        if request.step_idx > 0:
            raw["action_ref"] = _action_ref(request)
            raw["logical_seq"] = event.seq
    return EventResult(
        seq=event.seq, capability_id=event.capability_id,
        event_kind=event.event_kind or event.capability_id,
        status=res.status, executor_used=res.executor_used,
        elapsed_ms=res.elapsed_ms, summary=sanitize(res.summary), error=sanitize(res.error),
        ai_reasoning=event.ai_reasoning or "",
        plan_event=sanitize(event.model_dump(exclude_none=True)),
        raw_response=sanitize(raw),
        attempts=[
            {"executor": a.executor, "status": a.status.value if hasattr(a.status, "value") else a.status,
             "elapsed_ms": a.elapsed_ms, "error": a.error}
            for a in (res.attempts or [])
        ],
        started_at=started, finished_at=_now_iso(),
    )


def _dispatch_local_in_runtime(event: PlanEvent, *, ctx: Any, **kwargs: Any) -> EventResult:
    from mino_nexus.loop.local_executors import dispatch_local
    from mino_nexus.services.control_registry import registry_runtime

    with registry_runtime(getattr(ctx, "execution_mode", "legacy")):
        return dispatch_local(event, ctx=ctx, **kwargs)


def _run_sync(coro):
    """同步壳。agent_loop 在后台线程；Scout WS 在主 loop —— 必须桥接。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        main = _MAIN_LOOP
        if main is not None and main.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, main)
            return fut.result()
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError(
        "当前线程已有事件循环：请改用 dispatch_async / observe_async，"
        "或把同步循环放进 asyncio.to_thread"
    )
