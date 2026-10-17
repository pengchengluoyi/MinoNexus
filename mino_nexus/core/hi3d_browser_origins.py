"""Explicit browser deployment origins; never an authentication substitute."""
import os
from urllib.parse import urlsplit


def browser_origins() -> list[str]:
    raw = os.environ.get("MINO_WORKBENCH_ORIGINS")
    values = raw.split(",") if raw is not None else [
        f"http://{host}:{port}" for host in ("localhost", "127.0.0.1", "mino.local")
        for port in (5273, 5274)
    ]
    result = []
    for value in values:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if not value or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("MINO_WORKBENCH_ORIGINS must contain explicit HTTP(S) origins")
        _ = parsed.port
        result.append(value)
    return result


def browser_origin_allowed(value: str | None) -> bool:
    # Non-browser MCP and other clients do not require an Origin header; existing auth remains.
    return value is None or value in browser_origins()
