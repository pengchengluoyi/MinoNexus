"""从前置 / CaseScene 编译扩展 facet 约束（与五维同一套 Requirement DSL）。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.services.account_facet_schema import CORE_KEYS
from mino_nexus.services.resource_pool import _clause, empty_requirements

TITLE_DEVICE_LOGIN = "device_login"
TITLE_ACCOUNT_LOGIN = "account_login"
TITLE_ACCOUNT_DATA = "account_data"
TITLE_ENV_PERM = "env_perm"
TITLE_OTHER = "other"

_DEVICE_LOGIN_TITLE_RE = re.compile(r"设备登录态|机态|device[_\s-]?session", re.I)
_ACCOUNT_LOGIN_TITLE_RE = re.compile(r"账号登录态|账号登录|account[_\s-]?session", re.I)
_LEGACY_LOGIN_TITLE_RE = re.compile(r"登录态|登录状态", re.I)
_ACCOUNT_DATA_TITLE_RE = re.compile(r"账号与数据|账号数据", re.I)
_ENV_PERM_TITLE_RE = re.compile(r"环境与权限", re.I)
_DYNAMIC_FLOW_FACETS = frozenset({"login_flow", "register_flow", "audit_flow", "kyc_flow"})
_FIELD_STATUS_SPLIT = re.compile(r"[,，;；、|/]+")
_FIELD_STATUS_PAIR = re.compile(r"^(.+?)\s*[-–—=：:]\s*(.+)$")


def classify_precondition_title(title: str) -> str:
    t = str(title or "").strip()
    if not t:
        return TITLE_OTHER
    if _ACCOUNT_LOGIN_TITLE_RE.search(t):
        return TITLE_ACCOUNT_LOGIN
    if _DEVICE_LOGIN_TITLE_RE.search(t):
        return TITLE_DEVICE_LOGIN
    if _LEGACY_LOGIN_TITLE_RE.search(t):
        return TITLE_DEVICE_LOGIN
    if _ACCOUNT_DATA_TITLE_RE.search(t):
        return TITLE_ACCOUNT_DATA
    if _ENV_PERM_TITLE_RE.search(t):
        return TITLE_ENV_PERM
    return TITLE_OTHER


def _is_dynamic_flow_facet(key: str) -> bool:
    k = str(key or "").strip()
    if k in _DYNAMIC_FLOW_FACETS:
        return True
    return k.startswith("flow_")


def _static_field_defs(field_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in field_defs or [] if not _is_dynamic_flow_facet(str(d.get("key") or ""))]


def _session_field_def() -> dict[str, Any]:
    from mino_nexus.services.account_pool_templates import ACCOUNT_CORE_FACET_FIELDS

    for row in ACCOUNT_CORE_FACET_FIELDS:
        if str(row.get("key") or "") == "session":
            return dict(row)
    return {
        "key": "session",
        "label": "登录",
        "options": [
            {"value": "unknown", "label": "未设置"},
            {"value": "logged_out", "label": "未登录"},
            {"value": "logged_in", "label": "已登录"},
            {"value": "guest", "label": "游客"},
        ],
    }


def _clause_for_field_option(defn: dict[str, Any], value: str, label: str = "") -> dict[str, str]:
    key = str(defn.get("key") or "").strip()
    val = str(value or "").strip().lower()
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
    """最长标签优先、区间不重叠，避免「已配置」误伤「已配置形象」。"""
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
    """值片段按选项标签对齐；默认只扫静态字段，避免 login_flow / 引导阶段抢标签。"""
    return _non_overlapping_clauses(value, _static_field_defs(field_defs))


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


def _match_field_by_name(name: str, field_defs: list[dict[str, Any]]) -> dict[str, Any] | None:
    raw = str(name or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    exact_key: dict[str, Any] | None = None
    exact_label: dict[str, Any] | None = None
    partial: list[dict[str, Any]] = []
    for defn in field_defs or []:
        key = str(defn.get("key") or "").strip()
        label = str(defn.get("label") or "").strip()
        if key and key.lower() == lowered:
            exact_key = defn
            break
        if label and label == raw:
            exact_label = exact_label or defn
        elif label and (label in raw or raw in label):
            partial.append(defn)
    if exact_key:
        return exact_key
    if exact_label:
        return exact_label
    if len(partial) == 1:
        return partial[0]
    if partial:
        partial.sort(key=lambda d: -len(str(d.get("label") or "")))
        return partial[0]
    return None


def _clause_for_named_status(defn: dict[str, Any], status: str) -> dict[str, str] | None:
    st = str(status or "").strip()
    if not st:
        return None
    st_l = st.lower()
    for opt in defn.get("options") or []:
        if not isinstance(opt, dict):
            continue
        lab = str(opt.get("label") or "").strip()
        val = str(opt.get("value") or "").strip().lower()
        if lab == st or val == st_l:
            return _clause_for_field_option(defn, val, lab)
    hits: list[tuple[int, str, str]] = []
    for opt in defn.get("options") or []:
        if not isinstance(opt, dict):
            continue
        lab = str(opt.get("label") or "").strip()
        val = str(opt.get("value") or "").strip().lower()
        if lab and (lab in st or st in lab):
            hits.append((len(lab), val, lab))
    if hits:
        hits.sort(key=lambda x: -x[0])
        _, val, lab = hits[0]
        return _clause_for_field_option(defn, val, lab)
    return None


def parse_field_status_tokens(value: str) -> list[tuple[str, str]]:
    """「形象-已配置形象, 地址-已填写」→ [(字段名, 状态), ...]。"""
    out: list[tuple[str, str]] = []
    for part in _FIELD_STATUS_SPLIT.split(str(value or "")):
        chunk = part.strip()
        if not chunk:
            continue
        m = _FIELD_STATUS_PAIR.match(chunk)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
        else:
            out.append(("", chunk))
    return out


def _clauses_for_account_data(value: str, field_defs: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for name, status in parse_field_status_tokens(value):
        if name:
            defn = _match_field_by_name(name, field_defs)
            if not defn:
                continue
            clause = _clause_for_named_status(defn, status)
            if clause:
                out.append(clause)
            continue
        out.extend(_clauses_from_value_fragment(status, field_defs))
    return _dedupe_clauses_by_facet(out)


def _clauses_for_account_login(value: str) -> list[dict[str, str]]:
    defn = _session_field_def()
    clause = _clause_for_named_status(defn, value)
    return [clause] if clause else []


def _clauses_for_title_value(
    title: str,
    value: str,
    field_defs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    t = str(title or "").strip()
    v = str(value or "").strip()
    if not v:
        return []
    kind = classify_precondition_title(t)
    if kind in (TITLE_DEVICE_LOGIN, TITLE_ENV_PERM):
        return []
    if kind == TITLE_ACCOUNT_LOGIN:
        return _clauses_for_account_login(v)
    if kind == TITLE_ACCOUNT_DATA:
        return _clauses_for_account_data(v, field_defs)

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
        if scoped:
            return _dedupe_clauses_by_facet(scoped)
        return _dedupe_clauses_by_facet(_clauses_from_value_fragment(v, field_defs))
    return _dedupe_clauses_by_facet(_clauses_from_value_fragment(v, field_defs))


def _clauses_from_full_text(pre: str, field_defs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """全文扫描只作 prefer，且不扫动态流转字段。"""
    return _non_overlapping_clauses(pre, _static_field_defs(field_defs))


_PROFILE_NONE_RE = re.compile(r"未配置形象|形象未配置|无形象", re.I)
_PROFILE_FILLED_RE = re.compile(r"已配置形象|形象已配置", re.I)
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


def sanitize_account_requirements(req: dict[str, Any], precondition: str) -> dict[str, Any]:
    """设备登录态不得留下 login_flow；未点名的动态流转字段退出硬约束。"""
    out = dict(req or empty_requirements())
    pre = str(precondition or "")
    has_account_login = False
    named_keys: set[str] = set()
    for line in re.split(r"[\n\r]+", pre):
        chunk = re.sub(r"^\s*[\d]+[.)、]\s*", "", line.strip())
        if "：" not in chunk and ":" not in chunk:
            continue
        sep = "：" if "：" in chunk else ":"
        title, val = chunk.split(sep, 1)
        kind = classify_precondition_title(title.strip())
        if kind == TITLE_ACCOUNT_LOGIN:
            has_account_login = True
        if kind == TITLE_ACCOUNT_DATA:
            for name, _status in parse_field_status_tokens(val.strip()):
                if name:
                    named_keys.add(name.strip().lower())
    if not has_account_login:
        _strip_facet_clauses(out, "login_flow")
    for bucket in ("all", "prefer"):
        kept: list[dict[str, str]] = []
        for clause in out.get(bucket) or []:
            if not isinstance(clause, dict):
                continue
            facet = str(clause.get("facet") or "").strip()
            if _is_dynamic_flow_facet(facet) and facet != "login_flow":
                if facet.lower() not in named_keys and facet.replace("flow_", "") not in named_keys:
                    continue
            kept.append(clause)
        out[bucket] = kept
    return out


def augment_requirements_from_precondition(
    req: dict[str, Any],
    precondition: str,
    env_doc: dict | None,
    *,
    field_defs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs

    pre = str(precondition or "").strip()
    if not pre:
        return dict(req or {})
    out = dict(req or empty_requirements())
    defs = list(field_defs) if field_defs is not None else merged_pool_field_defs(env_doc)

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
    return sanitize_account_requirements(out, pre)
