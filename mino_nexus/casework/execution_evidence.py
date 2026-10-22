"""Public process identity stored with ordinary session events."""
from __future__ import annotations



def object_value(value):
    return value if isinstance(value, dict) else {}


def public_identity(value):
    value = object_value(value)
    safe = {key: value[key][:256] for key in ("instance_id", "build_id", "source_commit", "source_digest", "identity_kind")
            if isinstance(value.get(key), str)}
    safe.update({key: value[key] for key in ("dirty", "source_unchanged") if type(value.get(key)) is bool})
    return safe
