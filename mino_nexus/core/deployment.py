"""Nexus deployment contract. Standard library only; never imports the application DB.

The deployment journal is installation infrastructure, not a second business store.
All administrative changes share one OS lock; the journal survives lock release.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from mino_nexus.core.platform_runtime import file_lock, sync_directory, current_release, switch_release

TERMINAL = {"succeeded", "rolled_back", "failed", "cancelled", "expired"}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")


def identifier(value: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError("Invalid build/operation identifier")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)



def legacy_path(path):
    return path

def read_json(path: Path) -> dict:
    value = json.loads(legacy_path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected object: {path.name}")
    return value


def deployment_root() -> Path | None:
    raw = os.environ.get("MINO_NEXUS_INSTALL_ROOT", "")
    return Path(raw).expanduser().resolve() if raw else None


def identity() -> dict:
    # Reuse the installed BUILD.json and persisted data-root instance identity.
    from mino_nexus.core.build_info import identity as build_identity
    return {**build_identity(), "health_contract_version": 1,
            "deployment_managed": deployment_root() is not None}


class Deployment:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.config_path = legacy_path(self.root / "config/mino-deployment.json")
        self.state_path = legacy_path(self.root / "updates/mino-state.json")

    @property
    def config(self) -> dict:
        return read_json(self.config_path)

    @contextmanager
    def lock(self, name="control", *, shared=False, blocking=True):
        path = self.root / "updates" / (identifier(name) + ".lock")
        with file_lock(path, shared=shared, blocking=blocking):
            yield

    def state(self) -> dict:
        return read_json(self.state_path) if self.state_path.exists() else {}

    def save(self, row: dict, **fields) -> dict:
        row = {**row, **fields, "updated_at": time.time()}
        atomic_json(self.state_path, row)
        atomic_json(self.root / "updates" / ("mino-" + identifier(row["operation_id"]) + ".json"), row)
        return row

    def operation(self, operation_id: str) -> dict:
        identifier(operation_id)
        current = self.state()
        if current.get("operation_id") == operation_id:
            return current
        return read_json(self.root / "updates" / ("mino-" + identifier(operation_id) + ".json"))

    def release(self, build: str) -> Path:
        p = self.root / "releases" / identifier(build)
        if p.is_symlink() or p.resolve().parent != (self.root / "releases").resolve():
            raise ValueError("Release must be an immutable direct child")
        return p

    def current_build(self) -> str:
        return identifier(current_release(self.root).name)

    def switch(self, build: str) -> None:
        identifier(build)
        switch_release(self.root, build)

    def held(self) -> dict:
        row = self.state()
        if not row or row.get("state") in TERMINAL:
            return {}
        # An expired waiting hold is harmless, but installing/unknown never expires.
        if row.get("state") in {"requested", "waiting_idle"} and row["deadline"] <= time.time():
            return {}
        return row


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
