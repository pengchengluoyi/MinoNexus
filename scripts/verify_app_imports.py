#!/usr/bin/env python3
"""守门：Nexus 必须真的能起来。

**为什么单独要这一条**：其余 verify_* 都是静态扫描（不 import 代码），所以抓不到
"服务根本起不来"这类问题。已经踩过两次同一个坑：

  `from __future__ import annotations` + 请求模型/WebSocket 定义在函数内部
  → 注解变成字符串，FastAPI 拿模块 globals 去 eval → NameError → 整个 app 起不来。

第一次修完之后被一次重写覆盖，又坏了一整天，而 CI 全绿。所以把"能 import + 有路由"
变成硬断言。

这条需要装依赖（fastapi / pydantic / pyyaml），不像其它守门能用裸 python 跑。
CI 里放在 pip install 之后。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 关键路由：少了任何一条就说明装配出问题了（而不只是能 import）
REQUIRED_ROUTES = {
    "/health": "健康检查 —— 无 Scout 也必须能答（ARCHITECTURE.md §6）",
    "/nodes": "节点清单",
    "/devices": "设备清单（连通性来自 Scout 上报）",
    "/capabilities": "能力菜单 = 目录 ∩ 节点 provides",
    "/node": "Scout 节点接入（WS）",
}

# 这几条来自**带 prefix 的 APIRouter**。单独列出来是因为：
# fastapi 0.141.1 起 include_router(APIRouter(prefix=...)) 会把路径解析成空串，
# 于是 12 个 router 全部静默失效，而 app 照常启动、/health 照常 200。
# 最小复现：
#     r = APIRouter(prefix="/x")
#     @r.get("/ping")
#     def ping(): ...
#     app.include_router(r)
#     # 0.124.0 -> "/x/ping"   0.141.1 -> ""
# 所以基础路由全在**不代表**服务是好的，必须验一条带 prefix 的。
PREFIXED_ROUTES = {
    "/auth/status": "rAuth（登录，Console/Studio 的入口）",
    "/case-runner/run": "rCaseRunner（下发批次）",
    "/device/list": "rDevice（设备面）",
    "/settings/dispatch": "rSettings（调用记录）",
    "/settings/plugins": "rSettings（集成插件）",
}


def _deps_ready() -> tuple[bool, str]:
    """依赖没装时不要谎报失败 —— 那会让人以为代码坏了。"""
    for mod in ("fastapi", "pydantic", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            return False, mod
    return True, ""


def main() -> int:
    ok, missing = _deps_ready()
    if not ok:
        print(f"SKIP verify_app_imports — 缺依赖 {missing}（本条需要 pip install -e .；CI 里会真跑）")
        return 0

    errors: list[str] = []

    try:
        from mino_nexus.app import app
    except Exception as exc:
        print("FAIL verify_app_imports")
        print(f"  Nexus 起不来：{type(exc).__name__}: {exc}")
        print("\n  常见原因：请求模型 / WebSocket 定义在函数内部，而本文件有")
        print("  `from __future__ import annotations` —— 提到模块级即可。")
        import traceback

        for line in traceback.format_exc().splitlines()[-6:]:
            print("  " + line)
        return 1

    paths = {getattr(r, "path", "") for r in app.routes}
    for path, why in REQUIRED_ROUTES.items():
        if path not in paths:
            errors.append(f"缺路由 {path}（{why}）")

    missing_prefixed = [(p, w) for p, w in PREFIXED_ROUTES.items() if p not in paths]
    if missing_prefixed:
        import fastapi

        for path, why in missing_prefixed:
            errors.append(f"缺路由 {path}（{why}）")
        if "" in paths:
            errors.append(
                f"检测到空路径路由 + 带 prefix 的路由全丢 → 极可能是 fastapi 版本问题"
                f"（当前 {fastapi.__version__}，需 <0.130，见本文件顶部说明）"
            )

    # 能力目录也要能加载，且不带 load error
    try:
        from mino_nexus.catalog import registry as catalog

        caps = catalog.list_capabilities()
        if not caps:
            errors.append("能力目录为空 —— plugins/ 没被加载到")
        for e in catalog.list_load_errors():
            errors.append(f"能力目录加载错误：{e.kind} {e.message}")
    except Exception as exc:
        errors.append(f"能力目录加载失败：{type(exc).__name__}: {exc}")

    # 执行链路也要能 import。app.py 不直接引 agent_loop（是 lazy 的），
    # 所以 app 起得来 ≠ 跑得动。踩过一次：一个空的 local_executors/__init__.py 目录
    # 盖住了同名模块 local_executors.py，agent_loop 一 import 就 ImportError，
    # 而 /health 照常 200、CI 全绿。
    for mod, why in (
        ("mino_nexus.loop.agent_loop", "单用例循环"),
        ("mino_nexus.loop.case_runner", "批次编排"),
        ("mino_nexus.loop.local_executors", "本地 executor（hitl/vlm/persona/wait）"),
        ("mino_nexus.loop.router_proxy", "派给 Scout 的代理"),
        ("mino_nexus.ai.planner", "LLM 决策入口"),
    ):
        try:
            __import__(mod)
        except Exception as exc:
            errors.append(f"{mod} 导入失败（{why}）：{type(exc).__name__}: {exc}")

    if errors:
        print("FAIL verify_app_imports")
        for e in errors:
            print("  " + e)
        return 1

    print(f"OK verify_app_imports — app 可导入，{len(paths)} 条路由，能力目录无错")
    return 0


if __name__ == "__main__":
    sys.exit(main())
