#!/usr/bin/env python3
"""只读架构体检。

统计 MinoNexus 的 import 方向（**含函数内 import**）、模块级环、状态散落程度。
不改任何业务代码，不连数据库，不跑 pytest。

用法：
    python3 tools/arch_report.py               # 文本报告
    python3 tools/arch_report.py --json        # JSON
    python3 tools/arch_report.py --check       # 有违规时退出码 1
    python3 tools/arch_report.py --top 40      # fan-in 前 N

为什么必须扫函数内 import：本仓 4,205 处 `from mino_nexus ...` 里有 3,279 处在函数体内，
只扫顶层会把 78% 的真实依赖漏掉，看到的结构是假的。
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "mino_nexus")

# 目标分层（ring 越小越底层）。尚未迁移的旧包名单独标注，不参与强制校验。
LAYER = {
    "core": 0,
    "contract": 0,
    "models": 0,
    # 环 1：能力域
    "records": 1,
    "catalog": 1,
    "navigation": 1,
    "casework": 1,
    "resources": 1,
    "intelligence": 1,
    "knowledge": 1,
    "platform": 1,
    # 环 2：执行与适配
    "execution": 2,
    "connectors": 2,
    # 环 3/4
    "assistant": 3,
    "api": 4,
    # 旧名（迁移期）：loop/ai 等同 execution/intelligence，单独列出以便看差值
    "legacy:loop": 2,
    "legacy:ai": 1,
    "legacy:routers": 4,
    "legacy:websocket": 4,
    "legacy:runtime": 1,
    "legacy:action_space": 1,
    "legacy:semantic": 1,
    "legacy:services": -1,  # 混合层，不参与校验
    "legacy:prompts": -1,
    "<root>": 5,  # 装配根（app.py / cli.py），允许 import api
}

LEGACY = {
    "loop": "legacy:loop",
    "ai": "legacy:ai",
    "routers": "legacy:routers",
    "websocket": "legacy:websocket",
    "runtime": "legacy:runtime",
    "action_space": "legacy:action_space",
    "semantic": "legacy:semantic",
    "services": "legacy:services",
    "prompts": "legacy:prompts",
}

# 唯一允许 execution -> connectors 的文件
WIRING_ALLOW = {"mino_nexus.execution.schedule.wiring", "mino_nexus.loop.case_runner"}


def pkg_of(rel_path: str) -> str:
    parts = rel_path.split(os.sep)
    if len(parts) == 1:
        return "<root>"
    head = parts[0]
    return LEGACY.get(head, head)


def module_of(rel_path: str) -> str:
    return "mino_nexus." + rel_path[:-3].replace(os.sep, ".")


def imports_of(path: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("mino_nexus"):
            out.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mino_nexus"):
                    out.append((node.lineno, alias.name))
    return out


def module_names(tree: ast.AST) -> set[str]:
    return {m for _, m in imports_of_tree(tree)}


def imports_of_tree(tree: ast.AST) -> set[tuple[int, str]]:
    out: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("mino_nexus"):
            out.add((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mino_nexus"):
                    out.add((node.lineno, alias.name))
    return out


def scan() -> dict:
    modules: dict[str, dict] = {}
    total_imports = 0
    lazy_imports = 0
    setattr_total = 0
    ctx_setattr = 0
    files_with_setattr: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(PKG):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, PKG)
            mod = module_of(rel)
            src = open(path, encoding="utf-8").read()
            try:
                tree = ast.parse(src)
            except SyntaxError as exc:
                modules[mod] = {"rel": rel, "pkg": pkg_of(rel), "deps": [], "error": str(exc)}
                continue
            deps: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("mino_nexus"):
                    total_imports += 1
                    if node.col_offset > 0:
                        lazy_imports += 1
                    deps.append(node.module)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("mino_nexus"):
                            total_imports += 1
                            if node.col_offset > 0:
                                lazy_imports += 1
                            deps.append(alias.name)
            setattr_total += src.count("setattr(")
            ctx_setattr += src.count("setattr(ctx")
            if "setattr(" in src:
                files_with_setattr.add(rel)
            modules[mod] = {"rel": rel, "pkg": pkg_of(rel), "deps": deps, "error": None}

    edges = collections.Counter()
    for mod, info in modules.items():
        src_pkg = info["pkg"]
        for dep in info["deps"]:
            dst_pkg = dep.split(".")[1] if len(dep.split(".")) > 1 else "<root>"
            dst = LEGACY.get(dst_pkg, dst_pkg)
            if dst != src_pkg:
                edges[(src_pkg, dst)] += 1

    return {
        "modules": modules,
        "edges": edges,
        "total_imports": total_imports,
        "lazy_imports": lazy_imports,
        "setattr_total": setattr_total,
        "ctx_setattr": ctx_setattr,
        "files_with_setattr": len(files_with_setattr),
    }


def violations(data: dict) -> list[dict]:
    mods = data["modules"]
    out: list[dict] = []

    def add(rule: str, mod: str, dep: str, lineno: int = 0) -> None:
        out.append({"rule": rule, "module": mod, "imports": dep, "line": lineno})

    for mod, info in mods.items():
        pkg = info["pkg"]
        layer = LAYER.get(pkg, -1)
        for dep in info["deps"]:
            parts = dep.split(".")
            dst_raw = parts[1] if len(parts) > 1 else "<root>"
            dst = LEGACY.get(dst_raw, dst_raw)
            dst_layer = LAYER.get(dst, -1)

            # R1：底座不得 import 上层
            if pkg in {"core", "contract", "models"} and dst not in {"core", "contract", "models"}:
                add("R1-base-imports-upper", mod, dep)
            # R2：records 是叶子
            if pkg == "records" and dst not in {"core", "contract", "models"}:
                add("R2-records-not-leaf", mod, dep)
            # R3：任何域不得 import api
            if pkg not in {"api", "legacy:routers", "legacy:websocket", "<root>"} and dst in {"api", "legacy:routers"}:
                add("R3-domain-imports-api", mod, dep)
            # R5：execution 不得 import connectors（wiring 例外）
            if layer == 2 and pkg in {"execution", "legacy:loop"} and dst == "connectors" and mod not in WIRING_ALLOW:
                add("R5-execution-imports-connectors", mod, dep)
            # R6：connectors 不得 import execution
            if pkg == "connectors" and dst in {"execution", "legacy:loop"}:
                add("R6-connectors-imports-execution", mod, dep)
            # R7：assistant 不得 import api
            if pkg == "assistant" and dst in {"api", "legacy:routers"}:
                add("R7-assistant-imports-api", mod, dep)
    return out


def cycles(data: dict) -> list[list[str]]:
    """Tarjan SCC on module graph（模块级，只看 mino_nexus 内部边）。"""
    mods = data["modules"]
    graph: dict[str, list[str]] = {}
    for mod, info in mods.items():
        graph[mod] = []
    for mod, info in mods.items():
        for dep in info["deps"]:
            # dep 可能是包或模块；能精确匹配就加边
            if dep in mods and dep != mod:
                graph[mod].append(dep)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = [0]

    def strongconnect(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, []):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                result.append(sorted(comp))

    for v in graph:
        if v not in index:
            strongconnect(v)
    result.sort(key=len, reverse=True)
    return result


def package_cycles(data: dict) -> list[list[str]]:
    """包级 SCC：跨包环才是"无法单独理解一个包"的原因。包内环单独看。"""
    graph: dict[str, set[str]] = collections.defaultdict(set)
    for info in data["modules"].values():
        src = info["pkg"]
        for dep in info["deps"]:
            parts = dep.split(".")
            dst_raw = parts[1] if len(parts) > 1 else "<root>"
            dst = LEGACY.get(dst_raw, dst_raw)
            if dst != src:
                graph[src].add(dst)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    n = [0]

    def sc(v: str) -> None:
        index[v] = low[v] = n[0]
        n[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                sc(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    for v in list(graph):
        if v not in index:
            sc(v)
    out.sort(key=len, reverse=True)
    return out


def fan_in(data: dict) -> list[tuple[str, int]]:
    counter: collections.Counter[str] = collections.Counter()
    for info in data["modules"].values():
        for dep in info["deps"]:
            counter[dep] += 1
    return counter.most_common()


def report(data: dict, top: int = 25) -> str:
    lines: list[str] = []
    mods = data["modules"]
    total = data["total_imports"]
    lazy = data["lazy_imports"]
    lines.append("=" * 78)
    lines.append("MinoNexus 架构体检（只读）")
    lines.append("=" * 78)
    lines.append(f"模块数（含 __init__）       {len(mods)}")
    lines.append(f"mino_nexus import 总数       {total}")
    pct = (100.0 * lazy / total) if total else 0.0
    lines.append(f"其中函数内 import（延迟）    {lazy}  ({pct:.1f}%)")
    lines.append(f"setattr(...) 总数            {data['setattr_total']}")
    lines.append(f"setattr(ctx, ...)            {data['ctx_setattr']}")
    lines.append(f"含 setattr 的文件            {data['files_with_setattr']}")
    lines.append("")

    lines.append("-- 包间依赖（只列跨包，按条数） " + "-" * 40)
    for (src, dst), count in data["edges"].most_common(30):
        lines.append(f"  {src:24} -> {dst:24} {count}")
    lines.append("")

    psccs = package_cycles(data)
    lines.append(f"-- 包级环：{len(psccs)} 个 SCC " + "-" * 45)
    for comp in psccs:
        lines.append(f"  [{len(comp)}] " + ", ".join(comp))
    lines.append("")

    sccs = cycles(data)
    lines.append(f"-- 模块级环：{len(sccs)} 个 SCC（>1 模块） " + "-" * 30)
    for comp in sccs[:10]:
        lines.append(f"  [{len(comp)}] " + ", ".join(m.replace("mino_nexus.", "") for m in comp[:8])
                     + (" ..." if len(comp) > 8 else ""))
    lines.append("")

    viol = violations(data)
    by_rule: dict[str, list[dict]] = collections.defaultdict(list)
    for v in viol:
        by_rule[v["rule"]].append(v)
    lines.append(f"-- 分层规则违规：{len(viol)} 条 " + "-" * 40)
    for rule in sorted(by_rule):
        rows = by_rule[rule]
        lines.append(f"  {rule:36} {len(rows)}")
        for row in rows[:8]:
            lines.append(f"      {row['module'].replace('mino_nexus.', '')} -> {row['imports'].replace('mino_nexus.', '')}")
    lines.append("")

    lines.append(f"-- fan-in 前 {top} " + "-" * 40)
    for mod, count in fan_in(data)[:top]:
        lines.append(f"  {count:4}  {mod.replace('mino_nexus.', '')}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args()

    data = scan()
    if args.json:
        payload = {
            "modules": len(data["modules"]),
            "total_imports": data["total_imports"],
            "lazy_imports": data["lazy_imports"],
            "setattr_total": data["setattr_total"],
            "ctx_setattr": data["ctx_setattr"],
            "edges": {f"{s}->{d}": c for (s, d), c in data["edges"].items()},
            "cycles": cycles(data),
            "package_cycles": package_cycles(data),
            "violations": violations(data),
            "fan_in": [{"module": m, "count": c} for m, c in fan_in(data)[: args.top]],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(report(data, top=args.top))

    if args.check:
        viol = violations(data)
        if viol:
            print(f"\n[FAIL] {len(viol)} 条分层规则违规", file=sys.stderr)
            return 1
        print("\n[OK] 分层规则通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
