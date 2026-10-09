#!/usr/bin/env python3
"""核对导出文件与库里的 prompt：渲染结果逐字节相同。

只读打开 mino.db。ensure_db 被换成空操作，避免 bootstrap 写库。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prompt_pack import canonical_json, load_job_dir, load_yaml, messages_bytes  # noqa: E402

DATA_DB = Path.home() / ".mino-nexus" / "mino.db"
PROMPTS = ROOT / "mino_nexus" / "prompts"


def _install_readonly() -> None:
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    import mino_nexus.core.database as database

    def creator():
        con = sqlite3.connect(f"file:{DATA_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    engine = create_engine("sqlite://", creator=creator, poolclass=NullPool)

    def _ro(dbapi_connection, _record) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA query_only=ON")
        cur.close()

    event.listen(engine, "connect", _ro)
    database.engine = engine
    database.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    database.ensure_db = lambda: None
    database._ready = True

    # list_skills 会顺手 seed。只读核对时禁止插入。
    import mino_nexus.services.skill_store as skill_store

    skill_store.seed_skills = lambda: 0


def _loads(raw: Any, fallback: Any) -> Any:
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    return json.loads(raw)


def _db_jobs(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = []
    for raw in con.execute("SELECT * FROM llm_jobs ORDER BY sort_order, id"):
        row = dict(raw)
        rows.append({
            "id": row["id"],
            "label": row.get("label") or row["id"],
            "summary": row.get("summary") or "",
            "engine": row.get("engine") or "text_chat",
            "role_id": row.get("role_id") or "",
            "output_schema": row.get("output_schema") or "",
            "slots": _loads(row.get("slots_json"), []),
            "flags": _loads(row.get("flags_json"), []),
            "system_blocks": _loads(row.get("system_blocks_json"), []),
            "user_blocks": _loads(row.get("user_blocks_json"), []),
            "image": _loads(row.get("image_json"), {}),
            "call": _loads(row.get("call_json"), {}),
            "enabled": bool(row.get("enabled")),
            "prompt_version": int(row.get("prompt_version") or 1),
        })
    return rows


def _fake_slots(row: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in row.get("slots") or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or spec.get("id") or "")
        kind = str(spec.get("kind") or "text")
        if kind == "json":
            out[name] = "{}"
        elif kind == "image":
            out[name] = "aaaa"
        else:
            out[name] = "X"
    return out


def _check_jobs(con: sqlite3.Connection) -> list[str]:
    from mino_nexus.ai.prompt_render import render_job

    errors: list[str] = []
    db_rows = _db_jobs(con)
    file_ids = sorted(p.parent.name for p in (PROMPTS / "jobs").glob("*/job.yaml"))
    db_ids = [r["id"] for r in db_rows]
    missing = sorted(set(db_ids) - set(file_ids))
    if missing:
        errors.append(f"库里的 job 没有对应文件：{missing}")
        return errors
    for row in db_rows:
        loaded = load_job_dir(PROMPTS / "jobs" / row["id"])
        if int(loaded["prompt_version"]) != int(row["prompt_version"]):
            errors.append(f"{row['id']} version 文件={loaded['prompt_version']} 库={row['prompt_version']}")
        slots = _fake_slots(row)
        db_messages, _ = render_job(row, slots)
        file_messages, _ = render_job(loaded, slots)
        if messages_bytes(db_messages) != messages_bytes(file_messages):
            errors.append(f"{row['id']} messages 不一致")
            errors.append("  db   " + messages_bytes(db_messages)[:240].decode())
            errors.append("  file " + messages_bytes(file_messages)[:240].decode())
    return errors


def _check_skills(con: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    db_ids = []
    for raw in con.execute("SELECT * FROM skills ORDER BY sort_order, id"):
        row = dict(raw)
        db_ids.append(row["id"])
        spec = load_yaml(PROMPTS / "skills" / f"{row['id']}.yaml")
        pairs = (
            ("sop", _loads(row.get("sop_json"), {})),
            ("input", _loads(row.get("input_json"), {})),
            ("view", _loads(row.get("view_json"), {})),
            ("triggers", _loads(row.get("triggers_json"), [])),
        )
        for key, db_val in pairs:
            if canonical_json(spec.get(key)) != canonical_json(db_val):
                errors.append(f"skill {row['id']} {key} 不一致")
    file_ids = sorted(p.stem for p in (PROMPTS / "skills").glob("*.yaml"))
    if sorted(db_ids) != file_ids:
        errors.append(f"skill id 不一致 db={sorted(db_ids)} files={file_ids}")
    return errors


def _role_row_from_file(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": spec.get("id"),
        "label": spec.get("label") or spec.get("id"),
        "group": spec.get("group") or "",
        "kind": spec.get("kind") or "",
        "source": spec.get("source") or "",
        "used_in": list(spec.get("used_in") or []),
        "summary": spec.get("summary") or "",
        "system_prompt": "",
        "prompt_chars": 0,
        "related_ids": list(spec.get("related_ids") or []),
        "live": spec.get("live", True) is not False,
        "owner": spec.get("owner") or "",
        "job": spec.get("job") or "",
        "called": spec.get("called") or "",
        "triggers": list(spec.get("triggers") or []),
        "output": spec.get("output") or "",
        "editable": bool(spec.get("editable")),
        "default_prompt": "",
        "prompt_custom": False,
    }


def _check_roles_and_stack() -> list[str]:
    import mino_nexus.ai.layer_stack as layer_stack
    import mino_nexus.ai.roles_catalog as roles_catalog

    errors: list[str] = []
    live_roles = roles_catalog.list_roles()
    live_stack = layer_stack.get_stack()

    product = []
    runtime = []
    for path in sorted((PROMPTS / "roles").glob("*.yaml")):
        spec = load_yaml(path)
        row = _role_row_from_file(spec)
        (runtime if row["group"] == "runtime" else product).append(row)
    product.sort(key=lambda r: str(r["id"]))
    runtime.sort(key=lambda r: str(r["id"]))

    # 文件顺序必须跟目录函数一致，否则 trees 会变。按现有函数的 id 顺序重排。
    live_product_ids = [r["id"] for r in roles_catalog._product_roles()]
    live_runtime_ids = [r["id"] for r in roles_catalog._runtime_roles()]
    by_id = {r["id"]: r for r in product + runtime}
    product = [by_id[i] for i in live_product_ids]
    runtime = [by_id[i] for i in live_runtime_ids]

    roles_catalog._product_roles = lambda: product
    roles_catalog._runtime_roles = lambda: runtime
    roles_catalog._all_catalog_rows = lambda: [*product, *runtime]
    file_roles = roles_catalog.list_roles()
    if canonical_json(live_roles) != canonical_json(file_roles):
        errors.append("list_roles() 与文件重建结果不一致")
        errors.append(_first_diff(live_roles, file_roles))

    stack_spec = load_yaml(PROMPTS / "stack" / "stack.yaml")
    layer_stack.DRIVERS = list(stack_spec["drivers"])
    layer_stack.SKILL_CATEGORIES = list(stack_spec["skill_categories"])
    layer_stack.SKILLS = list(stack_spec["skills"])
    layer_stack.ROLES = list(stack_spec["roles"])
    layer_stack.TRIGGERS = list(stack_spec["triggers"])
    layer_stack.DEFAULT_SKILL_DRIVERS = dict(stack_spec["skill_drivers"])
    layer_stack.DEFAULT_ROLE_SKILLS = dict(stack_spec["role_skills"])
    layer_stack.DEFAULT_TRIGGER_ROLES = dict(stack_spec["trigger_roles"])
    layer_stack.DEFAULT_TRIGGER_SKILLS = dict(stack_spec["trigger_skills"])
    layer_stack._INTENT_LABEL = dict(stack_spec["intent_labels"])
    layer_stack._SKILL_IDS = {row["id"] for row in layer_stack.SKILLS}
    layer_stack._ROLE_IDS = {row["id"] for row in layer_stack.ROLES}
    bindings = stack_spec.get("bindings") or {}
    layer_stack._store = lambda: bindings if isinstance(bindings, dict) else {}
    file_stack = layer_stack.get_stack()
    if canonical_json(live_stack) != canonical_json(file_stack):
        errors.append("get_stack() 与 stack.yaml 重建结果不一致")
        errors.append(_first_diff(live_stack, file_stack))
    return errors


def _first_diff(left: Any, right: Any, path: str = "$") -> str:
    if type(left) is not type(right):
        return f"{path} 类型 {type(left).__name__} != {type(right).__name__}"
    if isinstance(left, dict):
        keys = list(dict.fromkeys([*left.keys(), *right.keys()]))
        for key in keys:
            if key not in left:
                return f"{path}.{key} 仅出现在文件侧"
            if key not in right:
                return f"{path}.{key} 仅出现在库侧"
            found = _first_diff(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return ""
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path} 长度 {len(left)} != {len(right)}"
        for i, (a, b) in enumerate(zip(left, right)):
            found = _first_diff(a, b, f"{path}[{i}]")
            if found:
                return found
        return ""
    if left != right:
        return f"{path} {left!r} != {right!r}"
    return ""


def main() -> int:
    _install_readonly()
    con = sqlite3.connect(f"file:{DATA_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        errors = _check_jobs(con) + _check_skills(con)
    finally:
        con.close()
    if errors:
        print("核对失败")
        for line in errors:
            print(line)
        return 1
    print("核对通过：库中 job 的 messages 与文件逐字节相同，技能 JSON 相同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
