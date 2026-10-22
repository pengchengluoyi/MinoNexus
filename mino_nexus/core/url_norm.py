"""URL 规范化。"""
from __future__ import annotations

from urllib.parse import urlsplit

def origin(value):
    if not isinstance(value, str):
        raise ValueError("source_api_url_invalid")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or len(value) > 500 or any(character.isspace() or ord(character) < 32 for character in value)):
            raise ValueError
        parsed.port
    except (ValueError, TypeError):
        raise ValueError("source_api_url_invalid") from None
    return value.rstrip("/")
