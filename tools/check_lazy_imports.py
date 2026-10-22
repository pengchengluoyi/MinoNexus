"""函数内 import 的解析检查（只读）。

`compileall` / `create_app()` / `arch_report` 都看不见**函数体内的 import**：
模块能编译、能导入，直到某条接口第一次走到那行才炸。2026-10-22 的
`GET /project/{id}/collab/workspaces/{id}` 500 就是这么来的 ——
`case_store._public` 里 `from ... import stored_preview_lines`，那个名字
已经被搬到 `contract/case_preview.py` 了。

本工具把每个 `mino_nexus/**/*.py` 里出现在函数体内的 import 逐个解析，
名字不存在就报错。用法：

    python3 tools/check_lazy_imports.py          # 打印问题，exit 1
    python3 tools/check_lazy_imports.py -v       # 同时打印检查了多少条

只读：不写文件、不连数据库、不发网络请求。
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "mino_nexus"


def _prepare_env() -> tempfile.TemporaryDirectory:
    """导入期 `core/paths.py` 会建目录；给它一个临时根，别碰真实数据。"""
    tmp = tempfile.TemporaryDirectory(prefix="mino-lazy-import-")
    os.environ["MINO_NEXUS_DATA_DIR"] = tmp.name
    os.environ["MINO_NEXUS_DEV_DATA_DIR"] = tmp.name
    sys.path.insert(0, str(ROOT))
    return tmp


def _is_submodule(module: str, name: str) -> bool:
    try:
        importlib.import_module(f"{module}.{name}")
        return True
    except Exception:
        return False


def scan() -> list[tuple[str, int, str]]:
    problems: list[tuple[str, int, str]] = []
    checked = 0
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append((rel, exc.lineno or 0, f"SyntaxError: {exc.msg}"))
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Import):
                    targets = [(alias.name, alias.asname, None) for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    targets = [(node.module, None, alias.name) for alias in node.names]
                else:
                    continue
                for module, alias, name in targets:
                    if not module.startswith("mino_nexus"):
                        continue
                    checked += 1
                    try:
                        mod = importlib.import_module(module)
                    except Exception as exc:  # 模块本身都进不去
                        problems.append((rel, node.lineno, f"import {module}: {exc!r}"))
                        continue
                    if name is None or name == "*" or hasattr(mod, name):
                        continue
                    # `from pkg import 子模块` 是合法的：属性在首次访问时才绑定。
                    if _is_submodule(module, name):
                        continue
                    problems.append((rel, node.lineno, f"from {module} import {name}: 名字不存在"))
    if os.environ.get("MINO_LAZY_IMPORT_VERBOSE"):
        print(f"检查了 {checked} 条函数内 import")
    return problems


def main(argv: list[str]) -> int:
    if "-v" in argv or "--verbose" in argv:
        os.environ["MINO_LAZY_IMPORT_VERBOSE"] = "1"
    tmp = _prepare_env()
    try:
        problems = scan()
    finally:
        tmp.cleanup()
    if not problems:
        print("[OK] 函数内 import 全部可解析")
        return 0
    print(f"[FAIL] {len(problems)} 条函数内 import 无法解析")
    print("=" * 78)
    for rel, line, message in problems:
        print(f"  {rel}:{line}")
        print(f"      {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
