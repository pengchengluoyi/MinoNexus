"""导入冒烟：真正 import 每个模块，抓「删了定义但还有人 import」这类错误。

tools/check_globals.py 只看名字有没有定义，不看你 import 的东西还在不在；
删除共享常量后，调用方要到运行时才炸（2026-10-23：RECOVER_PREFIX）。
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SKIP_PREFIXES = (
    # 起服务/连外部依赖的入口，import 有副作用，单独验证
    "mino_nexus.api.",
    "mino_nexus.assistant.connectors.",
)


def main() -> int:
    import mino_nexus

    bad: list[tuple[str, str]] = []
    for mod in pkgutil.walk_packages(mino_nexus.__path__, "mino_nexus."):
        name = mod.name
        if name.startswith(SKIP_PREFIXES):
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            bad.append((name, f"{type(exc).__name__}: {exc}"))
    if bad:
        for name, err in bad:
            print(f"[FAIL] {name}: {err}")
        print(f"[FAIL] {len(bad)} 个模块导入失败")
        return 1
    print("[OK] 所有模块可导入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
