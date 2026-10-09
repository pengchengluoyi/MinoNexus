#!/usr/bin/env python3
"""校验 prompts/：格式、渲染、引用，以及 version 与内容哈希。

内容变了但 version 没有加 1 就失败。通过后把新哈希写回 prompts.lock.json。
这是发版前要跑的脚本，不是 pytest。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mino_nexus.ai.prompt_repo import (  # noqa: E402
    PromptRepoError,
    content_sha256,
    load_yaml,
    prompts_dir,
)

PROMPTS = prompts_dir()
LOCK_PATH = PROMPTS / "prompts.lock.json"


def _files(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file() and p.name != ".gitkeep"]


def _collect() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def add(key: str, version: int, directory: Path, files: list[Path]) -> None:
        found[key] = {
            "version": int(version),
            "sha256": content_sha256(files, root=directory),
        }

    for directory in sorted(p for p in (PROMPTS / "jobs").iterdir() if p.is_dir()):
        spec = load_yaml(directory / "job.yaml")
        add(f"jobs/{spec['id']}", int(spec["version"]), directory, _files(directory))
    assistant_jobs = PROMPTS / "assistant" / "jobs"
    if assistant_jobs.is_dir():
        for directory in sorted(p for p in assistant_jobs.iterdir() if p.is_dir()):
            spec_path = directory / "job.yaml"
            if not spec_path.is_file():
                continue
            spec = load_yaml(spec_path)
            add(f"assistant/jobs/{spec['id']}", int(spec["version"]), directory, _files(directory))
    for folder, prefix in (
        (PROMPTS / "skills", "skills"),
        (PROMPTS / "roles", "roles"),
        (PROMPTS / "assistant" / "tools", "assistant/tools"),
        (PROMPTS / "assistant" / "cards", "assistant/cards"),
    ):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            spec = load_yaml(path)
            add(f"{prefix}/{spec['id']}", int(spec["version"]), folder, [path])
    stack = PROMPTS / "stack" / "stack.yaml"
    if stack.is_file():
        spec = load_yaml(stack)
        add("stack/stack", int(spec.get("version") or 1), stack.parent, [stack])
    return found


def main() -> int:
    current = _collect()
    locked = {}
    if LOCK_PATH.is_file():
        locked = json.loads(LOCK_PATH.read_text(encoding="utf-8")).get("entries") or {}
    errors: list[str] = []
    for key, row in current.items():
        old = locked.get(key)
        if not isinstance(old, dict):
            continue
        if row["sha256"] != old.get("sha256") and int(row["version"]) <= int(old.get("version") or 0):
            errors.append(
                f"{key} 内容变了但 version 仍是 {row['version']}（lock 里是 {old.get('version')}）。改内容时要把 version 加 1"
            )
    if errors:
        print("校验失败")
        for line in errors:
            print(line)
        return 1
    LOCK_PATH.write_text(
        json.dumps({"entries": dict(sorted(current.items()))}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        from mino_nexus.ai.prompt_repo import load_all

        repo = load_all()
    except PromptRepoError as exc:
        print(f"校验失败：{exc}")
        return 1
    print(f"校验通过：{len(repo.jobs)} 个 job，{len(repo.skills)} 个技能，{len(repo.roles)} 个角色")
    return 0


if __name__ == "__main__":
    sys.exit(main())
