#!/usr/bin/env python3
"""守门：目录结构必须与 CLAUDE.md §3 一致。

**为什么需要这一条**：CLAUDE.md §3 是新来的人（和模型）判断"东西该放哪"的唯一依据。
它一过期，文件就会照着错的结构落 —— 已经发生过：§3 里写着 `models/` `core/` `services/`
三个目录，实际一个都不存在（ORM 被 JSON store 替掉了），而根目录悄悄堆到 26 个模块。

同类事故还有：一个**空的** `loop/local_executors/__init__.py` 目录盖住了同名模块
`local_executors.py`，`agent_loop` 一 import 就 ImportError，而 CI 全绿一整天。
所以这里也顺手拦"包和模块同名"。

改结构的正确姿势：**先改 CLAUDE.md §3，再改这里的白名单，再动文件**，三者同一个 commit。
纯静态，裸 python3 可跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "mino_nexus"

# 允许出现在 mino_nexus/ 根目录的模块。与 CLAUDE.md §3 逐条对应。
ROOT_MODULES = {
    # 入口与契约
    "app.py", "cli.py", "protocol.py", "schemas.py", "log.py", "paths.py",
    # 基础设施
    "json_store.py", "http_util.py", "client_gate.py", "runtime_tokens.py",
    "node_registry.py", "mdns.py",
    # 配置（两套，分工见 CLAUDE.md §8）
    "settings.py", "settings_store.py",
    # 业务服务（欠账：应收进 services/，见 CLAUDE.md §3 末尾）
    "app_automation.py", "project_env.py", "project_store.py", "auth_store.py",
    "studio_nav.py", "plugins_store.py", "node_store.py", "device_store.py",
    "device_secrets.py", "knowledge_jobs_store.py",
    "run_store.py", "task_store.py", "qa_cover.py", "qa_process_assist.py",
    "qa_process_jobs.py", "figma_service.py", "figma_logic.py", "atlas_aliases.py",
    "ui_devices.py",
}

# 允许的子包
SUB_PACKAGES = {"ai", "catalog", "loop", "routers", "runtime", "websocket"}


def main() -> int:
    errors: list[str] = []

    if not PKG.is_dir():
        print(f"FAIL verify_layout — 找不到 {PKG}")
        return 1

    # ---- 根目录模块 ----
    actual_mods = {p.name for p in PKG.glob("*.py") if p.name != "__init__.py"}
    for extra in sorted(actual_mods - ROOT_MODULES):
        errors.append(
            f"根目录多了 {extra} —— 要么放进对应子包，要么先在 CLAUDE.md §3 "
            f"和本文件白名单里登记（别只加白名单）"
        )
    for gone in sorted(ROOT_MODULES - actual_mods):
        errors.append(f"白名单里的 {gone} 不存在了 —— 删/改名后请同步 CLAUDE.md §3 与本文件")

    # ---- 子包 ----
    actual_pkgs = {p.name for p in PKG.iterdir() if p.is_dir() and p.name != "__pycache__"}
    for extra in sorted(actual_pkgs - SUB_PACKAGES):
        errors.append(f"多了子包 {extra}/ —— 同上，先登记再加")
    for gone in sorted(SUB_PACKAGES - actual_pkgs):
        errors.append(f"子包 {gone}/ 不存在了")

    # ---- 包与模块同名（Python 会选包，静默盖住模块）----
    for name in sorted(actual_pkgs):
        if f"{name}.py" in actual_mods:
            errors.append(
                f"{name}/ 和 {name}.py 同名：Python 会选包，模块被静默盖住。"
                f"（踩过一次：空的 local_executors/ 盖住 local_executors.py）"
            )
    for sub in sorted(actual_pkgs):
        d = PKG / sub
        sub_pkgs = {p.name for p in d.iterdir() if p.is_dir() and p.name != "__pycache__"}
        sub_mods = {p.name for p in d.glob("*.py")}
        for name in sorted(sub_pkgs):
            if f"{name}.py" in sub_mods:
                errors.append(f"{sub}/{name}/ 和 {sub}/{name}.py 同名，模块被静默盖住")

    # ---- 空包（往往是遗留的占位目录，正是上面那类事故的来源）----
    for sub in sorted(actual_pkgs):
        d = PKG / sub
        if not [p for p in d.glob("*.py") if p.name != "__init__.py"]:
            init = d / "__init__.py"
            if not init.is_file() or not init.read_text(encoding="utf-8").strip():
                errors.append(f"{sub}/ 是空包（只有空 __init__.py）—— 删掉，别留占位目录")

    if errors:
        print("FAIL verify_layout")
        for e in errors:
            print("  " + e)
        print("\n  改结构的顺序：CLAUDE.md §3 → 本文件白名单 → 动文件（同一个 commit）")
        return 1

    print(
        f"OK verify_layout — 根目录 {len(actual_mods)} 个模块、{len(actual_pkgs)} 个子包，"
        f"与 CLAUDE.md §3 一致"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
