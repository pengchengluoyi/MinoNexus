#!/usr/bin/env python3
"""Jobs 烟测：save→load→preview→恢复上一版（需 llm_jobs 表已有数据）。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MINO_NEXUS_DATA_DIR"] = tempfile.mkdtemp(prefix="mino-e2e-jobs-")

from mino_nexus.services.job_store import (  # noqa: E402
    get_job,
    list_jobs,
    preview_job,
    save_job,
    startup_health,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== bootstrap ==")
jobs = list_jobs()
check("jobs in db", len(jobs) > 0, str(len(jobs)))
if not jobs:
    print("skip: empty llm_jobs — populate DB or copy from existing mino.db")
    sys.exit(0)

health = startup_health()
check("health ok", health.get("ok", 0) > 0, str(health))

job_id = "conductor" if get_job("conductor") else jobs[0]["id"]
print(f"== preview {job_id} ==")
prev = preview_job(job_id)
check("preview messages", bool(prev.get("messages")), str(prev)[:120])

print("== save round-trip ==")
row = get_job(job_id) or {}
blocks = list(row.get("system_blocks") or [])
if blocks:
    blocks[0] = {**blocks[0], "text": (blocks[0].get("text") or "") + "\n<!-- e2e -->"}
    saved = save_job(job_id, {"system_blocks": blocks})
    check("save returns id", saved.get("id") == job_id)
    reloaded = get_job(job_id) or {}
    check("reload has e2e marker", "e2e" in str((reloaded.get("system_blocks") or [{}])[0].get("text") or ""))
    revs = list((reloaded.get("overrides_json") or {}).get("revisions") or [])
    check("revision history", len(revs) >= 1, str(len(revs)))

    print("== reset (undo last save) ==")
    reset = save_job(job_id, {"reset": True})
    check("reset ok", "e2e" not in str((reset.get("system_blocks") or [{}])[0].get("text") or ""))

print()
if failures:
    print("FAILED:", ", ".join(failures))
    sys.exit(1)
print("ALL OK — jobs e2e")
