"""项目级账号 Facet 扩展：五维默认 + 每项目自定义字段（含 AI 建议）。"""
from __future__ import annotations

import json
import re
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.services.resource_pool import DEFAULT_FACETS, FACET_LABELS, normalize_facets

TAG = "FacetSchema"

CORE_KEYS = frozenset(DEFAULT_FACETS.keys())
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _slug_key(raw: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", str(raw or "").strip().lower()).strip("_")
    if not s or s[0].isdigit():
        s = f"x_{s}" if s else "extra"
    return s[:32]


def _norm_option(row: Any) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    raw_val = str(row.get("value") or row.get("id") or "").strip().lower()
    if re.match(r"^[0-9]{1,4}$", raw_val):
        val = raw_val
    else:
        val = _slug_key(raw_val)
    if not val:
        return None
    label = str(row.get("label") or val).strip()[:40] or val
    return {"value": val, "label": label}


def normalize_extension(raw: Any, seen: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    key = _slug_key(str(raw.get("key") or raw.get("id") or ""))
    if not key or not _KEY_RE.match(key) or key in CORE_KEYS or key in seen:
        return None
    label = str(raw.get("label") or key).strip()[:24] or key
    opts = []
    for item in raw.get("options") or []:
        o = _norm_option(item)
        if o and o["value"] not in {x["value"] for x in opts}:
            opts.append(o)
    if not opts:
        opts = [{"value": "unknown", "label": "未设置"}, {"value": "yes", "label": "是"}, {"value": "no", "label": "否"}]
    seen.add(key)
    kind = str(raw.get("data_kind") or "").strip().lower()
    if kind not in ("static", "dynamic"):
        kind = "static"
    return {
        "key": key,
        "label": label,
        "options": opts[:16],
        "help": str(raw.get("help") or "").strip()[:200],
        "source": str(raw.get("source") or "manual").strip()[:16] or "manual",
        "data_kind": kind,
    }


def normalize_extensions(raw: Any) -> list[dict[str, Any]]:
    rows = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        row = normalize_extension(item, seen)
        if row:
            out.append(row)
    return out[:24]


def extensions_from_env(env_doc: dict | None) -> list[dict[str, Any]]:
    doc = env_doc if isinstance(env_doc, dict) else {}
    return normalize_extensions(doc.get("account_facet_extensions"))


def merge_account_facets(
    facets: dict[str, Any] | None,
    extensions: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    ext_defs = extensions or []
    src = facets if isinstance(facets, dict) else {}
    if ext_defs:
        out: dict[str, str] = {}
        for defn in ext_defs:
            key = str(defn.get("key") or "")
            if not key:
                continue
            val = str(src.get(key) or "unknown").strip().lower()
            opts = {str(o.get("value") or "") for o in (defn.get("options") or []) if isinstance(o, dict)}
            if val not in opts:
                val = "unknown" if "unknown" in opts else (next(iter(opts), "unknown") if opts else "unknown")
            out[key] = val
        for k, v in src.items():
            if k not in out and str(v).strip():
                out[k] = str(v).strip().lower()
        return out
    return normalize_facets(facets)


def is_configured_facet_value(defn: dict[str, Any], value: str) -> bool:
    val = str(value or "").strip().lower()
    if not val or val == "unknown":
        return False
    for o in defn.get("options") or []:
        if isinstance(o, dict) and str(o.get("value") or "").strip().lower() == val:
            lab = str(o.get("label") or "").strip()
            if lab in ("未设置", "—", "-", "无"):
                return False
            return True
    return val not in ("unknown",)


def facets_for_storage(
    facets: dict[str, Any] | None,
    field_defs: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """只落盘用户显式配置过的 facet；其余展示/租号时按模板默认推断。"""
    src = facets if isinstance(facets, dict) else {}
    defs = {
        str(d.get("key") or ""): d
        for d in (field_defs or [])
        if str(d.get("key") or "")
    }
    out: dict[str, str] = {}
    health = str(src.get("health") or "available").strip().lower()
    if health and health != "available":
        out["health"] = health
    for key, raw in src.items():
        k = str(key or "").strip()
        if not k or k == "health":
            continue
        val = str(raw or "").strip().lower()
        defn = defs.get(k)
        if defn and is_configured_facet_value(defn, val):
            out[k] = val
    return out


def extension_display_rows(
    facets: dict[str, str],
    extensions: list[dict[str, Any]],
    *,
    configured_only: bool = False,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for defn in extensions or []:
        key = str(defn.get("key") or "")
        if not key:
            continue
        val = str(facets.get(key) or "unknown")
        if configured_only and not is_configured_facet_value(defn, val):
            continue
        label = val
        for o in defn.get("options") or []:
            if isinstance(o, dict) and str(o.get("value")) == val:
                label = str(o.get("label") or val)
                break
        rows.append({
            "key": key,
            "title": str(defn.get("label") or key),
            "value": val,
            "label": label,
            "tone": "muted" if val in ("unknown", "none") else "ok",
            "extended": True,
        })
    return rows


def save_extensions_to_env(env_doc: dict, extensions: list[dict]) -> dict:
    doc = dict(env_doc or {})
    doc["account_facet_extensions"] = normalize_extensions(extensions)
    return doc


def merge_extensions(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_key = {str(x.get("key")): x for x in normalize_extensions(existing)}
    for row in normalize_extensions(incoming):
        key = str(row.get("key"))
        if key in by_key and str(by_key[key].get("source")) == "manual":
            prev = by_key[key]
            row = {**row, "label": prev.get("label") or row.get("label"), "options": prev.get("options") or row.get("options")}
        by_key[key] = row
    return normalize_extensions(list(by_key.values()))


def _sample_preconditions(project_id: str, limit: int = 40) -> list[str]:
    from mino_nexus.services.app_automation import list_project_cases

    texts: list[str] = []
    for case in list_project_cases(project_id)[:limit]:
        if not isinstance(case, dict):
            continue
        for key in ("precondition", "precondition_raw", "instruction", "expected"):
            t = str(case.get(key) or "").strip()
            if t and t not in texts:
                texts.append(t[:300])
    return texts[:limit]


def ai_expand_facet_extensions(
    project_id: str,
    *,
    hint: str = "",
    existing: list[dict] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """根据用例前置/备注建议项目级扩展字段；不改动五维默认字段。"""
    pre = _sample_preconditions(project_id)
    if not pre and not str(hint or "").strip():
        return [], "没有用例前置可参考，请填写补充说明后再试"

    payload = {
        "project_id": project_id,
        "hint": str(hint or "").strip(),
        "existing_extension_keys": [str(x.get("key")) for x in normalize_extensions(existing or [])],
        "reserved_core_keys": sorted(CORE_KEYS),
        "sample_preconditions": pre[:25],
    }
    system = """你是测试账号池 Facet 设计助手。项目已有固定五维（注册/会话/资料/地址/健康），禁止重复。
只输出 JSON：
{"extensions":[{"key":"snake_case","label":"中文名","help":"何时使用","options":[{"value":"...","label":"..."}]}]}

规则：
- key 必须 snake_case，不得与 lifecycle/session/profile_data/address/health 重复
- 每个扩展字段 2～6 个枚举 options，必须含 value=unknown label=未设置
- 只建议能从用例前置判断的差异维度（如：会员/优惠券/关注态/支付方式）
- 不要建议存手机号、密码、环境名
- 若无合理扩展，返回 {"extensions":[]}"""

    try:
        from mino_nexus.ai.dispatch_log import bind, reset
        from mino_nexus.ai.llm_client import call_chat_text, resolve_regression_provider
    except Exception as exc:
        return [], f"LLM 不可用: {exc}"

    provider, gate = resolve_regression_provider()
    if provider is None:
        return [], str((gate or {}).get("reason") or "未配置大模型")

    tok = bind(
        trigger="account_facet_expand",
        source="account_facet_schema",
        role="test-engineer",
        job="account-facet-expand",
        skill="run-case",
    )
    try:
        raw, meta = call_chat_text(
            provider=provider,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=900,
            timeout_sec=45,
            json_mode=True,
        )
    finally:
        reset(tok)

    if raw is None:
        return [], str((meta or {}).get("error") or "模型调用失败")

    data = raw if isinstance(raw, dict) else {}
    items = data.get("extensions") if isinstance(data.get("extensions"), list) else []
    seen = set(CORE_KEYS) | {str(x.get("key")) for x in normalize_extensions(existing or [])}
    normalized = []
    for item in items:
        row = normalize_extension({**item, "source": "ai"}, seen)
        if row:
            normalized.append(row)
    if not normalized:
        return [], "模型未建议新字段（可能现有五维已够用）"
    merged = merge_extensions(existing or [], normalized)
    SLog.i(TAG, f"ai expand project={project_id} +{len(normalized)} fields")
    return merged, ""
