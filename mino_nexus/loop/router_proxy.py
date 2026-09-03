"""RouterProxy：签名兼容上游 `CapabilityRouter`，内部把动作发给 Scout。

这是本次拆分**最省力的一处**（CLAUDE.md §2.2）。上游 `agent_executor.py`(3,533 行)、
`recovery.py`、`orchestrator.py` 只依赖一个签名：

    result: EventResult = router.dispatch(event, ctx, ...)

本类提供同签名实现，于是那几千行循环代码搬过来**基本不用改** —— 改的是注入进去的对象。

职责（选路在 Nexus，执行在 Scout）：
  1. 查能力目录：这条 cap 在该节点的 connectivity 下允许哪些 executor
  2. 合成 executor_order：expected_executor → fallback_executors → 菜单兜底
  3. 从 YAML 取 low_level / selected_impl
  4. 注入 device_hint（sn → adb_serial / password）
  5. 发 EXECUTE，等 RESULT；超时按协议 §6 重发一次
  6. RESULT → EventResult（五态、attempts、executor_used 原样透传）

**循环代码不该知道 Scout 存在。** 网络、重试、节点选择都在这里。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from mino_nexus import protocol as P
from mino_nexus.catalog import registry as catalog
from mino_nexus.device_secrets import get_lock_password
from mino_nexus.log import SLog
from mino_nexus.node_registry import NodeSession, get_registry
from mino_nexus.schemas import CapturedScreen, EventResult, EventStatus, PlanEvent

TAG = "RouterProxy"

# 这四个 capability 由 Nexus 本地 executor 处理，**不出网**（CLAUDE.md §2.3）。
# 搞错会让 Scout 收到它 supports() 返回 False 的能力，白跑一圈 fallback。
LOCAL_CAP_PREFIXES = ("human_",)
LOCAL_CAPS = frozenset({
    "assert_visual",        # vlm_executor —— 是 LLM 调用
    "persona_subtask",      # ai_persona_executor —— 拟人化编排
    "wait_ms",              # internal_executor —— 不需要设备
    "wait_screen_ready",
})


def is_local_cap(capability_id: str) -> bool:
    return capability_id in LOCAL_CAPS or capability_id.startswith(LOCAL_CAP_PREFIXES)


class NoRouteError(Exception):
    """派不出去。原因必须说清是哪一种（NODE_REGISTRY.md §4：不要静默排队）。"""


class RouterProxy:
    def __init__(self, sn: str, *, run_id: str = "", timeout_pad_sec: float = 5.0):
        self.sn = sn
        self.run_id = run_id
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
        timeout_sec: float = 15.0,
        compress_ratio: float = 2.0,
    ) -> CapturedScreen:
        """替代上游的 `capture_screen(ctx, ...)`，返回值保持 CapturedScreen 形状。"""
        return _run_sync(
            self.observe_async(
                kind, prefer=prefer, force_fresh=force_fresh,
                timeout_sec=timeout_sec, compress_ratio=compress_ratio,
            )
        )

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
            )

        node, why = get_registry().resolve(self.sn)
        if node is None:
            return _fail(event, started, t0, why, executor_used="router_proxy")

        try:
            order, impl = self._plan_route(event, node)
        except NoRouteError as exc:
            return _fail(event, started, t0, str(exc), executor_used="router_proxy")

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
        timeout_sec: float = 15.0,
        compress_ratio: float = 2.0,
    ) -> CapturedScreen:
        node, why = get_registry().resolve(self.sn)
        if node is None:
            return CapturedScreen(ok=False, source="router_proxy", error=why)

        if prefer is None:
            prefer = _default_prefer(node)

        req = P.Observe(
            run_id=self.run_id, sn=self.sn, kind=P.ObserveKind(kind),
            prefer=list(prefer), force_fresh=force_fresh,
            timeout_sec=timeout_sec, compress_ratio=compress_ratio,
        )
        res = await self._send_with_retry(node, P.MsgType.OBSERVE, req, timeout_sec)
        if res is None:
            return CapturedScreen(
                ok=False, source="router_proxy",
                error=f"scout timeout（node={node.node_id}，OBSERVE {kind}）",
            )
        if res.status is not EventStatus.PASS:
            return CapturedScreen(ok=False, source=res.source or "", error=res.error or res.summary)
        return CapturedScreen(
            ok=True, source=res.source, image_base64=res.image_base64,
            image_mime=res.image_mime, width=res.width, height=res.height,
            elapsed_ms=res.elapsed_ms, remote_detail=dict(res.extra or {}),
        )

    # ---------------- 选路 ----------------

    def _plan_route(self, event: PlanEvent, node: NodeSession) -> tuple[list[str], dict[str, Any]]:
        """算 executor_order + 选中的 implementation。

        对齐上游 `router._executor_order()` 的优先级：
          1. event.expected_executor（AI 选的）
          2. event.fallback_executors
          3. 该 cap 在菜单里允许的其它 executor（按 cost 升序）
        再按**该节点上报的可用 executor** 过滤 —— 这是拆分后的关键差异：
        连通性来自 Scout 的 manifest，不是 Nexus 自己探的。
        """
        cap = catalog.get_capability(event.capability_id)
        if cap is None:
            raise NoRouteError(
                f"能力目录里没有 cap={event.capability_id}"
                "（AI 选了菜单外的能力？或 plugins/ 缺这条 yaml）"
            )

        avail = set(node.available_executors())
        provides = node.provides()

        # 菜单顺序：按 cost 升序（YAML 里已排，这里稳妥再排一次）
        impls = sorted(cap.implementations or [], key=lambda i: getattr(i, "cost", 99))
        by_executor: dict[str, Any] = {}
        for impl in impls:
            by_executor.setdefault(impl.executor, impl)

        ordered: list[str] = []
        for ex in [event.expected_executor, *(event.fallback_executors or [])]:
            if ex and ex not in ordered and ex in by_executor:
                ordered.append(ex)
        for ex in by_executor:
            if ex not in ordered:
                ordered.append(ex)

        out: list[str] = []
        dropped: dict[str, str] = {}
        for ex in ordered:
            if ex not in avail:
                dropped[ex] = "该节点未上报此 executor 可用"
                continue
            need = set(getattr(by_executor[ex], "requires_caps", None) or [])
            missing = need - provides
            if missing:
                dropped[ex] = f"缺 abstract cap {sorted(missing)}"
                continue
            out.append(ex)

        if not out:
            raise NoRouteError(
                f"cap={event.capability_id} 在节点 {node.node_id} 上无可用实现："
                f"{dropped or '该能力没有任何 implementation'}"
                f"（节点可用 executor={sorted(avail)}）"
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
        hint: dict[str, Any] = {"platform": dev.platform if dev else "android"}
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

    async def _send_with_retry(
        self, node: NodeSession, mtype: P.MsgType, payload: Any, timeout_sec: float
    ) -> Optional[P.Result]:
        """协议 §6：超时后按同键重发一次；Scout 有幂等缓存，不会重复操作设备。"""
        deadline = timeout_sec + self.timeout_pad_sec
        assert node.send is not None
        res = await node.send(mtype, payload, timeout=deadline)
        if res is not None:
            return res
        SLog.w(TAG, f"[{node.node_id}] {mtype.value} 超时，按同键重发一次（Scout 侧幂等）")
        return await node.send(mtype, payload, timeout=deadline)


# ---------------- 小工具 ----------------


def _default_prefer(node: NodeSession) -> tuple[str, ...]:
    avail = node.available_executors()
    if "playwright" in avail and len(avail) == 1:
        return ("playwright",)
    return tuple(ex for ex in ("adb", "remote", "ios_wda", "playwright") if ex in avail)


def _timeout_for(event: PlanEvent) -> float:
    params = event.params or {}
    if event.capability_id == "install_apk":
        return 300.0
    ms = params.get("duration_ms") or params.get("ms")
    if ms:
        return max(30.0, float(ms) / 1000.0 + 10.0)
    return 30.0


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _fail(event: PlanEvent, started: str, t0: float, msg: str, *, executor_used: str) -> EventResult:
    SLog.w(TAG, msg)
    return EventResult(
        seq=event.seq, capability_id=event.capability_id,
        event_kind=event.event_kind or event.capability_id,
        status=EventStatus.FAIL, executor_used=executor_used,
        elapsed_ms=int((time.time() - t0) * 1000),
        summary=msg, error=msg,
        ai_reasoning=event.ai_reasoning or "",
        plan_event=event.model_dump(exclude_none=True),
        started_at=started, finished_at=_now_iso(),
    )


def _event_result_from(event: PlanEvent, res: P.Result, started: str) -> EventResult:
    return EventResult(
        seq=event.seq, capability_id=event.capability_id,
        event_kind=event.event_kind or event.capability_id,
        status=res.status, executor_used=res.executor_used,
        elapsed_ms=res.elapsed_ms, summary=res.summary, error=res.error,
        ai_reasoning=event.ai_reasoning or "",
        plan_event=event.model_dump(exclude_none=True),
        raw_response=dict(res.extra or {}),
        attempts=[
            {"executor": a.executor, "status": a.status.value if hasattr(a.status, "value") else a.status,
             "elapsed_ms": a.elapsed_ms, "error": a.error}
            for a in (res.attempts or [])
        ],
        started_at=started, finished_at=_now_iso(),
    )


def _run_sync(coro):
    """同步壳。上游循环是同步代码，而 transport 是 asyncio。

    在已有事件循环的线程里（如 FastAPI handler）不能 run_until_complete ——
    那种场景应直接 await *_async 版本。这里显式报错，别悄悄死锁。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError(
        "当前线程已有事件循环：请改用 dispatch_async / observe_async，"
        "或把同步循环放进 asyncio.to_thread"
    )
