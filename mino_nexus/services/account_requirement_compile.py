"""从前置 / CaseScene 编译扩展 facet 约束（与五维同一套 Requirement DSL）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.services.account_facet_schema import CORE_KEYS
from mino_nexus.services.resource_pool import _clause, empty_requirements


def _clause_for_field_option(defn: dict[str, Any], value: str, label: str = "") -> dict[str, str]:
    key = str(defn.get("key") or "").strip()
    val = str(value or "").strip().lower()
    lab = str(label or "").strip()
    if key == "session" and val == "logged_out":
        return _clause("session", "in", "logged_out,guest,unknown")
    if key == "lifecycle" and val == "unregistered":
        return _clause("lifecycle", "in", "unregistered,unknown")
    return _clause(key, "eq", val)


def _append_clause(req: dict[str, Any], bucket: str, clause: dict[str, str]) -> None:
    facet = str(clause.get("facet") or "").strip()
    if not facet:
        return
    rows = list(req.get(bucket) or [])
    for i, existing in enumerate(rows):
        if not isinstance(existing, dict) or str(existing.get("facet") or "") != facet:
            continue
        # eq 优先于 in（前置里写清选项标签时）
        if clause.get("op") == "eq" and existing.get("op") == "in":
            rows[i] = clause
        req[bucket] = rows
        return
    rows.append(clause)
    req[bucket] = rows


def _option_label_hits(defn: dict[str, Any], blob: str) -> list[tuple[int, int, str, str]]:
    """在 blob 中找该字段的选项命中：(start, label_len, value, label)。"""
    text = str(blob or "").strip()
    if not text:
        return []
    hits: list[tuple[int, int, str, str]] = []
    for opt in defn.get("options") or []:
        if not isinstance(opt, dict):
            continue
        lab = str(opt.get("label") or "").strip()
        val = str(opt.get("value") or "").strip().lower()
        if not val:
            continue
        if lab and len(lab) >= 2 and lab in text:
            hits.append((text.find(lab), len(lab), val, lab))
        elif len(val) >= 2 and re.search(rf"\b{re.escape(val)}\b", text.lower()):
            start = text.lower().find(val)
            hits.append((start if start >= 0 else 0, len(val), val, lab or val))
    return hits


def _non_overlapping_clauses(
    blob: str,
    field_defs: list[dict[str, Any]],
    *,
    allowed_keys: set[str] | None = None,
) -> list[dict[str, str]]:
    """最长标签优先、区间不重叠，避免「已配置」误伤「已配置形象」、多字段重复约束。"""
    text = str(blob or "").strip()
    if not text:
        return []
    candidates: list[tuple[int, int, int, dict[str, Any], str, str]] = []
    for defn in field_defs:
        key = str(defn.get("key") or "").strip()
        if not key:
            continue
        if allowed_keys is not None and key not in allowed_keys:
            continue
        for start, lab_len, val, lab in _option_label_hits(defn, text):
            end = start + lab_len
            candidates.append((lab_len, start, end, defn, val, lab))

    def _facet_rank(defn: dict[str, Any]) -> int:
        key = str(defn.get("key") or "")
        if key == "login_status":
            return 3
        if key not in CORE_KEYS:
            return 2
        return 1

    candidates.sort(key=lambda x: (-x[0], -_facet_rank(x[3]), x[1]))
    used: list[tuple[int, int]] = []
    out: list[dict[str, str]] = []
    seen_facets: set[str] = set()

    def overlaps(a: int, b: int) -> bool:
        for u0, u1 in used:
            if a < u1 and b > u0:
                return True
        return False

    for lab_len, start, end, defn, val, lab in candidates:
        facet = str(defn.get("key") or "").strip()
        if facet in seen_facets or overlaps(start, end):
            continue
        used.append((start, end))
        seen_facets.add(facet)
        out.append(_clause_for_field_option(defn, val, lab))
    return out


def _clauses_from_value_fragment(value: str, field_defs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """值片段（如「未配置形象」）按选项标签对齐到 facet eq/in。"""
    return _non_overlapping_clauses(value, field_defs)


def _dedupe_clauses_by_facet(clauses: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for clause in clauses:
        facet = str(clause.get("facet") or "").strip()
        if not facet or facet in seen:
            continue
        seen.add(facet)
        out.append(clause)
    return out


def _clauses_for_title_value(
    title: str,
    value: str,
    field_defs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    t = str(title or "").strip()
    v = str(value or "").strip()
    out: list[dict[str, str]] = []
    if not v:
        return out
    if re.search(r"登录", t) and re.search(r"未登录|游客", v):
        out.append(_clause("session", "in", "logged_out,guest,unknown"))

    scoped_keys: set[str] = set()
    for defn in field_defs:
        key = str(defn.get("key") or "")
        label = str(defn.get("label") or "")
        if not key or not label:
            continue
        if label in t:
            scoped_keys.add(key)

    if scoped_keys:
        scoped_defs = [d for d in field_defs if str(d.get("key") or "") in scoped_keys]
        scoped = _non_overlapping_clauses(v, scoped_defs)
        out.extend(scoped)
        if not scoped:
            out.extend(_clauses_from_value_fragment(v, field_defs))
    else:
        out.extend(_clauses_from_value_fragment(v, field_defs))

    return _dedupe_clauses_by_facet(out)


def _clauses_from_full_text(pre: str, field_defs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """全文扫描：选项标签出现在前置里 → 偏好约束（不与 all 重复时由 _append_clause 合并）。"""
    return _non_overlapping_clauses(pre, field_defs)


_PROFILE_NONE_RE = re.compile(r"未配置形象|形象未配置|无形象", re.I)
_PROFILE_FILLED_RE = re.compile(r"已配置形象|形象已配置", re.I)
# Console 号池模板里「形象」常映射到自定义 facet（如 field_7065t5），与核心 profile_data 不同步
PROFILE_SHAPE_FACETS = frozenset({"field_7065t5"})
_PROFILE_SHAPE_FACETS = PROFILE_SHAPE_FACETS


def _custom_profile_shape_clause(req: dict[str, Any]) -> dict[str, str] | None:
    for bucket in ("all", "prefer"):
        for clause in req.get(bucket) or []:
            if not isinstance(clause, dict):
                continue
            facet = str(clause.get("facet") or "").strip()
            if facet not in _PROFILE_SHAPE_FACETS:
                continue
            if str(clause.get("op") or "eq").strip().lower() != "eq":
                continue
            val = str(clause.get("value") or "").strip().lower()
            if val in ("yes", "no"):
                return clause
    return None


def _strip_facet_clauses(req: dict[str, Any], facet: str, *, bucket: str | None = None) -> None:
    facet = str(facet or "").strip()
    if not facet:
        return
    for key in ("all", "prefer"):
        if bucket and key != bucket:
            continue
        rows = [c for c in (req.get(key) or []) if str(c.get("facet") or "") != facet]
        req[key] = rows


def apply_profile_data_hints(pre: str, req: dict[str, Any]) -> dict[str, Any]:
    """编号行「未配置形象」优先于全文扫描里的「已配置」误匹配。"""
    out = dict(req or empty_requirements())
    text = str(pre or "")
    shape = _custom_profile_shape_clause(out)
    if _PROFILE_NONE_RE.search(text):
        _strip_facet_clauses(out, "profile_data")
        if not (shape and str(shape.get("value") or "").lower() == "no"):
            _append_clause(out, "all", _clause("profile_data", "eq", "none"))
    elif _PROFILE_FILLED_RE.search(text):
        _strip_facet_clauses(out, "profile_data")
        if not (shape and str(shape.get("value") or "").lower() == "yes"):
            _append_clause(out, "all", _clause("profile_data", "eq", "filled"))
    return out


def augment_requirements_from_precondition(
    req: dict[str, Any],
    precondition: str,
    env_doc: dict | None,
) -> dict[str, Any]:
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    pre = str(precondition or "").strip()
    if not pre:
        return dict(req or {})
    out = dict(req or empty_requirements())
    defs = merged_pool_field_defs(env_doc)

    for line in re.split(r"[\n\r]+", pre):
        chunk = re.sub(r"^\s*[\d]+[.)、]\s*", "", line.strip())
        if not chunk:
            continue
        if "：" in chunk or ":" in chunk:
            sep = "：" if "：" in chunk else ":"
            title, val = chunk.split(sep, 1)
            for clause in _clauses_for_title_value(title.strip(), val.strip(), defs):
                _append_clause(out, "all", clause)
        else:
            for clause in _clauses_from_value_fragment(chunk, defs):
                _append_clause(out, "all", clause)

    # 单行多段（用户试筛常见）
    for part in re.split(r"\s*\d+[.)、]\s*", pre):
        part = part.strip()
        if "：" in part or ":" in part:
            sep = "：" if "：" in part else ":"
            title, val = part.split(sep, 1)
            for clause in _clauses_for_title_value(title.strip(), val.strip(), defs):
                _append_clause(out, "all", clause)

    for clause in _clauses_from_full_text(pre, defs):
        _append_clause(out, "prefer", clause)

    out = apply_profile_data_hints(pre, out)
    return out
