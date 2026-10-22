"""仓库内 VERSION 文件：只读一次，供 HTTP 与 Scout 握手共用。"""
from __future__ import annotations

from pathlib import Path

VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
