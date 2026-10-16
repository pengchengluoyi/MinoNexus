"""Persisted deployment admission gate. Existing runs continue under update_hold."""
from contextlib import contextmanager
from functools import wraps
from mino_nexus.core.deployment import Deployment, deployment_root


@contextmanager
def admission():
    root = deployment_root()
    if root is None:
        yield
        return
    d = Deployment(root)
    # Serializes creation/claim against the updater's hold and final idle check.
    with d.lock():
        row = d.held()
        if row:
            from fastapi import HTTPException
            raise HTTPException(409, {"status": "blocked_by_update", "operation_id": row["operation_id"]})
        yield


def admitted(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with admission():
            return fn(*args, **kwargs)
    return wrapper


@contextmanager
def background_work():
    root = deployment_root()
    if root is None:
        yield True
        return
    d = Deployment(root)
    # control lock is only held during admission; the shared writers lock covers
    # the entire operation, including a to_thread worker after task cancellation.
    with d.lock():
        if d.held():
            yield False
            return
        lock = d.lock("writers", shared=True)
        lock.__enter__()
    try:
        yield True
    finally:
        lock.__exit__(None, None, None)


def background(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with background_work() as allowed:
            return fn(*args, **kwargs) if allowed else None
    return wrapper


class MaintenanceMiddleware:
    """Account for in-flight HTTP work, and close admission during stop/verify.

    Probe and operation queries remain reachable after restart. Websocket device
    tasks retain their persisted running run until their existing cleanup finishes.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        root = deployment_root()
        if root is None or scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse
        from starlette.concurrency import run_in_threadpool
        d = Deployment(root)
        path = scope.get("path", "")
        administrative = path in {"/health", "/sys/server_info", "/mcp"} or path.startswith("/sys/updates/")
        def enter():
            with d.lock():
                row = d.held()
                if row and row["state"] not in {"waiting_idle", "requested"} and not administrative:
                    return None, row
                guard = d.lock("writers", shared=True)
                guard.__enter__()
                return guard, None
        guard, blocked = await run_in_threadpool(enter)
        if blocked:
            return await JSONResponse({"detail": {"status": "blocked_by_update", "operation_id": blocked["operation_id"]}}, status_code=503)(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            guard.__exit__(None, None, None)


@contextmanager
def tracked_work(*, maintenance=False):
    root = deployment_root()
    if root is None:
        yield
        return
    d = Deployment(root)
    with d.lock():
        row = d.held()
        if row and row["state"] not in {"requested", "waiting_idle"} and not maintenance:
            raise RuntimeError("blocked_by_update: " + row["operation_id"])
        guard = d.lock("writers", shared=True)
        guard.__enter__()
    try:
        yield
    finally:
        guard.__exit__(None, None, None)


def tracked(fn=None, *, maintenance=False):
    """Track accepted background work until its thread actually exits.

    Waiting holds do not interrupt accepted work. Unlike periodic background(),
    this wrapper never silently drops an operation with a durable receipt.
    """
    if fn is None:
        return lambda func: tracked(func, maintenance=maintenance)
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with tracked_work(maintenance=maintenance):
            return fn(*args, **kwargs)
    return wrapper


def async_tracked(fn):
    """Hold the writer lease through asynchronous receipt reconciliation."""
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        with tracked_work():
            return await fn(*args, **kwargs)
    return wrapper
