#!/usr/bin/env python3
"""把库里的 prompt 只读导出成 mino_nexus/prompts/。

不调用 ensure_db / bootstrap，因此不会跑 job_upgrades。
库只以 mode=ro 打开。备份写到数据目录，不写回 mino.db。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prompt_pack import content_sha256, dump_yaml, entry_files, split_blocks  # noqa: E402

TODAY = date.today().isoformat()
DATA_DB = Path.home() / ".mino-nexus" / "mino.db"
BACKUP = Path.home() / ".mino-nexus" / f"mino.db.bak-prompt-export-{TODAY}"
RAW_DIR = ROOT / "docs" / "prompts" / f"db-export-{TODAY}"
PROMPTS = ROOT / "mino_nexus" / "prompts"

_PROMPT_FIELDS = ("system_prompt", "prompt_chars", "default_prompt", "prompt_custom")


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def backup_db() -> None:
    if not DATA_DB.is_file():
        raise SystemExit(f"找不到库：{DATA_DB}")
    if BACKUP.exists():
        print(f"备份已存在，跳过：{BACKUP}")
        return
    print(f"备份 {DATA_DB} → {BACKUP}")
    src = _connect_ro(DATA_DB)
    dst = sqlite3.connect(BACKUP)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"备份完成 {BACKUP.stat().st_size} 字节")


def _loads(raw: Any, fallback: Any) -> Any:
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    return json.loads(raw)


def _dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export_raw(con: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    job_cols = [r[1] for r in con.execute("PRAGMA table_info(llm_jobs)")]
    skill_cols = [r[1] for r in con.execute("PRAGMA table_info(skills)")]
    jobs = [dict(r) for r in con.execute("SELECT * FROM llm_jobs ORDER BY sort_order, id")]
    skills = [dict(r) for r in con.execute("SELECT * FROM skills ORDER BY sort_order, id")]
    for row in jobs:
        for col in job_cols:
            if col.endswith("_json"):
                row[col] = _loads(row.get(col), [] if col != "image_json" and col != "call_json" and col != "overrides_json" else {})
    for row in skills:
        for col in skill_cols:
            if col.endswith("_json"):
                fallback: Any = [] if col == "triggers_json" else {}
                row[col] = _loads(row.get(col), fallback)

    for row in jobs:
        _dump_json(RAW_DIR / "llm_jobs" / f"{row['id']}.json", row)
    for row in skills:
        _dump_json(RAW_DIR / "skills" / f"{row['id']}.json", row)

    settings_row = con.execute("SELECT id, payload FROM settings WHERE id='main'").fetchone()
    payload = _loads(settings_row["payload"], {}) if settings_row else {}
    ai = payload.get("ai") if isinstance(payload.get("ai"), dict) else {}
    role_prompts = ai.get("_role_prompts") if isinstance(ai.get("_role_prompts"), dict) else {}
    layer_stack = payload.get("layer_stack") if isinstance(payload.get("layer_stack"), dict) else {}
    _dump_json(RAW_DIR / "settings" / "ai._role_prompts.json", role_prompts)
    _dump_json(RAW_DIR / "settings" / "layer_stack.json", layer_stack)

    catalog = _catalog_snapshot()
    for name, data in catalog.items():
        _dump_json(RAW_DIR / "catalog" / f"{name}.json", data)
    return jobs, skills, {"role_prompts": role_prompts, "layer_stack": layer_stack, "catalog": catalog}


def _catalog_snapshot() -> dict[str, Any]:
    import mino_nexus.ai.roles_catalog as roles_catalog
    from mino_nexus.ai.layer_stack import (
        DEFAULT_ROLE_SKILLS,
        DEFAULT_SKILL_DRIVERS,
        DEFAULT_TRIGGER_ROLES,
        DEFAULT_TRIGGER_SKILLS,
        DRIVERS,
        ROLES,
        SKILL_CATEGORIES,
        SKILLS,
        TRIGGERS,
        _INTENT_LABEL,
        _SKILL_ALIASES,
    )
    from mino_nexus.ai.skill_defs import ALIASES, builtin_skills, enums

    roles_catalog._job_prompt = lambda *_a, **_k: ""  # 目录正文在 job 里，这里只取写死字段
    product = [_strip_prompt(row) for row in roles_catalog._product_roles()]
    runtime = [_strip_prompt(row) for row in roles_catalog._runtime_roles()]
    return {
        "skill_defs": {"skills": builtin_skills(), "aliases": dict(ALIASES), "enums": enums()},
        "roles_catalog": {
            "role_job_map": dict(roles_catalog.ROLE_JOB_MAP),
            "product": product,
            "runtime": runtime,
            "playbooks": roles_catalog.list_playbooks(),
        },
        "layer_stack": {
            "drivers": DRIVERS,
            "skill_categories": SKILL_CATEGORIES,
            "skills": SKILLS,
            "roles": ROLES,
            "triggers": TRIGGERS,
            "skill_drivers": DEFAULT_SKILL_DRIVERS,
            "role_skills": DEFAULT_ROLE_SKILLS,
            "trigger_roles": DEFAULT_TRIGGER_ROLES,
            "trigger_skills": DEFAULT_TRIGGER_SKILLS,
            "intent_labels": dict(_INTENT_LABEL),
            "skill_aliases": dict(_SKILL_ALIASES),
        },
    }


def _strip_prompt(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in _PROMPT_FIELDS}


def _job_yaml(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    system, system_files = split_blocks(list(row.get("system_blocks_json") or []), side="system")
    user, user_files = split_blocks(list(row.get("user_blocks_json") or []), side="user")
    spec = {
        "id": row["id"],
        "version": int(row.get("prompt_version") or 1),
        "label": row.get("label") or row["id"],
        "summary": row.get("summary") or "",
        "engine": row.get("engine") or "text_chat",
        "role": row.get("role_id") or "",
        "output_schema": row.get("output_schema") or "",
        "enabled": bool(row.get("enabled")),
        "sort_order": int(row.get("sort_order") or 0),
        "flags": list(row.get("flags_json") or []),
        "slots": list(row.get("slots_json") or []),
        "call": dict(row.get("call_json") or {}),
        "image": dict(row.get("image_json") or {}),
        "system": system,
        "user": user,
    }
    files = {**system_files, **user_files}
    return spec, files


def _skill_yaml(row: dict[str, Any]) -> dict[str, Any]:
    view = dict(row.get("view_json") or {})
    return {
        "id": row["id"],
        "version": 1,
        "label": row.get("label") or row["id"],
        "summary": row.get("summary") or "",
        "category": row.get("category") or "flow",
        "role": row.get("role_id") or "",
        "role_label": row.get("role_label") or "",
        "engine": row.get("engine") or "chat",
        "sop": dict(row.get("sop_json") or {}),
        "input": dict(row.get("input_json") or {}),
        "view": view,
        "triggers": list(row.get("triggers_json") or []),
        "enabled": bool(row.get("enabled")),
        "sort_order": int(row.get("sort_order") or 0),
    }


def _role_jobs(role_id: str, mapped: str, job_field: str, jobs: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for item in (mapped, job_field):
        if item and item not in out:
            out.append(item)
    for row in jobs:
        if str(row.get("role_id") or "") == role_id and row["id"] not in out:
            out.append(row["id"])
    return out


def write_prompts(jobs: list[dict[str, Any]], skills: list[dict[str, Any]], extra: dict[str, Any]) -> dict[str, Any]:
    if PROMPTS.exists():
        # 只清本脚本拥有的三类目录，避免误删后来手写的 assistant。
        for sub in ("jobs", "skills", "roles", "stack"):
            target = PROMPTS / sub
            if target.exists():
                for path in sorted(target.rglob("*"), reverse=True):
                    if path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
    lock: dict[str, Any] = {}
    char_rows: list[tuple[str, int, int]] = []

    for row in jobs:
        directory = PROMPTS / "jobs" / row["id"]
        directory.mkdir(parents=True, exist_ok=True)
        spec, files = _job_yaml(row)
        (directory / "job.yaml").write_text(dump_yaml(spec), encoding="utf-8")
        for rel, text in files.items():
            dest = directory / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        chars = sum(len(text) for text in files.values())
        chars += sum(len(str(b.get("text") or "")) for b in spec["system"] + spec["user"])
        char_rows.append((f"jobs/{row['id']}", int(spec["version"]), chars))
        lock[f"jobs/{row['id']}"] = {
            "version": int(spec["version"]),
            "sha256": content_sha256(entry_files(directory), root=directory),
        }

    for row in skills:
        directory = PROMPTS / "skills"
        directory.mkdir(parents=True, exist_ok=True)
        spec = _skill_yaml(row)
        path = directory / f"{row['id']}.yaml"
        path.write_text(dump_yaml(spec), encoding="utf-8")
        lock[f"skills/{row['id']}"] = {
            "version": 1,
            "sha256": content_sha256([path], root=directory),
        }

    catalog_roles = extra["catalog"]["roles_catalog"]
    role_map: dict[str, str] = catalog_roles["role_job_map"]
    for row in [*catalog_roles["product"], *catalog_roles["runtime"]]:
        rid = str(row["id"])
        spec = {
            "id": rid,
            "version": 1,
            "label": row.get("label") or rid,
            "group": row.get("group") or "",
            "kind": row.get("kind") or "",
            "source": row.get("source") or "",
            "summary": row.get("summary") or "",
            "used_in": list(row.get("used_in") or []),
            "related_ids": list(row.get("related_ids") or []),
            "live": bool(row.get("live", True)),
            "owner": row.get("owner") or "",
            "job": row.get("job") or "",
            "called": row.get("called") or "",
            "triggers": list(row.get("triggers") or []),
            "output": row.get("output") or "",
            "editable": bool(row.get("editable")),
            "jobs": _role_jobs(rid, role_map.get(rid, ""), str(row.get("job") or ""), jobs),
        }
        directory = PROMPTS / "roles"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{rid}.yaml"
        path.write_text(dump_yaml(spec), encoding="utf-8")
        lock[f"roles/{rid}"] = {
            "version": 1,
            "sha256": content_sha256([path], root=directory),
        }

    stack_src = extra["catalog"]["layer_stack"]
    stack = {
        "version": 1,
        "drivers": stack_src["drivers"],
        "skill_categories": stack_src["skill_categories"],
        "skills": stack_src["skills"],
        "roles": stack_src["roles"],
        "triggers": stack_src["triggers"],
        "skill_drivers": stack_src["skill_drivers"],
        "role_skills": stack_src["role_skills"],
        "trigger_roles": stack_src["trigger_roles"],
        "trigger_skills": stack_src["trigger_skills"],
        "intent_labels": stack_src["intent_labels"],
        "skill_aliases": stack_src["skill_aliases"],
        "bindings": extra["layer_stack"] or {},
    }
    stack_dir = PROMPTS / "stack"
    stack_dir.mkdir(parents=True, exist_ok=True)
    stack_path = stack_dir / "stack.yaml"
    stack_path.write_text(dump_yaml(stack), encoding="utf-8")
    lock["stack/stack"] = {"version": 1, "sha256": content_sha256([stack_path], root=stack_dir)}

    # assistant 目录先占位。社交助手的正文不在这次迁移里生成。
    for rel in ("assistant/jobs", "assistant/tools", "assistant/cards", "assistant/persona"):
        (PROMPTS / rel).mkdir(parents=True, exist_ok=True)
        keep = PROMPTS / rel / ".gitkeep"
        if not any(p.is_file() and p.name != ".gitkeep" for p in (PROMPTS / rel).iterdir()):
            keep.write_text("", encoding="utf-8")

    lock_doc = {"entries": dict(sorted(lock.items()))}
    (PROMPTS / "prompts.lock.json").write_text(
        json.dumps(lock_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"lock": lock, "chars": char_rows}


def report(jobs: list[dict[str, Any]], skills: list[dict[str, Any]], written: dict[str, Any]) -> None:
    job_files = list((PROMPTS / "jobs").glob("*/job.yaml"))
    skill_files = list((PROMPTS / "skills").glob("*.yaml"))
    role_files = list((PROMPTS / "roles").glob("*.yaml"))
    raw_jobs = list((RAW_DIR / "llm_jobs").glob("*.json"))
    raw_skills = list((RAW_DIR / "skills").glob("*.json"))
    print("—— 清点 ——")
    print(f"llm_jobs 行数 {len(jobs)}  原始 JSON {len(raw_jobs)}  job.yaml {len(job_files)}")
    print(f"skills 行数 {len(skills)}  原始 JSON {len(raw_skills)}  skill yaml {len(skill_files)}")
    print(f"roles yaml {len(role_files)}")
    print(f"{'条目':<36} {'version':>7} {'chars':>8}")
    for key, version, chars in written["chars"]:
        print(f"{key:<36} {version:>7} {chars:>8}")
    bad = []
    if len(jobs) != len(raw_jobs) or len(jobs) != len(job_files):
        bad.append("job 数量不一致")
    if len(skills) != len(raw_skills) or len(skills) != len(skill_files):
        bad.append("skill 数量不一致")
    if len(role_files) != len(written["lock"]) - len(jobs) - len(skills) - 1:
        # lock = jobs + skills + roles + stack
        pass
    expected_roles = len(jobs) * 0  # 占位，下面按 catalog 数核对
    catalog_roles = len(list((RAW_DIR / "catalog").glob("roles_catalog.json")))
    if catalog_roles != 1:
        bad.append("roles_catalog 原始导出缺失")
    role_n = 0
    raw_roles = json.loads((RAW_DIR / "catalog" / "roles_catalog.json").read_text(encoding="utf-8"))
    role_n = len(raw_roles.get("product") or []) + len(raw_roles.get("runtime") or [])
    if role_n != len(role_files):
        bad.append(f"role 数量不一致 db-catalog={role_n} files={len(role_files)}")
    if bad:
        for item in bad:
            print("错误", item)
        raise SystemExit(1)
    print("清点通过")
    _ = expected_roles


def main() -> None:
    backup_db()
    con = _connect_ro(DATA_DB)
    try:
        jobs, skills, extra = export_raw(con)
    finally:
        con.close()
    written = write_prompts(jobs, skills, extra)
    report(jobs, skills, written)
    print(f"原始导出 {RAW_DIR}")
    print(f"prompt 文件 {PROMPTS}")


if __name__ == "__main__":
    main()
