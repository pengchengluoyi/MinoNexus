"""Installed release identity, fixed for the lifetime of this process."""
from __future__ import annotations
import json
import os
import tempfile
import uuid
from pathlib import Path
from functools import lru_cache

def _load_build():
    path = Path(__file__).resolve().parents[1] / "BUILD.json"
    if not path.is_file():
        return {"build_id": "source-unpackaged", "source_commit": "", "dirty": True}
    data = json.loads(path.read_text())
    return {key: data[key] for key in ("build_id", "source_commit", "dirty")}

BUILD = _load_build()

def initialize_instance_id() -> str:
    from mino_nexus.core.paths import data_dir
    path = data_dir() / "instance-id"
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".instance-id-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(str(uuid.uuid4()))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Publish a complete file without replacing another initializer's ID.
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    return instance_id()

@lru_cache(maxsize=1)
def instance_id() -> str:
    from mino_nexus.core.paths import data_dir
    path = data_dir() / "instance-id"
    # Requests only read; startup must initialize the persistent identity first.
    value = path.read_text().strip()
    return str(uuid.UUID(value))

def identity() -> dict:
    return {**BUILD, "instance_id": instance_id()}
