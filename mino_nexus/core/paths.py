"""本地数据目录。

port(nexus): 替代上游 `server/core/database.APP_DATA_DIR`。
上游把它和 SQLAlchemy engine 放在同一个模块里，于是任何要拼路径的模块都被迫
import 数据库。这里拆开 —— `dispatch_log` 只是要个落盘位置，不该拖进 ORM。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def get_app_data_dir() -> Path:
    development = not getattr(sys, "frozen", False)
    prod_default = (Path(os.environ.get("APPDATA", Path.home())) / "MinoNexus" if os.name == "nt"
                    else Path.home() / "Library/Application Support/Mino/nexus/data" if sys.platform == "darwin"
                    else Path.home() / ".mino-nexus")
    if not development and sys.platform == "darwin" and not os.environ.get("MINO_NEXUS_DATA_DIR"):
        legacy = Path.home() / ".mino-nexus"
        if (legacy / "mino.db").exists():
            if prod_default.exists():
                raise ValueError("检测到新旧 Nexus 数据目录，请显式指定 MINO_NEXUS_DATA_DIR；不会自动合并")
            prod_default = legacy
    dev_default = Path.home() / ".mino-dev" / "nexus"
    # Only this process's environment selects its data root. IDEs may retain an
    # old MINO_NEXUS_DATA_DIR pointing at dev; it is not a production instance.
    variable = "MINO_NEXUS_DEV_DATA_DIR" if development else "MINO_NEXUS_DATA_DIR"
    default = dev_default if development else prod_default
    p = Path(os.environ.get(variable) or default).expanduser().resolve()
    forbidden = ([prod_default, Path.home() / ".mino-nexus", Path.home() / "Mino" / "nexus",
                  Path.home() / "Library" / "Application Support" / "Mino" / "nexus"]
                 if development else [dev_default])
    for other in forbidden:
        other = other.resolve()
        if p == other or p in other.parents or other in p.parents:
            raise ValueError("开发与正式 Nexus 数据目录必须完全独立，不能相同或互相包含")
    p.mkdir(parents=True, exist_ok=True)
    marker = p / ".nexus-environment"
    expected = "development" if development else "production"
    try:
        with marker.open("x", encoding="utf-8") as stream:
            stream.write(expected)
    except FileExistsError:
        if marker.read_text(encoding="utf-8").strip() != expected:
            raise ValueError("Nexus 数据目录属于另一环境，禁止共用")
    return p


def data_dir() -> Path:
    """Path 形态的入口。并发在做的 Console/Studio 层（rReleases / 上传目录）用这个。"""
    return get_app_data_dir()


# 字符串形态。搬过来的 dispatch_log 按上游写法用 os.path.join 拼，所以保留。
APP_DATA_DIR = str(get_app_data_dir())


def nav_calibration_dir(app_id: str, calibration_id: str = "") -> Path:
    """NavFSM 校准证据目录（docs/NAVIGATION_ATLAS.md §0.2.2）。

    与 `mino.db` 同根，**不进 git**：运维备份整个数据目录即可。
    `calibration_id` 为空时返回该 app 的证据根目录，供列表用。

    v2.5 起 walkthrough 批次为遗留路径；新采集走 `nav_capture_dir`。
    """
    base = data_dir() / "nav" / "calibration" / _safe_seg(app_id)
    return base / _safe_seg(calibration_id) if calibration_id else base


def nav_capture_dir(app_id: str, session_id: str = "") -> Path:
    """v2.5 被动 hierarchy 样本（docs/NAVIGATION_ATLAS.md §19.4）。

    `{data_dir}/nav/capture/{app_id}/{session_id}/turn_NNNN.*`
    """
    base = data_dir() / "nav" / "capture" / _safe_seg(app_id)
    return base / _safe_seg(session_id) if session_id else base


def nav_candidates_dir(app_id: str, batch_id: str = "") -> Path:
    """v2.5 字段候选包（审核前不进 `nav_fsm*`）。"""
    base = data_dir() / "nav" / "candidates" / _safe_seg(app_id)
    return base / f"{_safe_seg(batch_id)}.json" if batch_id else base


def _safe_seg(raw: str) -> str:
    """app_id / calibration_id 来自 HTTP 路径，落盘前挡掉穿越。"""
    seg = str(raw or "").strip().replace("\\", "/").split("/")[-1]
    seg = "".join(ch for ch in seg if ch.isalnum() or ch in "-_.")
    return "_" if seg in ("", ".", "..") else seg
