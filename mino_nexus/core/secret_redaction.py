"""Sanitize copies at output boundaries; never transform execution inputs."""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.RLock()
_values: set[str] = set()
_ordered_values: tuple[str, ...] | None = None
_keys = {"password", "passwd", "secret", "secret_value", "api_key", "access_token", "authorization"}


def remember(value: Any) -> None:
    global _ordered_values
    if isinstance(value, str) and value:
        with _lock:
            if value not in _values:
                _values.add(value)
                _ordered_values = None


def register_inputs(params: dict) -> None:
    for key, value in params.items():
        if str(key).lower() in _keys and not isinstance(value, dict):
            remember(value)
        elif isinstance(value, dict):
            register_inputs(value)
    if params.get("sensitive") or (params.get("control") or {}).get("sensitive") or params.get("field") in {"password", "otp"} or params.get("login_field") in {"password", "otp"}:
        remember(params.get("text"))
        remember(params.get("value"))
        remember((params.get("payload") or {}).get("text"))
        remember((params.get("payload") or {}).get("value"))


def sanitize(value: Any) -> Any:
    global _ordered_values
    if isinstance(value, str):
        with _lock:
            # Reuse the immutable ordering until a new secret is registered.
            # Every string still acquires the lock and sees the latest snapshot.
            if _ordered_values is None:
                _ordered_values = tuple(sorted(_values, key=len, reverse=True))
            secrets = _ordered_values
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {k: ("" if str(k).lower() in _keys and not isinstance(v, (dict, bool))
                    else sanitize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize(v) for v in value)
    return value
