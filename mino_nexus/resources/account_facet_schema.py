"""项目级账号 Facet 扩展：五维默认 + 每项目自定义字段（含 AI 建议）。"""
from __future__ import annotations

import json
import re
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.resources.accounts.account_facts import (
    FACT_VALUE_KIND_ENUM,
    FACT_VALUE_KIND_NUMBER,
    FACT_VALUE_KINDS,
    fact_value_configured,
    is_numeric_fact,
    normalize_fact_value,
)
from mino_nexus.resources.resource_pool import DEFAULT_FACETS, FACET_LABELS, normalize_facets

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
    kind = str(raw.get("data_kind") or "").strip().lower()
    if kind not in ("static", "dynamic"):
        kind = "static"
    value_kind = str(raw.get("value_kind") or "").strip().lower()
    if value_kind not in FACT_VALUE_KINDS:
        value_kind = FACT_VALUE_KIND_ENUM
    if value_kind == FACT_VALUE_KIND_NUMBER:
        opts = [o for o in opts if o.get("value") == "unknown"] or [{"value": "unknown", "label": "未设置"}]
    if not opts:
        opts = [{"value": "unknown", "label": "未设置"}, {"value": "yes", "label": "是"}, {"value": "no", "label": "否"}]
    seen.add(key)
    row = {
        "key": key,
        "label": label,
        "options": opts[:16],
        "help": str(raw.get("help") or "").strip()[:200],
        "source": str(raw.get("source") or "manual").strip()[:16] or "manual",
        "data_kind": kind,
        "value_kind": value_kind,
    }
    if value_kind == FACT_VALUE_KIND_NUMBER:
        for key_name in ("unit", "min", "max"):
            if raw.get(key_name) is not None:
                row[key_name] = raw[key_name]
    return row


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
            if is_numeric_fact(defn):
                val = normalize_fact_value(defn, val)
            else:
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
    if is_numeric_fact(defn):
        return fact_value_configured(defn, val)
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
        norm_health = normalize_facets({"health": health}).get("health")
        if norm_health and norm_health != "available":
            out["health"] = norm_health
    for key, raw in src.items():
        k = str(key or "").strip()
        if not k or k == "health":
            continue
        val = str(raw or "").strip().lower()
        if k in CORE_KEYS:
            # 五维核心 facet 不在扩展 defs 里，但用户显式改过就必须落盘；
            # 否则 lifecycle/session 会被下一次读取当成 unknown。
            norm = normalize_facets({k: val}).get(k)
            if norm and norm != "unknown" and not (k == "health" and norm == "available"):
                out[k] = norm
            continue
        defn = defs.get(k)
        if defn and is_configured_facet_value(defn, val):
            out[k] = normalize_fact_value(defn, val) if is_numeric_fact(defn) else val
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
        if is_numeric_fact(defn) and val != "unknown" and str(defn.get("unit") or "").strip():
            label = f"{val} {str(defn.get('unit')).strip()}"
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
    from mino_nexus.resources.app_automation import list_project_cases

    texts: list[str] = []
    for case in list_project_cases(project_id)[:limit]:
        if not isinstance(case, dict):
            continue
        for key in ("precondition", "precondition_raw", "instruction", "expected"):
            t = str(case.get(key) or "").strip()
            if t and t not in texts:
                texts.append(t[:300])
    return texts[:limit]
