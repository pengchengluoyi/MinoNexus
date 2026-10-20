"""Runtime selection independent of Python packaging (no data-directory side effects)."""
import os
import sys


def is_development() -> bool:
    mode = os.environ.get("MINO_NEXUS_MODE", "")
    if mode not in {"", "development", "production"}:
        raise ValueError("MINO_NEXUS_MODE must be development or production")
    return mode == "development" if mode else not getattr(sys, "frozen", False)
