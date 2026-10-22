"""函数可能「掉出末尾」返回 None 的静态检查（只读）。

`check_globals` 管没导入的名字，`check_calls` 管参数对不上，这个管第三种：
**函数声明了自己返回什么，但某条路径走到最后没有 return** —— 调用方拿到 None，
再取属性就是 `AttributeError: 'NoneType' object has no attribute ...`。

2026-10-22 的 `apply_turn_prior` 就是这样：末尾那句 `return PriorFlow(cont=True)`
被误缩进进了 `if input_guard == "reask":` 分支，正常路径掉出函数返回 None，
调用方 `_prior.leave` 直接崩，而静态检查全绿、编译也全绿。

判定方式（保守）：

- 只检查有返回注解、且注解不是 `None` / `Optional[...]` / `... | None` 的函数；
- 从函数体最后一条语句往回看，如果它（以及嵌套的 if/else 两边）都以
  `return` / `raise` / `while True` / `sys.exit` 结束，就不算；
- 否则报「可能掉出末尾」。

故意不做的判断：`try/finally` 里 return 的复杂情形、生成器、`yield`。
这些不猜，宁可不报。

用法：

    python3 tools/check_returns.py          # 有问题 exit 1
    python3 tools/check_returns.py -v       # 打印扫描规模

只读，不导入目标模块，不执行任何业务代码。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "mino_nexus"


def _returns_none_only(annotation: ast.AST | None) -> bool:
    if annotation is None:
        return True
    text = ast.unparse(annotation) if hasattr(ast, "unparse") else ""
    flat = text.replace(" ", "")
    if flat in {"None", "NoneType", "NoReturn", "Never", "..."}:
        return True
    if flat.startswith("Optional[") and "None" in flat:
        return True
    if flat.endswith("|None"):
        return True
    return False


def _terminates(stmts: list[ast.stmt]) -> bool:
    """这一串语句执行完是否一定不会继续往下。"""
    for st in reversed(stmts):
        if isinstance(st, (ast.Return, ast.Raise)):
            return True
        if isinstance(st, ast.While) and isinstance(st.test, ast.Constant) and st.test.value is True:
            return True
        if isinstance(st, ast.If):
            body_ok = _terminates(st.body)
            else_ok = _terminates(st.orelse) if st.orelse else False
            if body_ok and else_ok:
                return True
            continue
        if isinstance(st, (ast.With, ast.AsyncWith)):
            if _terminates(st.body):
                return True
            continue
        if isinstance(st, ast.Try):
            body_ok = _terminates(st.body)
            handlers_ok = all(_terminates(h.body) for h in st.handlers) if st.handlers else True
            else_ok = _terminates(st.orelse) if st.orelse else True
            if body_ok and handlers_ok and else_ok:
                return True
            continue
        break
    return False


def scan() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _returns_none_only(node.returns):
                continue
            if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)):
                continue  # 生成器：yield 之后返回 None 是正常语义
            body = [st for st in node.body if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str))]
            if not body or _terminates(body):
                continue
            found.append((
                rel,
                f"{node.name}() 声明返回 {ast.unparse(node.returns)}，但最后一条语句不是 return —— "
                f"走到末尾会返回 None",
            ))
    return found


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    found = scan()
    if not found:
        if verbose:
            files = [p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts]
            print(f"扫描了 {len(files)} 个模块，没有会掉出末尾的函数")
        print("[OK] 没有会掉出末尾的函数")
        return 0
    print(f"[FAIL] {len(found)} 个函数可能返回 None（调用方取属性会 AttributeError）")
    print("=" * 78)
    for rel, message in found:
        print(f"  {rel}")
        print(f"      {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
