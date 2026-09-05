"""mino-nexus 命令行。

    mino-nexus                      监听 :10104，并注册 mino.local
    mino-nexus --port 10104 --host 0.0.0.0

**无 Scout 也必须能起**（ARCHITECTURE.md §6）—— 设备相关接口会明确报"无可用执行节点"，
而不是整个后端起不来。
"""
from __future__ import annotations

import argparse
import sys

from mino_nexus.core.log import SLog

TAG = "CLI"


def _catalog_cli(argv: list[str]) -> int:
    import json

    ap = argparse.ArgumentParser(prog="mino-nexus catalog", description="能力目录导出")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export", help="把 catalog_entries 打成 JSON 到 stdout")
    args = ap.parse_args(argv)

    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.catalog import CatalogEntry

    ensure_db()
    if args.cmd != "export":
        return 1
    db = SessionLocal()
    try:
        rows = []
        for row in db.query(CatalogEntry).order_by(CatalogEntry.sort_order).all():
            payload = dict(row.payload_json or {})
            payload.pop("trigger_phrases", None)
            impls = payload.get("implementations")
            if isinstance(impls, list):
                cleaned = []
                for it in impls:
                    if not isinstance(it, dict):
                        continue
                    item = dict(it)
                    item.pop("cost", None)
                    item.pop("requires_caps", None)
                    cleaned.append(item)
                payload["implementations"] = cleaned
            rows.append({
                "kind": row.kind,
                "id": row.id,
                "display_name": row.display_name,
                "description": row.description,
                "enabled": bool(row.enabled),
                "lifecycle": row.lifecycle,
                "provider": row.provider,
                "owner": row.owner,
                "platforms": list(row.platforms_json or []),
                "visible_to": list(row.visible_to_json or []),
                "category": row.category,
                "sort_order": row.sort_order,
                "payload": payload,
            })
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "catalog":
        return _catalog_cli(argv[1:])

    ap = argparse.ArgumentParser(prog="mino-nexus", description="MinoNexus 服务端")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址；局域网要 0.0.0.0 才能解析 mino.local")
    ap.add_argument("--port", type=int, default=10104, help="UI 仍打这个端口，与上游一致")
    args = ap.parse_args(argv)

    import uvicorn

    from mino_nexus.catalog import registry as catalog
    from mino_nexus.core.mdns import http_origin, node_ws_url, set_bind

    set_bind(args.host, args.port)

    health = catalog.health_summary()
    errs = catalog.list_load_errors()
    SLog.i(TAG, f"能力目录: {health}")
    for e in errs:
        SLog.e(TAG, f"目录加载错误 {e.kind} {e.path}: {e.message}")

    SLog.i(TAG, f"MinoNexus 对外 {http_origin(args.port)}")
    SLog.i(TAG, f"  UI  → HTTP + WS /ws    Scout → {node_ws_url(args.port)}")
    uvicorn.run("mino_nexus.app:app", host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
