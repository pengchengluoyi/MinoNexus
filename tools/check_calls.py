"""调用关键字与形参对不上的静态检查（只读）。

`check_globals` 管「名字没导入」，这个管另一半：**名字导入了、函数也在，但传了
一个它没有的关键字参数**。这类错误导入期完全看不出来，只有真正调用时才
`TypeError: got an unexpected keyword argument`（或者更糟 —— 因为调用点里那个
变量本身没定义，先炸成 `UnboundLocalError`）。

2026-10-22 的 `cannot access local variable 'cap_id'` 就是这样：抽取 `run_login_nav`
时给函数加了个 `cap_id` 形参，调用点也照抄了 `cap_id=cap_id`，但那个位置函数体
里还没定义 `cap_id`（它在 200 行之后才算出来），于是每条用例一进登录导航就炸。

判定范围（保守，只报能确定的）：

- 被调用方能用模块级 import 解析到包内函数（`from x import f` 或 `import x.y as y`）；
- 该函数的签名里没有 `**kwargs`（有就没法判断）；
- 调用点显式写了关键字参数，且该名字不在形参集合里 → 报错。

不解析 `obj.method(...)`、局部变量里存的函数、`getattr` 取的函数 —— 这些不猜。

用法：

    python3 tools/check_calls.py          # 有问题 exit 1
    python3 tools/check_calls.py -v       # 打印扫描规模

只读，不导入目标模块，不执行任何业务代码。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "mino_nexus"


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _signatures(tree: ast.AST) -> dict[str, tuple[set[str], bool]]:
    """模块级函数 → (形参名集合, 是否有 **kwargs)。"""
    out: dict[str, tuple[set[str], bool]] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            names = {
                a.arg
                for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            }
            out[node.name] = (names, args.kwarg is not None)
    return out


def scan() -> list[tuple[str, str]]:
    trees: dict[str, ast.AST] = {}
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            trees[_module_name(path)] = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
    sigs = {mod: _signatures(tree) for mod, tree in trees.items()}
    modules = set(trees)

    found: list[tuple[str, str]] = []
    for mod, tree in trees.items():
        # 模块级名字 → 目标 (模块, 函数名)
        named: dict[str, tuple[str, str]] = {}
        aliases: dict[str, str] = {}
        for node in getattr(tree, "body", []):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("mino_nexus"):
                        bound = alias.asname or alias.name.split(".")[0]
                        aliases[bound] = alias.name if alias.asname else alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("mino_nexus"):
                for alias in node.names:
                    target_mod = f"{node.module}.{alias.name}"
                    if target_mod in modules:
                        aliases[alias.asname or alias.name] = target_mod
                    else:
                        named[alias.asname or alias.name] = (node.module, alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            target: tuple[str, str] | None = None
            if isinstance(func, ast.Name) and func.id in named:
                target = named[func.id]
            elif isinstance(func, ast.Name) and func.id in sigs[mod]:
                target = (mod, func.id)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                target_mod = aliases.get(func.value.id)
                if target_mod:
                    target = (target_mod, func.attr)
            if not target:
                continue
            sig = sigs.get(target[0], {}).get(target[1])
            if sig is None:
                continue
            params, has_kwargs = sig
            if has_kwargs:
                continue
            for keyword in node.keywords:
                if keyword.arg and keyword.arg not in params:
                    rel = Path(*mod.split(".")).with_suffix(".py").as_posix()
                    found.append((
                        rel,
                        f"L{node.lineno} 调用 {target[0].split('.')[-1]}.{target[1]}() 传了不存在的参数 "
                        f"`{keyword.arg}`（形参: {', '.join(sorted(params)) or '无'}）",
                    ))
    return found


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    found = scan()
    if not found:
        if verbose:
            files = [p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts]
            print(f"扫描了 {len(files)} 个模块，关键字参数与形参一致")
        print("[OK] 关键字参数与形参一致")
        return 0
    print(f"[FAIL] {len(found)} 处调用传了不存在的关键字参数（首次调用就 TypeError）")
    print("=" * 78)
    for rel, message in found:
        print(f"  {rel}")
        print(f"      {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
