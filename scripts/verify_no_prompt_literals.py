#!/usr/bin/env python3
"""拦截写回 .py 的 prompt 字面量。中英文都算。"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOT = ROOT / "mino_nexus"
MIN_LEN = 80


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_long(node: ast.AST) -> bool:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    return len(node.value.strip()) >= MIN_LEN


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        names = [_name(t) for t in node.targets]
        interesting = any("SYSTEM" in n or "PROMPT" in n for n in names)
        if interesting and _is_long(node.value):
            snippet = str(node.value.value).strip().replace("\n", " ")[:60]
            self.hits.append((node.lineno, snippet))
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        role = ""
        content: ast.AST | None = None
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "role" and isinstance(value, ast.Constant):
                role = str(value.value)
            if isinstance(key, ast.Constant) and key.value == "content":
                content = value
        if role == "system" and content is not None and _is_long(content):
            snippet = str(content.value).strip().replace("\n", " ")[:60]
            self.hits.append((content.lineno, snippet))
        self.generic_visit(node)


def scan_file(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return [(exc.lineno or 0, f"语法错误：{exc}")]
    visitor = _Visitor()
    visitor.visit(tree)
    return visitor.hits


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
    print(f"OK — no prompt literals under {SCAN_ROOT.name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
