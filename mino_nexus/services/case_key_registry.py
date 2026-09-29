"""用例密钥注册表：catalog + 逻辑块 + Nav 屏幕 key_ref 聚合与校验（P8）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.services.case_resource_key_catalog import catalog_payload
from mino_nexus.services.case_step_key_compiler import compile_case_step_program_keys, sync_case_step_program_keys

_BLOCK_ID_RE = re.compile(r"【块[:：]\s*([^】]+)】")
_NAV_ID_RE = re.compile(r"【导航[:：]\s*([^】]+)】")


def default_block_key_ref(block_id: str) -> str:
    bid = str(block_id or "").strip()
    slug = re.sub(r"[^\w]+", "_", bid).strip("_").lower()
    return f"operation.block.{slug or 'unknown'}"


def default_screen_key_ref(state_id: str) -> str:
    sid = str(state_id or "").strip()
    slug = re.sub(r"[^\w]+", "_", sid).strip("_").lower()
    return f"operation.nav.{slug or 'unknown'}"


def catalog_key_refs(*, layers: tuple[str, ...] = ("operation", "expected", "precondition")) -> set[str]:
    out: set[str] = set()
    for ent in catalog_payload().get("entries") or []:
        if not isinstance(ent, dict):
            continue
        layer = str(ent.get("key_layer") or "precondition")
        if layer not in layers:
            continue
        ref = str(ent.get("key_ref") or "").strip()
        if ref:
            out.add(ref)
    return out


def flow_block_key_refs() -> dict[str, str]:
    """block_id → key_ref（全局库）。"""
    from mino_nexus.services.nav_flow_block_catalog import list_catalog

    mapping: dict[str, str] = {}
    for row in list_catalog():
        bid = str(row.get("block_id") or "").strip()
        if not bid:
            continue
        ref = str(row.get("key_ref") or "").strip() or default_block_key_ref(bid)
        mapping[bid] = ref
    return mapping


def nav_screen_key_refs(app_id: str) -> dict[str, str]:
    """state_id → key_ref（NavFSM state.meta）。"""
    from mino_nexus.services import nav_fsm_store as store

    app = str(app_id or "").strip()
    if not app:
        return {}
    doc = store.load(app) or {}
    out: dict[str, str] = {}
    for st in doc.get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "").strip()
        if not sid:
            continue
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        ref = str(meta.get("key_ref") or "").strip() or default_screen_key_ref(sid)
        out[sid] = ref
    return out


def step_keys_summary_for_case(case: dict[str, Any] | None) -> dict[str, Any]:
    """Console 用例详情：密钥编译摘要（不重复全文 warnings）。"""
    if not isinstance(case, dict):
        return {"telemetry": "green", "warning_count": 0, "fallback_count": 0, "steps": {}}
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    warnings = list(meta.get("key_compile_warnings") or [])
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    fb = sum(1 for w in warnings if isinstance(w, dict) and w.get("fallback"))
    steps_out: dict[str, Any] = {}
    for key, bundle in spk.items():
        if not isinstance(bundle, dict):
            continue
        steps_out[str(key)] = {
            "operations": list(bundle.get("operations") or []),
            "expected": list(bundle.get("expected") or []),
            "do_step_count": len((bundle.get("do_program_plan") or {}).get("steps") or []),
            "check_count": len((bundle.get("check_program_plan") or {}).get("checkpoints") or []),
        }
    return {
        "telemetry": "red" if fb else "green",
        "warning_count": len(warnings),
        "fallback_count": fb,
        "warnings_preview": warnings[:12],
        "steps": steps_out,
    }


def catalog_entries_by_layer() -> dict[str, Any]:
    """Console 用例密钥页：按层分组条目（含 operation/expected 高亮）。"""
    buckets: dict[str, list[dict[str, Any]]] = {
        "precondition": [],
        "operation": [],
        "expected": [],
        "generic": [],
    }
    for ent in catalog_payload().get("entries") or []:
        if not isinstance(ent, dict):
            continue
        layer = str(ent.get("key_layer") or "precondition").strip().lower()
        if layer not in buckets:
            layer = "generic"
        buckets[layer].append(
            {
                "key_ref": str(ent.get("key_ref") or "").strip(),
                "section": str(ent.get("section") or ""),
                "write_category": str(ent.get("write_category") or ""),
                "write_examples": list(ent.get("write_examples") or [])[:8],
                "dsl": str(ent.get("dsl") or ""),
                "runtime": str(ent.get("runtime") or ""),
            }
        )
    return {
        "entries_by_layer": buckets,
        "operation_count": len(buckets["operation"]),
        "expected_count": len(buckets["expected"]),
        "precondition_count": len(buckets["precondition"]),
        "compile_hint": "保存用例或 POST step-keys/compile 生成 meta.step_program_keys；warnings 在 meta.key_compile_warnings",
    }


def registry_payload(*, app_id: str = "") -> dict[str, Any]:
    blocks = flow_block_key_refs()
    screens = nav_screen_key_refs(app_id) if app_id else {}
    return {
        "catalog_operation": sorted(catalog_key_refs(layers=("operation",))),
        "catalog_expected": sorted(catalog_key_refs(layers=("expected",))),
        "flow_blocks": [
            {"block_id": bid, "key_ref": ref, "dsl": f"【块:{bid}】"}
            for bid, ref in sorted(blocks.items())
        ],
        "nav_screens": [
            {"state_id": sid, "key_ref": ref, "dsl": f"【导航:{sid}】"}
            for sid, ref in sorted(screens.items())
        ],
        "app_id": str(app_id or ""),
        "catalog_layers": catalog_entries_by_layer(),
    }


def _extract_structured_refs(case: dict[str, Any]) -> tuple[list[str], list[str]]:
    blocks: list[str] = []
    navs: list[str] = []
    blob = "\n".join(
        [
            str(case.get("precondition") or ""),
            str(case.get("steps_raw") or ""),
            str(case.get("expected_raw") or ""),
            "\n".join(str(x) for x in (case.get("steps") or [])),
            "\n".join(str(x) for x in (case.get("expected") or [])),
        ]
    )
    for m in _BLOCK_ID_RE.finditer(blob):
        blocks.append(m.group(1).strip())
    for m in _NAV_ID_RE.finditer(blob):
        navs.append(m.group(1).strip())
    return blocks, navs


def validate_case_step_keys(
    case: dict[str, Any],
    *,
    app_id: str = "",
    known_blocks: dict[str, str] | None = None,
    known_screens: dict[str, str] | None = None,
) -> dict[str, Any]:
    """保存/导入前校验：未知块、Nav 屏、fallback 行、建议补 key。"""
    synced = sync_case_step_program_keys(dict(case))
    warnings = list((synced.get("meta") or {}).get("key_compile_warnings") or [])
    block_map = known_blocks if known_blocks is not None else flow_block_key_refs()
    screen_map = known_screens if known_screens is not None else nav_screen_key_refs(app_id)
    block_refs, nav_refs = _extract_structured_refs(synced)
    unknown_blocks = sorted({b for b in block_refs if b and b not in block_map})
    unknown_screens = sorted({s for s in nav_refs if s and s not in screen_map})
    fallback_count = sum(1 for w in warnings if w.get("fallback"))
    issues: list[dict[str, Any]] = []
    for bid in unknown_blocks:
        issues.append(
            {
                "kind": "unknown_block",
                "block_id": bid,
                "suggested_key_ref": default_block_key_ref(bid),
                "dsl": f"【块:{bid}】",
            }
        )
    for sid in unknown_screens:
        issues.append(
            {
                "kind": "unknown_screen",
                "state_id": sid,
                "suggested_key_ref": default_screen_key_ref(sid),
                "dsl": f"【导航:{sid}】",
            }
        )
    for w in warnings:
        if w.get("fallback"):
            issues.append({"kind": "fallback_line", **w})
    return {
        "ok": not issues,
        "fallback_count": fallback_count,
        "unknown_block_count": len(unknown_blocks),
        "unknown_screen_count": len(unknown_screens),
        "issues": issues,
        "warnings": warnings,
        "step_program_keys": (synced.get("meta") or {}).get("step_program_keys"),
    }


def import_row_key_summary(row: dict[str, Any], *, app_id: str = "") -> dict[str, Any]:
    case_like = {
        "precondition": row.get("precondition") or "",
        "steps": row.get("steps") or [],
        "expected": row.get("expected") or [],
        "steps_raw": row.get("steps_raw") or "",
        "expected_raw": row.get("expected_raw") or "",
    }
    v = validate_case_step_keys(case_like, app_id=app_id)
    return {
        "telemetry": "red" if v.get("issues") else "green",
        "fallback_count": v.get("fallback_count"),
        "unknown_block_count": v.get("unknown_block_count"),
        "unknown_screen_count": v.get("unknown_screen_count"),
        "issues": (v.get("issues") or [])[:24],
    }
