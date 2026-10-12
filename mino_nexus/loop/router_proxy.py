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
import time
from typing import Any, Optional

from mino_nexus.core import protocol as P
from mino_nexus.catalog import registry as catalog
from mino_nexus.catalog.exec_classes import LOCAL_EXECUTOR_IDS as LOCAL_CAPS
from mino_nexus.services.device_secrets import get_lock_password
from mino_nexus.core.log import SLog
from mino_nexus.services.node_registry import NodeSession, get_registry
from mino_nexus.core.schemas import CapturedScreen, EventResult, EventStatus, PlanEvent

TAG = "RouterProxy"

# case_runner 在后台线程跑同步 agent_loop，Scout WS 却在 FastAPI 主 loop。
# 若用 asyncio.run() 临时 loop 去 await node.send，RESULT 回在主 loop 上，
# waiter 永远等不到 → observe/action 每次都顶满 timeout+pad（17s / 35s）。
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
_OBSERVE_TIMEOUT_SEC = 8.0
_OBSERVE_PAD_SEC = 2.0
_MUTATE_TIMEOUT_SEC = 10.0
_DEFAULT_ACTION_TIMEOUT_SEC = 15.0


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
    ):
        from mino_nexus.services.access_scope import require_owned_device
        require_owned_device(sn)
        self.sn = sn
        self.run_id = run_id
        self.task_id = str(task_id or "").strip()
        self.target_package = str(target_package or "")
        self.playwright_headless = bool(playwright_headless)
        # 协议 §6：等 RESULT 用 payload.timeout_sec + 宽限
        self.timeout_pad_sec = timeout_pad_sec

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
            note_channel_capture_ratio(float(compress_ratio or 1.0), authoritative=plat == "desktop")
        shot = _run_sync(
            self.observe_async(
                kind, prefer=prefer, force_fresh=force_fresh,
                timeout_sec=timeout_sec, compress_ratio=compress_ratio,
            )
        )
        if kind == "screenshot":
            note_channel_capture_ratio(
                float((shot.remote_detail or {}).get("compress_ratio") or compress_ratio or 1.0),
                authoritative=plat == "desktop",
            )
        return shot

    def notify_scout_cancel_run(self, run_id: str = "") -> None:
        """通知 Scout 结束 run：清幂等缓存并回收 Web 环境（best-effort）。"""
        _run_sync(self.notify_scout_cancel_run_async(run_id))

    async def notify_scout_cancel_run_async(self, run_id: str = "") -> None:
        node, why = get_registry().resolve(self.sn)
        if node is None:
            SLog.w(TAG, f"cancel_run 跳过 sn={self.sn}: {why}")
            return
        rid = run_id or self.run_id
        req = P.Execute(
            run_id=rid,
            step_idx=-1,
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

        if self._task_aborted():
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

        if str(event.capability_id or "").startswith("plugin."):
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
            from mino_nexus.loop.local_executors import dispatch_local

            ctx = kwargs.get("ctx")
            # 本地能力里还会再 dispatch（例如 relogin → read_web_auth）。
            # 放在线程里，事件循环才能接着把这次 EXECUTE 发给 Scout。
            return await asyncio.to_thread(
                dispatch_local,
                event,
                ctx=ctx,
                router=self,
                target_package=self.target_package,
            )

        node, why = get_registry().resolve(self.sn)
        if node is None:
            return _fail(event, started, t0, why, executor_used="router_proxy")

        if _device_platform(self.sn, node) == "desktop":
            from mino_nexus.services.device_admission import owns_desktop
            dev = node.devices.get(self.sn)
            hint = self._device_hint(node)
            task_id = self.task_id or (run_id or self.run_id).split("::", 1)[0]
            if not dev or dev.desktop_state != "ready" or hint.get("session_generation") != dev.session_generation or not owns_desktop(task_id, node.node_id):
                return _fail(event, started, t0, "桌面未就绪、会话已变化或没有独占租约", executor_used="router_proxy", local_reason="desktop_not_admitted")
        try:
            order, impl = self._plan_route(event, node)
        except NoRouteError as exc:
            msg = str(exc)
            reason = "no_impl_for_device" if "无可用实现" in msg else "cap_not_in_catalog"
            return _fail(event, started, t0, msg, executor_used="router_proxy", local_reason=reason)

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
        from mino_nexus.action_space.assemble import stamp_scheme_params
        from mino_nexus.action_space.scheme import action_scheme

        ctx = kwargs.get("ctx")
        scheme = action_scheme(ctx) if ctx is not None else str(getattr(self, "action_scheme", "") or "visual")
        focus_ref = str(params.get("focus_ref") or "")
        params = stamp_scheme_params(event.capability_id, params, scheme)
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
        if _device_platform(self.sn, node) == "desktop" and event.capability_id in ("tap_element", "tap_xy"):
            params["focus_ref"] = f"focus:{run_id or self.run_id}:{event.seq}"
        event.params = params

        req = P.Execute(
            run_id=run_id or self.run_id,
            step_idx=step_idx if step_idx >= 0 else event.seq,
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
        )

        res = await self._send_with_retry(node, P.MsgType.EXECUTE, req, req.timeout_sec)
        if res is None:
            return _fail(
                event, started, t0,
                f"scout timeout（node={node.node_id}，重发一次仍无 RESULT）",
                executor_used="router_proxy",
                raw_response={"scout": _request_metadata(node, req)},
            )
        return _event_result_from(event, res, started, node=node, request=req)

    async def _dispatch_host_plugin(
        self,
        event: PlanEvent,
        *,
        started: str,
        t0: float,
        run_id: str,
        step_idx: int,
    ) -> EventResult:
        """具名插件能力。参数里没有密钥，密钥在执行节点自己的保险库。"""
        node, why = get_registry().resolve(self.sn)
        if node is None:
            return _fail(event, started, t0, why, executor_used="router_proxy")
        params = dict(event.params or {})
        try:
            wait_ms = int(params.get("max_wait_ms") or 30_000)
        except (TypeError, ValueError):
            wait_ms = 30_000
        timeout = min(600.0, max(20.0, wait_ms / 1000.0 + 15.0))
        req = P.Execute(
            run_id=run_id or self.run_id,
            step_idx=-1,
            sn=self.sn,
            capability_id=event.capability_id,
            params=params,
            executor_order=[],
            low_level={},
            selected_impl={},
            device_hint={},
            timeout_sec=timeout,
            device_id=self.sn,
            platform=_device_platform(self.sn, node),
        )
        res = await self._send_with_retry(node, P.MsgType.EXECUTE, req, req.timeout_sec)
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
    ) -> CapturedScreen:
        if self._task_aborted():
            return CapturedScreen(ok=False, source="router_proxy", error="任务已取消")
        node, why = get_registry().resolve(self.sn)
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

            note_channel_capture_ratio(float(compress_ratio or 1.0), authoritative=plat == "desktop")
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
            f"[{(self.run_id or '')[:8]}] observe {kind} sn={self.sn} plat={plat} order={list(prefer)}",
        )

        cap = P.OBSERVE_CAPS.get(kind, kind)
        req = P.Execute(
            run_id=self.run_id,
            step_idx=-1,
            sn=self.sn,
            capability_id=cap,
            params={
                "force_fresh": force_fresh,
                "compress_ratio": compress_ratio,
            },
            executor_order=list(prefer),
            device_hint=self._device_hint(node),
            timeout_sec=timeout_sec,
            device_id=self.sn,
            platform=_device_platform(self.sn, node),
        )
        # 截图每轮都要走；observe 单独用更紧的 pad，且不重试（重试会把一步拖到 ~40s+）。
        res = await self._send_once(
            node, P.MsgType.EXECUTE, req, timeout_sec, pad_sec=_OBSERVE_PAD_SEC,
        )
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
                authoritative=plat == "desktop",
            )
        scout_ms = int(res.elapsed_ms or 0)
        if scout_ms and scout_ms < 5000:
            SLog.d(TAG, f"[{(self.run_id or '')[:8]}] observe ok scout={scout_ms}ms cap={cap}")
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
        if plat == "desktop":
            from mino_nexus.services.desktop_catalog import CAPABILITY_IDS, capabilities

            if event.capability_id in CAPABILITY_IDS:
                dev = node.devices.get(self.sn)
                reported = list(getattr(dev, "capabilities", None) or [])
                os_name = str(getattr(dev, "os", "") or "")
                cap = next((candidate for candidate in capabilities(reported, os_name)
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
        SLog.i(
            TAG,
            f"[{self.run_id[:8]}] cap={event.capability_id} order={out} impl={impl_dict.get('id')}"
            + (f" dropped={dropped}" if dropped else ""),
        )
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
        if plat == "desktop":
            from mino_nexus.services import run_store
            from mino_nexus.services.device_admission import owns_desktop
            task_id = self.task_id or self.run_id.split("::", 1)[0]
            task = run_store.get(task_id) if task_id else None
            target = (task or {}).get("device_targets", {}).get(self.sn) or {}
            hint["session_generation"] = target.get("session_generation") or (dev.session_generation if dev else "")
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
    if plat == "desktop":
        return ("desktop",)
    if plat == "android":
        return ("adb", "remote")
    return ()


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
    if plat == "desktop":
        return [ex for ex in ordered if ex in connected] if dev and dev.desktop_state == "ready" else []
    if connected:
        return [ex for ex in ordered if ex in set(connected)] or list(connected)
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
    return {"node_id": node.node_id, "scout_version": node.scout_version,
            "sn": request.sn, "platform": request.platform,
            "os": str((request.device_hint or {}).get("os") or ""),
            "run_id": request.run_id, "step_idx": request.step_idx}


def _event_result_from(event: PlanEvent, res: P.Result, started: str, *, node=None, request=None) -> EventResult:
    raw = dict(res.extra or {})
    if res.data:
        raw = {**raw, **dict(res.data), "data": dict(res.data)}
    if node is not None and request is not None:
        raw["scout"] = _request_metadata(node, request)
    return EventResult(
        seq=event.seq, capability_id=event.capability_id,
        event_kind=event.event_kind or event.capability_id,
        status=res.status, executor_used=res.executor_used,
        elapsed_ms=res.elapsed_ms, summary=res.summary, error=res.error,
        ai_reasoning=event.ai_reasoning or "",
        plan_event=event.model_dump(exclude_none=True),
        raw_response=raw,
        attempts=[
            {"executor": a.executor, "status": a.status.value if hasattr(a.status, "value") else a.status,
             "elapsed_ms": a.elapsed_ms, "error": a.error}
            for a in (res.attempts or [])
        ],
        started_at=started, finished_at=_now_iso(),
    )


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
