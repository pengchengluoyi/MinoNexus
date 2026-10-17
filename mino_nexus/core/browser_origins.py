"""Explicit browser deployment origins; never an authentication substitute."""
import os
import sys
import ipaddress
from urllib.parse import urlsplit


def browser_origins() -> list[str]:
    raw = os.environ.get("MINO_WORKBENCH_ORIGINS")
    values = raw.split(",") if raw is not None else [
        f"http://{host}:{port}" for host in ("localhost", "127.0.0.1", "mino.local")
        for port in ((60104,) if getattr(sys, "frozen", False) else (60101,))
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


def browser_origin_allowed(value: str | None, server_host: str = "") -> bool:
    # Trust the accepted socket address, never an untrusted Host/Forwarded header.
    if value is None or value in browser_origins():
        return True
    if "MINO_WORKBENCH_ORIGINS" in os.environ or not getattr(sys, "frozen", False):
        return False
    try:
        origin = urlsplit(value)
        local = ipaddress.ip_address(server_host)
        return (origin.scheme == "http" and origin.port == 60104
                and not origin.username and not origin.password and not origin.path
                and not origin.query and not origin.fragment and not local.is_unspecified
                and ipaddress.ip_address(origin.hostname) == local)
    except (ValueError, TypeError):
        return False
