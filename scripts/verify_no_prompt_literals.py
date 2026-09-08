#!/usr/bin/env python3
"""扫描 mino_nexus 模块级 ALL_CAPS 长中文字符串，防止 prompt 回流到 .py。"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOT = ROOT / "mino_nexus"
MIN_LEN = 80
_CJK = re.compile(r"[\u4e00-\u9fff]{8,}")
_ALL_CAPS = re.compile(r"^[A-Z][A-Z0-9_]+$")


def _assign_names(node: ast.Assign) -> list[str]:
    out: list[str] = []
    for target in node.targets:
        if isinstance(target, ast.Name):
            out.append(target.id)
    return out


def _is_prompt_literal(node: ast.AST) -> bool:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    text = node.value.strip()
    if len(text) < MIN_LEN:
        return False
    return bool(_CJK.search(text))


def scan_file(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [(e.lineno or 0, f"语法错误：{e}")]
    hits: list[tuple[int, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = _assign_names(node)
        if not any(_ALL_CAPS.match(n) for n in names):
            continue
        if _is_prompt_literal(node.value):
            snippet = str(node.value.value).strip().replace("\n", " ")[:60]
            hits.append((node.lineno, snippet))
    return hits


def main() -> int:
    failures: list[str] = []
    for path in sorted(SCAN_ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        for line, snippet in scan_file(path):
            failures.append(f"{rel}:{line} {snippet}")
    if failures:
        print("prompt literals found:")
        for row in failures:
            print(" ", row)
        return 1
    print(f"OK — no module-level prompt literals under {SCAN_ROOT.name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
