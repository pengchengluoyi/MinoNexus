"""App 私有逻辑块：从用例拓扑里沉淀可复用的连续步骤。

公共块只覆盖登录/协议/系统弹窗这类跨 App 骨架。真实业务里还有大量
「这个 App 的下单确认」「这个 App 的实名弹窗」只在单一 App 内重复出现。
规划 §5 要求把这些沉淀为 App 私有块，并保留块→用例、分支点、执行次数三类反向指标。

纯静态挖掘：只读用例编译产物，不跑设备。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

SCHEMA = "mino.private_blocks.v1"
MIN_SEQUENCE = 2
MAX_SEQUENCE = 4
MIN_CASES = 2


def _signature(step: dict[str, Any]) -> str:
    cap = str(step.get("hook_cap") or step.get("cap") or step.get("device_cap") or "")
    params = step.get("params") if isinstance(step.get("params"), dict) else {}
    try:
        blob = json.dumps(params, ensure_ascii=False, sort_keys=True)[:600]
    except (TypeError, ValueError):
        blob = str(params)[:600]
    return cap + "|" + blob


def _step_payload(step: dict[str, Any], index: int) -> dict[str, Any]:
    cap = str(step.get("hook_cap") or step.get("cap") or step.get("device_cap") or "")
    out = {
        "id": "s%d" % index,
        "hook_cap": cap,
        "params": dict(step.get("params") or {}),
        "title": str(step.get("title") or step.get("summary") or cap),
    }
    return out


def _app_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    out: list[dict[str, Any]] = []
    for bundle in (meta.get("step_program_keys") or {}).values():
        if not isinstance(bundle, dict):
            continue
        plan = bundle.get("do_program_plan") if isinstance(bundle.get("do_program_plan"), dict) else {}
        for step in plan.get("steps") or []:
            if isinstance(step, dict) and not step.get("block_id") and (step.get("hook_cap") or step.get("cap")):
                out.append(step)
    return out


def _sequences(steps: list[dict[str, Any]]) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    for size in range(MIN_SEQUENCE, MAX_SEQUENCE + 1):
        for start in range(0, max(0, len(steps) - size + 1)):
            seq = steps[start:start + size]
            sig = ">>".join(_signature(step) for step in seq)
            yield hashlib.sha1(sig.encode("utf-8")).hexdigest()[:10], seq


def mine_private_blocks(project_id: str, app_id: str, *, min_cases: int = MIN_CASES) -> dict[str, Any]:
    """统计重复片段，返回候选；不写库（写库由 seed_private_blocks 显式调用）。"""
    from mino_nexus.casework import case_store

    app = str(app_id or "").strip()
    cases = [case for case in case_store.list_cases(str(project_id or ""))
             if isinstance(case, dict)
             and (not app or not str(case.get("app_id") or "").strip()
                  or str(case.get("app_id") or "").strip() == app)]
    seen: dict[str, dict[str, Any]] = {}
    for case in cases:
        cid = str(case.get("case_id") or "")
        for digest, seq in _sequences(_app_steps(case)):
            row = seen.setdefault(digest, {
                "digest": digest, "steps": seq, "cases": [], "executions": 0,
                "branch_points": 0, "signatures": [_signature(s) for s in seq],
            })
            if cid not in row["cases"]:
                row["cases"].append(cid)
            row["executions"] += 1
            row["branch_points"] += sum(1 for step in seq if len(step.get("exec_caps") or []) > 1)
    picked = []
    for row in seen.values():
        if len(row["cases"]) < min_cases:
            continue
        block_id = "fb.app." + (app or str(project_id or "project")).replace(".", "_") + ".pvt_" + row["digest"]
        picked.append({
            "block_id": block_id,
            "display_name": "私有片段 " + row["digest"][:6],
            "description": "从 %d 条用例沉淀的 %d 步连续操作" % (len(row["cases"]), len(row["steps"])),
            "steps": [_step_payload(step, i) for i, step in enumerate(row["steps"], 1)],
            "case_ids": row["cases"],
            "case_count": len(row["cases"]),
            "executions": row["executions"],
            "branch_points": row["branch_points"],
            "signatures": row["signatures"],
        })
    picked.sort(key=lambda item: (-item["case_count"], item["block_id"]))
    return {"schema": SCHEMA, "app_id": app, "cases": len(cases),
            "candidates": picked[:200], "total": len(picked)}


def seed_private_blocks(project_id: str, app_id: str, *, min_cases: int = MIN_CASES) -> dict[str, Any]:
    """把候选写入块目录（block_origin=app），已存在则更新步骤。"""
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import upsert_app_block

    got = mine_private_blocks(project_id, app_id, min_cases=min_cases)
    written = []
    for row in got["candidates"]:
        saved = upsert_app_block(app_id=str(app_id or ""), block_id=row["block_id"],
                                 display_name=row["display_name"], description=row["description"],
                                 steps_json=row["steps"], version="v1", enabled=True,
                                 block_origin="app", case_ids=list(row.get("case_ids") or []))
        if saved:
            written.append({"block_id": row["block_id"], "case_count": row["case_count"],
                            "steps": len(row["steps"])})
    return {**got, "written": written, "written_count": len(written)}


def clear_private_blocks(project_id: str, app_id: str) -> dict[str, Any]:
    """清空 App 私有块，保留全局骨架；重新 seed 时避免旧候选堆积。"""
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import delete_app_blocks

    removed = delete_app_blocks(str(app_id or ""), prefix="fb.app.")
    return {"schema": SCHEMA, "app_id": str(app_id or ""), "removed": removed}


def reverse_metrics(project_id: str, app_id: str) -> dict[str, Any]:
    """块→用例数、分支点数、执行次数。执行次数来自编译产物，不伪造真机次数。"""
    from mino_nexus.casework import case_store
    from mino_nexus.navigation.atlas.nav_flow_block_catalog import list_catalog

    app = str(app_id or "").strip()
    blocks = [row for row in list_catalog(app_id=app)
              if str(row.get("app_id") or "") == app or str(row.get("block_id") or "").startswith("fb.global.")]
    cases = [case for case in case_store.list_cases(str(project_id or ""))
             if isinstance(case, dict)
             and (not app or str(case.get("app_id") or case.get("project_id") or "") in ("", app))]
    metrics: dict[str, dict[str, Any]] = {}
    for block in blocks:
        bid = str(block.get("block_id") or "")
        stored_ids = [str(x) for x in (block.get("case_ids") or []) if str(x)]
        metrics[bid] = {"block_id": bid, "private": str(block.get("app_id") or "") == app,
                        "case_ids": list(stored_ids), "executions": 0, "branch_points": 0,
                        "steps": len(block.get("steps_json") or [])}
    for case in cases:
        cid = str(case.get("case_id") or "")
        meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
        for bundle in (meta.get("step_program_keys") or {}).values():
            if not isinstance(bundle, dict):
                continue
            plan = bundle.get("do_program_plan") if isinstance(bundle.get("do_program_plan"), dict) else {}
            for step in plan.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                bid = str(step.get("block_id") or "")
                if not bid or bid not in metrics:
                    continue
                row = metrics[bid]
                if cid not in row["case_ids"]:
                    row["case_ids"].append(cid)
                row["executions"] += 1
                row["branch_points"] += 1 if len(step.get("exec_caps") or []) > 1 else 0
    # 私有块是从用例步骤沉淀出来的，步骤本身不会回写 block_id；用同一份挖掘
    # 结果补块→用例反查，保证 seed 后私有块也能看到来源用例。
    try:
        mined = mine_private_blocks(str(project_id or ""), app, min_cases=1).get("candidates") or []
    except Exception:  # noqa: BLE001 - 反查降级不影响块目录
        mined = []
    for item in mined:
        row = metrics.get(str(item.get("block_id") or ""))
        if not row:
            continue
        row["case_ids"] = list(item.get("case_ids") or [])
        row["executions"] = int(item.get("executions") or 0)
        row["branch_points"] = int(item.get("branch_points") or 0)
    return {"schema": SCHEMA, "app_id": app, "blocks": list(metrics.values()),
            "blocks_total": len(metrics), "cases_total": len(cases)}


def describe() -> dict[str, Any]:
    return {"schema": SCHEMA, "min_sequence": MIN_SEQUENCE, "max_sequence": MAX_SEQUENCE,
            "min_cases": MIN_CASES, "origin": "app",
            "metrics": ["block_to_cases", "branch_points", "executions"]}


__all__ = [
    "SCHEMA",
    "describe",
    "mine_private_blocks",
    "reverse_metrics",
    "seed_private_blocks",
]
