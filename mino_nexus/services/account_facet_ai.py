"""用例结束：根据执行轨迹由 LLM 推断号池 facet 写回（与 pass/fail 无关）。"""
from __future__ import annotations

import json
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services.resource_pool import (
    DEFAULT_FACETS,
    _expected_text,
    account_facet_values_for_match,
)

TAG = "FacetAI"
JOB_ID = "account-facet-commit"


def _timeline_from_outcome(outcome: dict[str, Any] | None, limit: int = 48) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in (outcome or {}).get("steps") or []:
        if not isinstance(row, dict):
            continue
        cap = str(row.get("capability_id") or row.get("cap") or "").strip()
        if not cap:
            continue
        rows.append(
            {
                "seq": str(row.get("seq") or ""),
                "capability_id": cap[:64],
                "status": str(row.get("status") or "")[:16],
                "summary": str(row.get("summary") or row.get("error") or "")[:280],
            }
        )
    return rows[-limit:]


def _field_catalog(env_doc: dict) -> list[dict[str, Any]]:
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    catalog: list[dict[str, Any]] = []
    for key, label in (
        ("lifecycle", "注册态"),
        ("session", "会话"),
        ("profile_data", "资料"),
        ("address", "地址"),
        ("health", "健康"),
    ):
        catalog.append({"key": key, "label": label, "core": True})
    for defn in merged_pool_field_defs(env_doc):
        if not isinstance(defn, dict):
            continue
        key = str(defn.get("key") or "").strip()
        if not key or key in DEFAULT_FACETS:
            continue
        opts = []
        for o in defn.get("options") or []:
            if not isinstance(o, dict):
                continue
            val = str(o.get("value") or "").strip().lower()
            if val:
                opts.append({"value": val, "label": str(o.get("label") or val)[:24]})
        catalog.append(
            {
                "key": key,
                "label": str(defn.get("label") or key)[:24],
                "core": False,
                "options": opts[:12],
            }
        )
    return catalog


def _sanitize_updates(
    raw: dict[str, Any] | None,
    *,
    catalog: list[dict[str, Any]],
) -> dict[str, str]:
    allowed: dict[str, set[str]] = {}
    keys_ok = set(DEFAULT_FACETS.keys())
    for row in catalog:
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        keys_ok.add(key)
        opts = {str(o.get("value") or "").strip().lower() for o in (row.get("options") or []) if isinstance(o, dict)}
        if opts:
            allowed[key] = opts
    out: dict[str, str] = {}
    for key, val in (raw or {}).items():
        k = str(key or "").strip()
        v = str(val or "").strip().lower()
        if not k or k not in keys_ok or not v:
            continue
        if k in allowed and v not in allowed[k]:
            continue
        out[k] = v
    return out


def infer_facet_updates_from_run(
    *,
    project_id: str,
    case: dict[str, Any] | None,
    status: str,
    outcome: dict[str, Any] | None,
    current_facets: dict[str, str],
    probe_facets: dict[str, str] | None = None,
    author_hints: dict[str, Any] | None = None,
) -> tuple[dict[str, str], str, bool]:
    """调用 LLM 分析轨迹。返回 (updates, reason, llm_ok)。"""
    from mino_nexus.services import project_store as ps

    try:
        env_doc = ps.project_env(project_id)
    except KeyError:
        return {}, "project missing", False

    pre = str(
        (case or {}).get("precondition")
        or (case or {}).get("precondition_raw")
        or ""
    ).strip()
    expected = _expected_text((case or {}).get("expected") or (case or {}).get("expected_raw") or "")
    catalog = _field_catalog(env_doc)
    payload = {
        "case_id": str((case or {}).get("case_id") or (case or {}).get("id") or ""),
        "run_status": str(status or "").strip().lower(),
        "run_summary": str((outcome or {}).get("summary") or "")[:400],
        "precondition": pre[:1200],
        "expected": expected[:1200],
        "current_facets": dict(current_facets or {}),
        "probe_facets": dict(probe_facets or {}),
        "author_facet_hints": dict(author_hints or {}),
        "field_catalog": catalog,
        "timeline": _timeline_from_outcome(outcome),
    }
    if not payload["timeline"] and not probe_facets:
        return {}, "no timeline", False

    try:
        from mino_nexus.ai.dispatch_log import bind, reset
        from mino_nexus.ai.llm_client import call_chat_text, resolve_regression_provider
        from mino_nexus.ai.prompt_render import JobRenderError, render
    except Exception as exc:
        return {}, f"llm import: {exc}", False

    provider, gate = resolve_regression_provider()
    if provider is None:
        return {}, str((gate or {}).get("reason") or "no provider"), False

    slots = {"payload_json": json.dumps(payload, ensure_ascii=False)}
    try:
        messages, job_meta = render(JOB_ID, slots)
    except JobRenderError:
        return {}, f"missing job {JOB_ID}", False

    call = dict(job_meta.get("call") or {})
    tok = bind(
        trigger="account_facet_commit",
        source="account_facet_ai",
        role="test-engineer",
        job=JOB_ID,
        skill="run-case",
    )
    try:
        raw, meta = call_chat_text(
            provider=provider,
            messages=messages,
            temperature=float(call.get("temperature", 0.1)),
            max_tokens=int(call.get("max_tokens", 700)),
            timeout_sec=float(call.get("timeout_sec", 45)),
            json_mode=bool(call.get("json_mode", True)),
        )
    finally:
        reset(tok)

    if raw is None:
        return {}, str((meta or {}).get("error") or "llm failed"), False

    data = raw if isinstance(raw, dict) else {}
    updates = _sanitize_updates(
        data.get("updates") if isinstance(data.get("updates"), dict) else {},
        catalog=catalog,
    )
    reason = str(data.get("reason") or "")[:300]
    conf = str(data.get("confidence") or "").strip().lower()
    if updates:
        SLog.i(TAG, f"ai facet updates keys={list(updates.keys())} conf={conf} {reason[:80]}")
    else:
        SLog.d(TAG, f"ai facet no-op conf={conf} {reason[:80]}")
    return updates, reason, True


def collect_probe_for_account(ctx: Any, account_id: str) -> dict[str, str]:
    store = getattr(ctx, "account_probe_facets", None)
    if not isinstance(store, dict):
        return {}
    row = store.get(account_id)
    return dict(row) if isinstance(row, dict) else {}
