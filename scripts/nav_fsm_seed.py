#!/usr/bin/env python3
"""把一份 NavFSM 配置 JSON 写进本机 mino.db。

    python scripts/nav_fsm_seed.py <app_id> path/to/nav_fsm.json [--dry-run]

用途：walkthrough（设计稿 §8.4）做完后，一次性把实测值录进库。日常改配置走
Studio 的 `PUT /nav-fsm/{app_id}`；这个脚本只是没有 UI 时的等价入口。

**seed 前强制过 `validate_nav_fsm`**（§10.5 最后一句）：草稿里可以留 `__CALIBRATE__`，
但那种草稿不许进库 —— 进了就会在跑批时被 load 挡下，白配一场。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in argv
    if len(args) != 2:
        print(__doc__)
        return 2

    app_id, path = args[0], Path(args[1])
    if not path.exists():
        print(f"找不到文件：{path}")
        return 2

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"JSON 解析失败：{exc}")
        return 2

    from mino_nexus.core.database import ensure_db
    from mino_nexus.services import nav_fsm_store as store

    ensure_db()
    try:
        store.validate_doc({**doc, "app_id": app_id})
    except store.NavFsmInvalid as exc:
        print(f"校验不通过，未写库：{exc}")
        return 1

    if dry_run:
        print(f"校验通过（--dry-run，未写库）：states={len(doc.get('states') or [])} "
              f"edges={len(doc.get('edges') or [])}")
        return 0

    saved = store.save(app_id, doc, updated_by="nav_fsm_seed")
    print(
        f"已写入 app_id={saved['app_id']} version={saved['version']} "
        f"states={len(saved['states'])} edges={len(saved['edges'])}"
    )
    _, reason = store.load_with_reason(app_id)
    print("runtime 可用" if not reason else f"注意：runtime 仍不可用 —— {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
