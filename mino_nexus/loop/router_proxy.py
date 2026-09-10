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

# 这四个 capability 由 Nexus 本地 executor 处理，**不出网**（CLAUDE.md §2.3）。
# 搞错会让 Scout 收到它 supports() 返回 False 的能力，白跑一圈 fallback。
LOCAL_CAP_PREFIXES = ("human_", "recover_")
LOCAL_CAPS = frozenset({
    "assert_visual",
    "persona_subtask",
    "wait_ms",
    "wait_screen_ready",
    "relogin",
    "lease_account",
    "check_run_env",
})


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
        timeout_pad_sec: float = 2.0,
        target_package: str = "",
        playwright_headless: bool = True,
    ):
        self.sn = sn
        self.run_id = run_id
        self.target_package = str(target_package or "")
        self.playwright_headless = bool(playwright_headless)
        # 协议 §6：等 RESULT 用 payload.timeout_sec + 宽限
        self.timeout_pad_sec = timeout_pad_sec

    # ---------------- 对外：与上游 dispatch 同签名 ----------------

    def dispatch(
        self,
        event: PlanEvent,
        *,
        run_id: str = "",
        step_idx: int = -1,
        **_ignored: Any,
    ) -> EventResult:
        """同步接口 —— 上游循环是同步代码，这里内部跑事件循环。"""
        return _run_sync(self.dispatch_async(event, run_id=run_id or self.run_id, step_idx=step_idx))

    def observe(
        self,
        kind: str = "screenshot",
        *,
        prefer: Optional[tuple[str, ...]] = None,
        force_fresh: bool = True,
        timeout_sec: float = _OBSERVE_TIMEOUT_SEC,
        compress_ratio: float = 2.0,
    ) -> CapturedScreen:
        """替代上游的 `capture_screen(ctx, ...)`，返回值保持 CapturedScreen 形状。"""
        return _run_sync(
            self.observe_async(
                kind, prefer=prefer, force_fresh=force_fresh,
                timeout_sec=timeout_sec, compress_ratio=compress_ratio,
            )
        )

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
        self, event: PlanEvent, *, run_id: str = "", step_idx: int = -1
    ) -> EventResult:
        started = _now_iso()
        t0 = time.time()

        if is_local_cap(event.capability_id):
            return _fail(
                event, started, t0,
                f"cap={event.capability_id} 应由 Nexus 本地 executor 处理，不该走 RouterProxy"
                "（CLAUDE.md §2.3；local_executors 尚未搬迁）",
                executor_used="router_proxy",
                local_reason="local_cap_misrouted",
            )

        node, why = get_registry().resolve(self.sn)
        if node is None:
            return _fail(event, started, t0, why, executor_used="router_proxy")

        try:
            order, impl = self._plan_route(event, node)
        except NoRouteError as exc:
            msg = str(exc)
            reason = "no_impl_for_device" if "无可用实现" in msg else "cap_not_in_catalog"
            return _fail(event, started, t0, msg, executor_used="router_proxy", local_reason=reason)

        req = P.Execute(
            run_id=run_id or self.run_id,
            step_idx=step_idx if step_idx >= 0 else event.seq,
            sn=self.sn,
            capability_id=event.capability_id,
            params=dict(event.params or {}),
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
            )
        return _event_result_from(event, res, started)

    async def observe_async(
        self,
        kind: str = "screenshot",
        *,
        prefer: Optional[tuple[str, ...]] = None,
        force_fresh: bool = True,
        timeout_sec: float = _OBSERVE_TIMEOUT_SEC,
        compress_ratio: float = 2.0,
    ) -> CapturedScreen:
        node, why = get_registry().resolve(self.sn)
        if node is None:
            return CapturedScreen(ok=False, source="router_proxy", error=why)

        plat = _device_platform(self.sn, node)
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
        cap = catalog.get_capability(event.capability_id)
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
    return ("adb", "remote")


def _device_platform(sn: str, node: NodeSession) -> str:
    from mino_nexus.runtime.run_context import is_web_slot

    dev = node.devices.get(sn)
    plat = str(getattr(dev, "platform", "") or "").strip().lower()
    if plat:
        return plat
    if is_web_slot(sn):
        return "web"
    return "android"


_CHANNEL_EXEC = (("playwright", "playwright"), ("adb", "adb"), ("remote", "remote"), ("ios", "ios_wda"))
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
) -> EventResult:
    SLog.w(TAG, msg)
    raw: dict[str, Any] = {}
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
        raw_response=raw or None,
    )


def _event_result_from(event: PlanEvent, res: P.Result, started: str) -> EventResult:
    raw = dict(res.extra or {})
    if res.data:
        raw = {**raw, **dict(res.data), "data": dict(res.data)}
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
