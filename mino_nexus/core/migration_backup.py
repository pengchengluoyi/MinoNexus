"""账号隔离迁移前做 SQLite 一致性备份；不覆盖已有备份。"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

MARKER = "migration:account-isolation-v1"


def backup_before_account_migration(path: Path) -> Path | None:
    if not path.is_file():
        return None
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "settings" in tables and source.execute("SELECT 1 FROM settings WHERE id=?", (MARKER,)).fetchone():
            cols = {r[1] for r in source.execute("PRAGMA table_info(im_route_identities)")}
            if "binding_key" in cols:
                return None
        directory = path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ("mino-before-account-isolation-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db")
        # 独占创建、仅当前系统账号可读写。
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
        return target
    finally:
        source.close()
