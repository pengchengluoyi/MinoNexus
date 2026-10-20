"""Explicit development/proxy browser origins; authentication remains mandatory."""
import os
from urllib.parse import urlsplit
from mino_nexus.core.runtime_mode import is_development


def browser_origins() -> list[str]:
    raw = os.environ.get("MINO_WORKBENCH_ORIGINS")
    if raw is None:
        return [f"http://{host}:60101" for host in ("localhost", "127.0.0.1")] if is_development() else []
    if not raw.strip():
        return []
    result = []
    for value in raw.split(","):
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if not value or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("MINO_WORKBENCH_ORIGINS must contain explicit HTTP(S) origins")
        _ = parsed.port
        result.append(value)
    return result


def browser_origin_allowed(value: str | None, server_host: str = "", request_origin: str = "") -> bool:
    return value is None or (bool(request_origin) and value == request_origin) or value in browser_origins()
