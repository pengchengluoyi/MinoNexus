"""FastAPI 应用。

UI（Console / Studio）只打这里。Scout 走 `WS /node`。
登录与运行态的契约见 `docs/HTTP.md`。`/debug/*` 仍是脚手架。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mino_nexus.core import protocol as P
from mino_nexus.catalog import registry as catalog
from mino_nexus.core.client_gate import ClientGateMiddleware
from mino_nexus.core.log import SLog
from mino_nexus.loop.router_proxy import RouterProxy, is_local_cap
from mino_nexus.services.node_registry import get_registry
from pathlib import Path

from mino_nexus.core.paths import data_dir
from mino_nexus.routers import (
    rAppAutomation,
    rAuth,
    rCaseRunner,
    rDevice,
    rMe,
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
    from mino_nexus.services.auth_store import ensure_seed_users

    ensure_seed_users()
    configure_proxy_bypass()
    loop = asyncio.get_running_loop()
    ui_ws.set_loop(loop)
    from mino_nexus.loop.router_proxy import set_main_loop

    set_main_loop(loop)
    handle = await register_beacon()
    app.state.mdns = handle
    yield
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

    app.add_middleware(ClientGateMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(rSys.router)
    app.include_router(rAuth.router)
    app.include_router(rMe.router)
    app.include_router(rRuntime.router)
    app.include_router(rReleases.router)
    app.include_router(rDevice.router)
    app.include_router(rCaseRunner.router)
    app.include_router(rSettings.router)
    app.include_router(rPacks.router)
    app.include_router(rProject.router)
    app.include_router(rProjectCases.router)
    app.include_router(rAppAutomation.router)
    app.include_router(rTask.router)

    node_ws.register_routes(app)
    ui_ws.register_routes(app)

    uploads = data_dir() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(uploads)), name="static")
    studio_static = Path(__file__).resolve().parent / "static"
    studio_static.mkdir(parents=True, exist_ok=True)
    app.mount("/studio-static", StaticFiles(directory=str(studio_static)), name="studio-static")

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
    def nodes() -> dict[str, Any]:
        return {"nodes": [n.brief() for n in get_registry().nodes()]}

    @app.get("/devices")
    def devices() -> dict[str, Any]:
        """连通性来自 Scout 上报，不是 Nexus 自己探的（CLAUDE.md §6）。"""
        return {"devices": get_registry().devices()}

    @app.get("/capabilities")
    def capabilities(sn: str = "") -> dict[str, Any]:
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
