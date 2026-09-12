"""mino-nexus 命令行。

    mino-nexus                      监听 :10104，并注册 mino.local
    mino-nexus --port 10104 --host 0.0.0.0

**无 Scout 也必须能起**（ARCHITECTURE.md §6）—— 设备相关接口会明确报"无可用执行节点"，
而不是整个后端起不来。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def _session_cli(argv: list[str]) -> int:
    import json

    ap = argparse.ArgumentParser(prog="mino-nexus session", description="Session Log Harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_eval = sub.add_parser("eval", help="聚合 eval 指标")
    p_eval.add_argument("session_id")
    p_audit = sub.add_parser("audit", help="审计证据链")
    p_audit.add_argument("session_id")
    p_replay = sub.add_parser("replay-plan", help="Replay 计划 JSON")
    p_replay.add_argument("session_id")
    p_replay.add_argument("--turn", type=int, default=None)
    p_fork = sub.add_parser("fork-plan", help="Fork 计划 JSON")
    p_fork.add_argument("session_id")
    p_fork.add_argument("--from-turn", type=int, required=True)
    p_fork.add_argument("--provider", default="")
    p_harvest = sub.add_parser("harvest", help="批量 harvest eval")
    p_harvest.add_argument("--app-id", default="")
    p_harvest.add_argument("--limit", type=int, default=20)
    p_harvest.add_argument("--expect-status", default="")
    p_harvest.add_argument("--menu-must", action="append", default=[])
    args = ap.parse_args(argv)

    from mino_nexus.core.database import ensure_db

    ensure_db()

    if args.cmd == "eval":
        from mino_nexus.loop.session_harness import project_eval

        print(json.dumps(project_eval(args.session_id), ensure_ascii=False, indent=2))
    elif args.cmd == "audit":
        from mino_nexus.loop.session_harness import project_audit

        print(json.dumps(project_audit(args.session_id), ensure_ascii=False, indent=2))
    elif args.cmd == "replay-plan":
        from mino_nexus.loop.session_harness import build_replay_plan

        print(json.dumps(build_replay_plan(args.session_id, up_to_turn=args.turn), ensure_ascii=False, indent=2))
    elif args.cmd == "fork-plan":
        from mino_nexus.loop.session_harness import build_fork_plan

        print(json.dumps(
            build_fork_plan(
                args.session_id,
                from_turn=args.from_turn,
                overrides={"provider_id": args.provider} if args.provider else None,
            ),
            ensure_ascii=False,
            indent=2,
        ))
    elif args.cmd == "harvest":
        from mino_nexus.loop.session_harness import harvest_sessions

        expect = {}
        if args.expect_status:
            expect["status"] = args.expect_status
        if args.menu_must:
            expect["tools_must_include"] = args.menu_must
        print(json.dumps(
            harvest_sessions(app_id=args.app_id, limit=args.limit, expect=expect or None),
            ensure_ascii=False,
            indent=2,
        ))
    return 0



def _nav_cli(argv: list[str]) -> int:
    """NavFSM 配置与校准。没有 Studio 时的等价入口（docs/NAVIGATION_ATLAS.md §8.4、§8.5）。"""
    import json

    ap = argparse.ArgumentParser(prog="mino-nexus nav", description="NavFSM 配置与真机校准")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="看一个 app 的配置与 runtime 可用性")
    p_show.add_argument("app_id")

    sub.add_parser("list", help="列出已配导航图的应用")

    p_tpl = sub.add_parser("template", help="产一份待校准骨架 JSON 到 stdout")
    p_tpl.add_argument("app_id")

    p_start = sub.add_parser("start", help="开一批 walkthrough")
    p_start.add_argument("app_id")
    p_start.add_argument("--account-id", required=True, help="**跑批将用的租号**，不是开发机账号（§11.4）")
    p_start.add_argument("--calibration-id", default="")

    p_step = sub.add_parser("step", help="记一步 walkthrough")
    p_step.add_argument("app_id")
    p_step.add_argument("calibration_id")
    p_step.add_argument("--step-key", required=True, help="如 W1 / W2")
    p_step.add_argument("--file", default="", help="hierarchy 文本文件；缺省从 stdin 读")
    p_step.add_argument("--run-id", default="")
    p_step.add_argument("--case-id", default="")
    p_step.add_argument("--turn-id", type=int, default=0)
    p_step.add_argument("--note", default="")

    p_fin = sub.add_parser("finish", help="收工并记结论")
    p_fin.add_argument("app_id")
    p_fin.add_argument("calibration_id")
    p_fin.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                       help="结论，如 --set follow_filled_detectable=id")

    p_cl = sub.add_parser("calibrations", help="列出某 app 的校准批次")
    p_cl.add_argument("app_id")

    p_ev = sub.add_parser("evidence", help="看一批证据（manifest + walkthrough）")
    p_ev.add_argument("app_id")
    p_ev.add_argument("calibration_id")
    p_ev.add_argument("--step", type=int, default=0, help="给了就只打这一步的 hierarchy 正文")

    p_ds = sub.add_parser("draft-save", help="把 JSON 文件存成待校准草稿")
    p_ds.add_argument("app_id")
    p_ds.add_argument("path")

    p_dp = sub.add_parser("draft-promote", help="草稿校验通过后写进库")
    p_dp.add_argument("app_id")

    p_m = sub.add_parser("metrics", help="按 app / run 聚合 nav 指标（§9）")
    p_m.add_argument("app_id")
    p_m.add_argument("--run-id", default="")
    p_m.add_argument("--limit", type=int, default=50)

    args = ap.parse_args(argv)

    from mino_nexus.core.database import ensure_db
    from mino_nexus.services import nav_calibration_store as calib
    from mino_nexus.services import nav_fsm_store as store

    ensure_db()

    def dump(obj) -> int:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.cmd == "show":
        raw = store.read_raw(args.app_id)
        if raw is None:
            print(f"app_id={args.app_id} 没有 nav_fsm 配置。先 `nav template {args.app_id}` 取骨架。")
            return 1
        _, reason = store.load_with_reason(args.app_id)
        return dump({**raw, "runtime_ready": not reason, "runtime_reason": reason})

    if args.cmd == "list":
        return dump(store.list_apps())

    if args.cmd == "template":
        from mino_nexus.services import nav_fsm_template as tpl
        from mino_nexus.services import project_store as ps

        app = ps.find_app(args.app_id)
        return dump(tpl.build_template(args.app_id, project_id=str((app or {}).get("project_id") or "")))

    if args.cmd == "start":
        from mino_nexus.services import project_store as ps

        app = ps.find_app(args.app_id)
        manifest = calib.start(
            args.app_id,
            project_id=str((app or {}).get("project_id") or ""),
            account_id=args.account_id,
            calibration_id=args.calibration_id,
        )
        SLog.i(TAG, f"walkthrough 开始：{manifest['calibration_id']}（顺序即语义，跳步补采无效）")
        return dump(manifest)

    if args.cmd == "step":
        text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
        return dump(calib.append_step(
            args.app_id, args.calibration_id,
            step_key=args.step_key, hierarchy_text=text,
            run_id=args.run_id, case_id=args.case_id, turn_id=args.turn_id, note=args.note,
        ))

    if args.cmd == "finish":
        conclusions: dict = {}
        for item in args.set:
            key, _, val = str(item).partition("=")
            if key:
                conclusions[key.strip()] = val.strip()
        return dump(calib.finish(args.app_id, args.calibration_id, conclusions=conclusions))

    if args.cmd == "calibrations":
        return dump(calib.list_calibrations(args.app_id))

    if args.cmd == "evidence":
        if args.step:
            print(calib.read_step(args.app_id, args.calibration_id, args.step))
            return 0
        row = calib.read(args.app_id, args.calibration_id)
        if row is None:
            print("没有这批证据")
            return 1
        return dump(row)

    if args.cmd == "draft-save":
        doc = json.loads(Path(args.path).read_text(encoding="utf-8"))
        calib.save_draft(args.app_id, doc)
        from mino_nexus.services import nav_fsm_template as tpl

        pending = tpl.pending_marks(doc)
        print(f"草稿已存。还差 {len(pending)} 个待校准字段" if pending else "草稿已存，且没有待校准字段")
        for row in pending[:20]:
            print("  -", row)
        return 0

    if args.cmd == "draft-promote":
        try:
            saved = calib.promote_draft(args.app_id, updated_by="cli")
        except store.NavFsmInvalid as exc:
            print(f"未写库：{exc}")
            return 1
        return dump({"app_id": saved["app_id"], "states": len(saved["states"]), "edges": len(saved["edges"])})

    if args.cmd == "metrics":
        from mino_nexus.services.nav_telemetry import aggregate_app

        return dump(aggregate_app(args.app_id, run_id=args.run_id, limit=args.limit))

    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "catalog":
        return _catalog_cli(argv[1:])
    if argv and argv[0] == "session":
        return _session_cli(argv[1:])
    if argv and argv[0] == "nav":
        return _nav_cli(argv[1:])

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
