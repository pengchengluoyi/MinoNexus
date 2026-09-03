"""mino-nexus 命令行。

    mino-nexus                      监听 :10104，并注册 mino.local
    mino-nexus --port 10104 --host 0.0.0.0

**无 Scout 也必须能起**（ARCHITECTURE.md §6）—— 设备相关接口会明确报"无可用执行节点"，
而不是整个后端起不来。
"""
from __future__ import annotations

import argparse
import sys

from mino_nexus.log import SLog

TAG = "CLI"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mino-nexus", description="MinoNexus 服务端")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址；局域网要 0.0.0.0 才能解析 mino.local")
    ap.add_argument("--port", type=int, default=10104, help="UI 仍打这个端口，与上游一致")
    args = ap.parse_args(argv)

    import uvicorn

    from mino_nexus.catalog import registry as catalog
    from mino_nexus.mdns import http_origin, node_ws_url, set_bind

    set_bind(args.host, args.port)

    # 启动就把目录加载一遍：YAML 有错要在这时候就吼出来，而不是第一次派单才发现
    health = catalog.health_summary()
    errs = catalog.list_load_errors()
    SLog.i(TAG, f"能力目录: {health}")
    for e in errs:
        SLog.e(TAG, f"目录加载错误 {e.kind} {e.path}: {e.message}")

    SLog.i(TAG, f"MinoNexus 对外 {http_origin(args.port)}")
    SLog.i(TAG, f"  UI  → HTTP + WS /ws    Scout → {node_ws_url(args.port)}")
    SLog.i(TAG, "  登录 admin / Mino@local（可用 MINO_BOOTSTRAP_PASSWORD 覆盖）")
    uvicorn.run("mino_nexus.app:app", host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
