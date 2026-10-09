"""prompt 文件的读写与内容哈希。

导出、核对、以及之后的 `prompt_repo` 共用同一套规则，避免「写进去」和「读出来」各算一遍。
`prompts/` 目录本身不放 Python。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

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


def load_yaml(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    return data if data is not None else {}


def content_sha256(paths: list[Path], *, root: Path) -> str:
    """条目内全部文件的哈希。路径按相对 posix 排序，避免机器差异。"""
    digest = hashlib.sha256()
    files = sorted(paths, key=lambda p: p.relative_to(root).as_posix())
    for path in files:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(rel)
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


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


def expand_blocks(base: Path, blocks: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in blocks or []:
        if not isinstance(raw, dict):
            continue
        block = dict(raw)
        rel = block.pop("file", None)
        if rel:
            path = base / str(rel)
            block["text"] = path.read_text(encoding="utf-8")
        out.append(block)
    return out


def job_to_public(spec: dict[str, Any], blocks_base: Path) -> dict[str, Any]:
    """文件规格 → 与 job_store._public 同形状的字典，供 render_job 使用。"""
    role = spec.get("role", spec.get("role_id", ""))
    version = spec.get("version", spec.get("prompt_version", 1))
    return {
        "id": str(spec.get("id") or ""),
        "label": str(spec.get("label") or spec.get("id") or ""),
        "summary": str(spec.get("summary") or ""),
        "engine": str(spec.get("engine") or "text_chat"),
        "role_id": str(role or ""),
        "output_schema": str(spec.get("output_schema") or ""),
        "slots": list(spec.get("slots") or []),
        "flags": list(spec.get("flags") or []),
        "system_blocks": expand_blocks(blocks_base, spec.get("system")),
        "user_blocks": expand_blocks(blocks_base, spec.get("user")),
        "image": dict(spec.get("image") or {}),
        "call": dict(spec.get("call") or {}),
        "enabled": spec.get("enabled", True) is not False,
        "builtin": False,
        "sort_order": int(spec.get("sort_order") or 0),
        "seed_rev": "",
        "prompt_version": int(version or 1),
        "overrides_json": {},
    }


def load_job_dir(directory: Path) -> dict[str, Any]:
    spec = load_yaml(directory / "job.yaml")
    if not isinstance(spec, dict):
        raise ValueError(f"{directory} 的 job.yaml 不是映射")
    return job_to_public(spec, directory)


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def messages_bytes(messages: Any) -> bytes:
    return json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
