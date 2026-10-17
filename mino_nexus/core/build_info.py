"""Installed release identity, fixed for the lifetime of this process."""
from __future__ import annotations
import json
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

@lru_cache(maxsize=1)
def instance_id() -> str:
    from mino_nexus.core.paths import data_dir
    path = data_dir() / "instance-id"
    # A single initialization writer creates this before serving requests.
    try:
        with path.open("x") as stream:
            stream.write(str(uuid.uuid4()))
    except FileExistsError:
        pass
    value = path.read_text().strip()
    return str(uuid.UUID(value))

def identity() -> dict:
    return {**BUILD, "instance_id": instance_id()}
