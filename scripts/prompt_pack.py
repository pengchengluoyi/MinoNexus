"""prompt 文件的读写与内容哈希。

导出、核对、以及之后的 `prompt_repo` 共用同一套规则，避免「写进去」和「读出来」各算一遍。
`prompts/` 目录本身不放 Python。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from mino_nexus.ai.prompt_repo import content_sha256, expand_blocks, job_to_public, load_yaml

# 超过这个长度，或本身带换行，就拆成独立 .md。短句留在 job.yaml 里。
_INLINE_TEXT_MAX = 80

_BLOCK_KEYS = (
    "id",
    "file",
    "text",
    "slot",
    "heading",
    "empty_text",
    "skip_if_empty",
    "max_chars",
    "enabled",
)


class _LiteralDumper(yaml.SafeDumper):
    pass


def _repr_str(dumper: yaml.SafeDumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralDumper.add_representer(str, _repr_str)


def dump_yaml(data: Any) -> str:
    text = yaml.dump(
        data,
        Dumper=_LiteralDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )
    if not text.endswith("\n"):
        text += "\n"
    return text


def entry_files(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file() and p.name != ".gitkeep"]


def _ordered_block(block: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _BLOCK_KEYS:
        if key in block:
            out[key] = block[key]
    for key, value in block.items():
        if key not in out:
            out[key] = value
    return out


def split_blocks(blocks: list[dict[str, Any]], *, side: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """长文本块改成 file: 引用。返回 yaml 块列表，以及相对路径 → 原文。"""
    yaml_blocks: list[dict[str, Any]] = []
    files: dict[str, str] = {}
    n = 0
    for raw in blocks:
        if not isinstance(raw, dict):
            continue
        block = dict(raw)
        text = block.get("text")
        text_s = text if isinstance(text, str) else ""
        long = bool(text_s) and (len(text_s) > _INLINE_TEXT_MAX or "\n" in text_s)
        if long:
            n += 1
            ident = str(block.get("id") or f"block{n}").strip() or f"block{n}"
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in ident)
            rel = f"{side}/{n * 10:02d}-{safe}.md"
            files[rel] = text_s
            block.pop("text", None)
            block["file"] = rel
        yaml_blocks.append(_ordered_block(block))
    return yaml_blocks, files


def load_job_dir(directory: Path) -> dict[str, Any]:
    spec = load_yaml(directory / "job.yaml")
    if not isinstance(spec, dict):
        raise ValueError(f"{directory} 的 job.yaml 不是映射")
    return job_to_public(spec, directory)


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def messages_bytes(messages: Any) -> bytes:
    return json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
