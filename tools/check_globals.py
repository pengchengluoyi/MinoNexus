"""未定义全局名的静态检查（只读）。

`compileall` 能过、`arch_report` 能过、`check_lazy_imports` 也能过 —— 它们都不检查
「用了一个从没导入、也没定义的名字」。2026-10-22 的 `NameError: name 'line_text'
is not defined` 就是这么来的：`loop/execution/budget.py` 从大文件里被拆出来时，
把 `from mino_nexus.core.case_text import line_text` 落下了，整个包照常导入，
第一条用例一跑就炸。

做法：用 `symtable` 看每个模块里「被引用但没有绑定」的名字。
判定标准（保守，只报确定错的）：

- 名字在本模块任意作用域被绑定过（赋值、def/class、import、参数、for、with、except）
  → 不算错（可能是闭包/延迟绑定）。
- 名字是内置名、`__dunder__` → 不算错。
- 其余「只被读、从未在任何地方绑定」的名字 → 报错。

用法：

    python3 tools/check_globals.py          # 有问题 exit 1
    python3 tools/check_globals.py -v       # 打印扫描规模

只读，不导入目标模块，不执行任何业务代码。
"""
from __future__ import annotations

import ast
import builtins
import symtable
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "mino_nexus"
_BUILTINS = set(dir(builtins))


def _collect(table: symtable.SymbolTable, bound: set[str], used: set[str]) -> None:
    for sym in table.get_symbols():
        name = sym.get_name()
        if sym.is_assigned() or sym.is_imported() or sym.is_parameter() or sym.is_namespace():
            bound.add(name)
    for child in table.get_children():
        _collect(child, bound, used)
    # 引用必须在收集完全部子表之后再做，否则闭包里的绑定会被误判成未定义。
    for sym in table.get_symbols():
        if sym.is_referenced() and not sym.is_assigned() and not sym.is_imported() and not sym.is_parameter():
            used.add(sym.get_name())


def _is_global_stmt(table: symtable.SymbolTable, name: str) -> bool:
    try:
        return table.lookup(name).is_global() or table.lookup(name).is_declared_global()
    except KeyError:
        return False


def scan_module(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    top = symtable.symtable(src, str(path), "exec")
    bound: set[str] = set()
    used: set[str] = set()
    _collect(top, bound, used)
    problems = []
    for name in sorted(used - bound):
        if name in _BUILTINS or (name.startswith("__") and name.endswith("__")):
            continue
        problems.append((0, name))
    return problems


def _module_bindings(tree: ast.AST) -> dict[str, str]:
    """模块级已绑定的名字 → 它来自哪个模块（用于判断局部导入是不是重复）。"""
    top: dict[str, str] = {}
    for st in getattr(tree, "body", []):
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            src = st.module if isinstance(st, ast.ImportFrom) else None
            for alias in st.names:
                name = (alias.asname or alias.name).split(".")[0]
                if name and name != "*":
                    top[name] = f"{src}.{alias.name}" if src else alias.name
    return top


def _walk_imports(stmts, chain):
    for st in stmts:
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            yield st, chain
        elif isinstance(st, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
            for attr in ("body", "orelse", "finalbody"):
                sub = getattr(st, attr, None)
                if isinstance(sub, list):
                    yield from _walk_imports(sub, chain + [st])
            for handler in getattr(st, "handlers", []) or []:
                yield from _walk_imports(handler.body, chain + [st])


def scan_shadowed(path: Path) -> list[str]:
    """函数内重复导入一个模块级已有的名字，且这个导入嵌在 if/for/while 分支里。

    这种写法会让该名字在整个函数里变成局部变量：分支没走到 → `UnboundLocalError`。
    2026-10-22 的 `cannot access local variable 'PlanEvent'` 就是这个（`preflight.py`
    在 `if opened:` 里又导入了一次 `PlanEvent`，安卓路径 `opened is None` 直接跳过）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top = _module_bindings(tree)
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        reassigned = {
            n.id for n in ast.walk(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }
        for node, chain in _walk_imports(fn.body, []):
            if not any(isinstance(c, (ast.If, ast.For, ast.While)) for c in chain):
                continue
            src = node.module if isinstance(node, ast.ImportFrom) else None
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                if name not in top or name in reassigned:
                    continue
                origin = f"{src}.{alias.name}" if src else alias.name
                if origin != top[name]:
                    continue
                block = chain[-1]
                end = getattr(block, "end_lineno", block.lineno)
                outside = [
                    n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Name) and n.id == name
                    and isinstance(n.ctx, ast.Load) and n.lineno > end
                ]
                if outside:
                    found.append(
                        f"{fn.name}() 里第 {node.lineno} 行重复导入 `{name}`（模块级已有），"
                        f"分支未走到时 L{outside[0]} 会 UnboundLocalError"
                    )
    return found


def scan() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        try:
            for _line, name in scan_module(path):
                found.append((rel, name))
            for message in scan_shadowed(path):
                found.append((rel, message))
        except SyntaxError as exc:
            found.append((rel, f"SyntaxError: {exc.msg}"))
    return found


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    files = [p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts]
    found = scan()
    if not found:
        if verbose:
            print(f"扫描了 {len(files)} 个模块，没有未定义的全局名")
        print("[OK] 没有未定义的全局名")
        return 0
    print(f"[FAIL] {len(found)} 处未定义的全局名（首次执行到就会 NameError）")
    print("=" * 78)
    for rel, name in found:
        print(f"  {rel}")
        print(f"      {name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
