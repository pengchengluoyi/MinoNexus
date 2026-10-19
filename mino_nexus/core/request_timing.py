"""HTTP timing without logging credentials, query strings or response bodies."""
from time import perf_counter

from mino_nexus.core.log import SLog


class RequestTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = perf_counter()
        first_byte_ms = None
        status = 500
        body_bytes = 0
        completed = False
        completed_ms = None

        async def timed_send(message):
            nonlocal first_byte_ms, status, body_bytes, completed, completed_ms
            if message["type"] == "http.response.start":
                duration = (perf_counter() - started) * 1000
                first_byte_ms = duration
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"server-timing", f"app;dur={duration:.2f}".encode("ascii")))
                message = {**message, "headers": headers}
            if message["type"] == "http.response.body":
                body_bytes += len(message.get("body", b""))
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
                completed_ms = (perf_counter() - started) * 1000

        try:
            await self.app(scope, receive, timed_send)
        finally:
            app_ms = (perf_counter() - started) * 1000
            duration = completed_ms if completed_ms is not None else app_ms
            if duration >= 250 or not completed:
                route = getattr(scope.get("route"), "path", "<unmatched>")
                # Route templates only: never query strings, raw paths or bodies.
                SLog.i("HTTP", f"PERF {scope.get('method', '')} {route} status={status} "
                       f"ttfb_ms={first_byte_ms} response_ms={duration:.2f} "
                       f"app_ms={app_ms:.2f} bytes={body_bytes} complete={completed}")
