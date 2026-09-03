"""本地数据目录。

port(nexus): 替代上游 `server/core/database.APP_DATA_DIR`。
上游把它和 SQLAlchemy engine 放在同一个模块里，于是任何要拼路径的模块都被迫
import 数据库。这里拆开 —— `dispatch_log` 只是要个落盘位置，不该拖进 ORM。
"""
from __future__ import annotations

import os
from pathlib import Path


def get_app_data_dir() -> Path:
    env = os.environ.get("MINO_NEXUS_DATA_DIR")
    if env:
        p = Path(env).expanduser().resolve()
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", Path.home())) / "MinoNexus"
    else:
        p = Path.home() / ".mino-nexus"
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    """Path 形态的入口。并发在做的 Console/Studio 层（json_store / rReleases）用这个。"""
    return get_app_data_dir()


# 字符串形态。搬过来的 dispatch_log 按上游写法用 os.path.join 拼，所以保留。
APP_DATA_DIR = str(get_app_data_dir())
