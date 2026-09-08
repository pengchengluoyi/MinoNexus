#!/usr/bin/env python3
"""从 llm_jobs 表导出 job 配置到指定目录。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def export_from_db(out: Path) -> int:
    sys.path.insert(0, str(ROOT))
    from mino_nexus.core.database import ensure_db
    from mino_nexus.services.job_store import list_jobs

    ensure_db()
    rows = list_jobs()
    out.mkdir(parents=True, exist_ok=True)
    ids = []
    for row in rows:
        jid = str(row.get("id") or "")
        if not jid:
            continue
        ids.append(jid)
        job_dir = out / jid
        job_dir.mkdir(parents=True, exist_ok=True)
        spec = dict(row)
        for side in ("system_blocks", "user_blocks"):
            blocks = []
            for block in spec.get(side) or []:
                if not isinstance(block, dict):
                    continue
                b = dict(block)
                text = str(b.get("text") or "")
                if len(text) > 120:
                    fname = f"{b.get('id') or side}.txt"
                    (job_dir / fname).write_text(text, encoding="utf-8")
                    b["text"] = f"@file:{fname}"
                blocks.append(b)
            spec[side] = blocks
        (job_dir / "job.json").write_text(
            json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    (out / "index.json").write_text(
        json.dumps({"rev": "export", "jobs": ids}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(ids)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "export-jobs"
    n = export_from_db(out)
    print(f"exported {n} jobs -> {out}")


if __name__ == "__main__":
    main()
