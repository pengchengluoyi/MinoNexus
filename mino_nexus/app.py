"""FastAPI 应用。

UI（Console / Studio）只打这里。Scout 走 `WS /node`。
登录与运行态的契约见 `docs/HTTP.md`。`/debug/*` 仍是脚手架。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Optional

from mino_nexus.routers.deps import current_session
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mino_nexus.core import protocol as P
from mino_nexus.catalog import registry as catalog
from mino_nexus.core.client_gate import ClientGateMiddleware
from mino_nexus.core.log import SLog
from mino_nexus.loop.router_proxy import RouterProxy, is_local_cap
from mino_nexus.services.node_registry import get_registry

from mino_nexus.core.paths import data_dir
from mino_nexus.routers import (
    rAppAutomation,
    rAppIntel,
    rAssistant,
    rAuth,
    rCaseRunner,
    rDevice,
    rDocs,
    rFlowBlocks,
    rMcp,
    rMe,
    rNavFsm,
    rObserve,
    rPacks,
    rProject,
    rProjectCases,
    rReleases,
    rRuntime,
    rSettings,
    rSys,
    rTask,
)
from mino_nexus.core.schemas import PlanEvent
from mino_nexus.websocket import node as node_ws
from mino_nexus.websocket import observers as ui_ws

TAG = "App"

NEXUS_VERSION = node_ws.NEXUS_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    from mino_nexus.core.database import ensure_db
    from mino_nexus.core.mdns import configure_proxy_bypass, register_beacon, unregister_beacon

    ensure_db()
    from mino_nexus.core.build_info import initialize_instance_id
    initialize_instance_id()
    from mino_nexus.services.auth_store import ensure_seed_users
    from mino_nexus.services.run_store import reconcile_stale_running_runs

    ensure_seed_users()
    stale = reconcile_stale_running_runs()
    if stale:
        SLog.w(TAG, f"启动清扫 {len(stale)} 条僵尸 running 任务: {stale[:5]}")
        from mino_nexus.services.device_resource_lease import release_device_leases_for_run

        for rid in stale:
            release_device_leases_for_run(rid)
    from mino_nexus.services.device_resource_lease import purge_expired_device_leases

    purged = purge_expired_device_leases()
    if purged:
        SLog.i(TAG, f"清扫过期设备租约 {purged} 条")
    configure_proxy_bypass()
    loop = asyncio.get_running_loop()
    ui_ws.set_loop(loop)
    from mino_nexus.loop.router_proxy import set_main_loop

    set_main_loop(loop)
    handle = await register_beacon()
    app.state.mdns = handle

    async def _doc_sync_loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                from mino_nexus.services.doc_sync import doc_sync_tick

                n = doc_sync_tick()
                if n:
                    SLog.i(TAG, f"飞书文档定时同步 {n} 条")
            except Exception as exc:
                SLog.w(TAG, f"doc sync tick: {exc}")

    doc_sync_task = asyncio.create_task(_doc_sync_loop())
    async def _assistant_delivery_loop() -> None:
        from mino_nexus.services.assistant_notify import retry_pending_notifications
        while True:
            try:
                await asyncio.to_thread(retry_pending_notifications)
            except Exception as exc:
                SLog.w(TAG, f"assistant delivery tick: {type(exc).__name__}")
            await asyncio.sleep(30)

    delivery_task = asyncio.create_task(_assistant_delivery_loop())
    async def _case_compile_loop() -> None:
        import uuid
        from mino_nexus.services.case_compilation import interrupt_expired, run_one
        worker_id = "nexus-" + uuid.uuid4().hex
        while True:
            try:
                await asyncio.to_thread(interrupt_expired)
                worked = await asyncio.to_thread(run_one, worker_id)
            except Exception as exc:
                SLog.w(TAG, f"case compile tick: {type(exc).__name__}")
                worked = False
            await asyncio.sleep(0.2 if worked else 2)

    compile_task = asyncio.create_task(_case_compile_loop())
    yield
    compile_task.cancel()
    doc_sync_task.cancel()
    delivery_task.cancel()
    await asyncio.gather(compile_task, doc_sync_task, delivery_task, return_exceptions=True)
    await unregister_beacon(handle)


# ---------------- 请求模型 ----------------
# 必须**模块级**定义。本文件有 `from __future__ import annotations`，函数注解是字符串，
# FastAPI 拿模块 globals 去 eval —— 定义在 create_app() 内部会 NameError，
# 整个服务起不来。
# 别再挪回函数里。


class ObserveBody(BaseModel):
    sn: str
    kind: str = "screenshot"
    prefer: Optional[list[str]] = None
    timeout_sec: float = 15.0
    compress_ratio: float = 2.0


class ExecuteBody(BaseModel):
    sn: str
    capability_id: str
    params: dict[str, Any] = {}
    expected_executor: str = ""
    fallback_executors: list[str] = []
    step_idx: int = 1
    # 每次调用换一个 run_id：Scout 对 (run_id, step_idx) 做幂等，
    # 固定值会让第二次起全部命中缓存、回上一次的结果。
    run_id: str = ""


def create_app() -> FastAPI:
    app = FastAPI(title="MinoNexus", version=NEXUS_VERSION, lifespan=lifespan)

    from mino_nexus.core.execution_user import ExecutionUserMiddleware

    app.add_middleware(ExecutionUserMiddleware)
    app.add_middleware(ClientGateMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from mino_nexus.core.maintenance_gate import MaintenanceMiddleware
    app.add_middleware(MaintenanceMiddleware)
    from mino_nexus.routers import rNexusUpdate, rWorkbenchUpdate
    app.include_router(rNexusUpdate.router)
    app.include_router(rWorkbenchUpdate.router)
    app.include_router(rSys.router)
    app.include_router(rAuth.router)
    app.include_router(rMe.router)
    app.include_router(rRuntime.router)
    app.include_router(rReleases.router)
    app.include_router(rDevice.router)
    app.include_router(rCaseRunner.router)
    app.include_router(rObserve.router)
    app.include_router(rSettings.router)
    app.include_router(rDocs.router)
    app.include_router(rPacks.router)
    app.include_router(rProject.router)
    app.include_router(rProjectCases.router)
    app.include_router(rAppAutomation.router)
    app.include_router(rNavFsm.router)
    app.include_router(rFlowBlocks.router)
    app.include_router(rAppIntel.router)
    app.include_router(rTask.router)
    app.include_router(rAssistant.router)
    app.include_router(rMcp.router)
    from mino_nexus.services.assistant_tools import bind_app

    bind_app(app)

    node_ws.register_routes(app)
    ui_ws.register_routes(app)

    uploads = data_dir() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(uploads)), name="static")

    @app.get("/health")
    def health() -> dict[str, Any]:
        """无 Scout 也必须能起、能答（ARCHITECTURE.md §6）。"""
        cat = catalog.health_summary()
        nodes = get_registry().nodes()
        return {
            "ok": True,
            "nexus_version": NEXUS_VERSION,
            "protocol_version": P.PROTOCOL_VERSION,
            "catalog": cat,
            "nodes": len(nodes),
            "nodes_alive": sum(1 for n in nodes if n.alive),
        }

    @app.get("/nodes")
    def nodes(sess: dict = Depends(current_session)) -> dict[str, Any]:
        from mino_nexus.services.node_store import filter_nodes
        return {"nodes": filter_nodes([n.brief() for n in get_registry().nodes()], sess)}

    @app.get("/devices")
    def devices(sess: dict = Depends(current_session)) -> dict[str, Any]:
        """连通性来自 Scout 上报，不是 Nexus 自己探的（CLAUDE.md §6）。"""
        from mino_nexus.services.ui_devices import ui_devices
        return {"devices": ui_devices()}

    @app.get("/capabilities")
    def capabilities(sn: str = "", sess: dict = Depends(current_session)) -> dict[str, Any]:
        """能力菜单 = Nexus 目录 ∩ 该节点上报的 executor。

        不带 sn 时返回目录全集；带 sn 时返回这台设备实际能干什么。
        """
        caps = catalog.list_capabilities()
        if not sn:
            return {
                "scope": "catalog",
                "capabilities": [
                    {"id": c.id, "kind": c.kind, "executors": sorted({i.executor for i in c.implementations})}
                    for c in caps
                ],
            }
        from mino_nexus.services.access_scope import require_owned_device
        require_owned_device(sn)
        node, why = get_registry().resolve(sn)
        if node is None:
            raise HTTPException(status_code=409, detail=why)
        from mino_nexus.loop.router_proxy import executors_for_device

        avail = set(executors_for_device(sn, node))
        out = []
        for c in caps:
            usable = [i.executor for i in c.implementations if i.executor in avail]
            if usable or not c.implementations:
                out.append({"id": c.id, "kind": c.kind, "executors": sorted(set(usable))})
        return {
            "scope": f"node={node.node_id} sn={sn}",
            "node_executors": sorted(avail),
            "capabilities": out,
            "count": len(out),
            "catalog_total": len(caps),
        }

    # ---------------- 调试脚手架 ----------------

    @app.post("/debug/observe")
    async def debug_observe(body: ObserveBody) -> dict[str, Any]:
        proxy = RouterProxy(body.sn, run_id="debug")
        shot = await proxy.observe_async(
            body.kind,
            prefer=tuple(body.prefer) if body.prefer else None,
            timeout_sec=body.timeout_sec,
            compress_ratio=body.compress_ratio,
        )
        return {
            "ok": shot.ok,
            "source": shot.source,
            "width": shot.width,
            "height": shot.height,
            "mime": shot.image_mime,
            "bytes_b64": len(shot.image_base64),
            "elapsed_ms": shot.elapsed_ms,
            "error": shot.error,
            "extra": shot.remote_detail,
        }

    @app.post("/debug/screenshot")
    async def debug_screenshot(body: ObserveBody) -> Any:
        from fastapi.responses import Response

        proxy = RouterProxy(body.sn, run_id="debug")
        shot = await proxy.observe_async("screenshot", compress_ratio=body.compress_ratio)
        if not shot.has_image():
            raise HTTPException(status_code=409, detail=shot.error or "抓图失败")
        import base64

        return Response(content=base64.b64decode(shot.image_base64), media_type=shot.image_mime)

    @app.post("/debug/execute")
    async def debug_execute(body: ExecuteBody) -> dict[str, Any]:
        if is_local_cap(body.capability_id):
            raise HTTPException(
                status_code=400,
                detail=f"cap={body.capability_id} 属于 Nexus 本地 executor（问人 / 视觉断言 / "
                       "拟人化 / 等待），不经 Scout。local_executors 尚未搬迁",
            )
        event = PlanEvent(
            seq=body.step_idx,
            capability_id=body.capability_id,
            params=body.params,
            expected_executor=body.expected_executor,
            fallback_executors=body.fallback_executors,
            ai_reasoning="debug 手动下发",
        )
        import uuid

        run_id = body.run_id or f"debug-{uuid.uuid4().hex[:8]}"
        proxy = RouterProxy(body.sn, run_id=run_id)
        res = await proxy.dispatch_async(event, run_id=run_id, step_idx=body.step_idx)
        return {
            "status": res.status.value,
            "executor_used": res.executor_used,
            "summary": res.summary,
            "error": res.error,
            "elapsed_ms": res.elapsed_ms,
            "attempts": res.attempts,
            "raw": res.raw_response,
        }

    return app


app = create_app()
