"""进程内带锁的小 JSON 文件。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from mino_nexus.paths import data_dir

_lock = threading.RLock()


def load_json(name: str, default: Any) -> Any:
    path = data_dir() / name
    with _lock:
        if not path.is_file():
            return default
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        return raw if raw is not None else default


def save_json(name: str, payload: Any) -> None:
    path = data_dir() / name
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
