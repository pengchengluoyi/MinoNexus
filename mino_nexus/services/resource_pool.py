"""测试账号资源池：Typed Facets、Requirement DSL、探针观测、Profile 模板。

标签 tags 仅作一次性迁移输入，匹配与展示真源均为 facets。
"""
from __future__ import annotations

import copy
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

# --- Facet schema (v1) ---
LIFECYCLE = frozenset({"unregistered", "registered", "unknown"})
SESSION = frozenset({"logged_out", "logged_in", "guest", "unknown"})
PROFILE_DATA = frozenset({"none", "filled", "unknown"})
ADDRESS = frozenset({"none", "filled", "unknown"})
HEALTH = frozenset({"available", "dirty", "quarantine", "bad"})

DEFAULT_FACETS: dict[str, str] = {
    "lifecycle": "unknown",
    "session": "unknown",
    "profile_data": "none",
    "address": "none",
    "health": "available",
}

FACET_LABELS: dict[str, dict[str, str]] = {
    "lifecycle": {
        "unregistered": "未注册",
        "registered": "已注册",
        "unknown": "未知",
    },
    "session": {
        "logged_out": "未登录",
        "logged_in": "已登录",
        "guest": "游客",
        "unknown": "未知",
    },
    "profile_data": {"none": "无资料", "filled": "有资料", "unknown": "未知"},
    "address": {"none": "无地址", "filled": "有地址", "unknown": "未知"},
    "health": {
        "available": "可用",
        "dirty": "待重置",
        "quarantine": "隔离",
        "bad": "坏号",
    },
}

# 补池 / 入库模板（Console 可选用 profile_id 初始化 facets）
POOL_PROFILES: dict[str, dict[str, str]] = {
    "fresh_user": {
        "lifecycle": "unregistered",
        "session": "logged_out",
        "profile_data": "none",
        "address": "none",
        "health": "available",
    },
    "registered_guest": {
        "lifecycle": "registered",
        "session": "logged_out",
        "profile_data": "none",
        "address": "none",
        "health": "available",
    },
    "logged_in_user": {
        "lifecycle": "registered",
        "session": "logged_in",
        "profile_data": "none",
        "address": "none",
        "health": "available",
    },
    "profile_complete": {
        "lifecycle": "registered",
        "session": "logged_in",
        "profile_data": "filled",
        "address": "none",
        "health": "available",
    },
    "address_complete": {
        "lifecycle": "registered",
        "session": "logged_in",
        "profile_data": "filled",
        "address": "filled",
        "health": "available",
    },
}

DEFAULT_LEASE_TTL_SEC = 7200
ACQUIRE_WAIT_MS_DEFAULT = 300_000
ACQUIRE_POLL_MS = 500

_REQUIREMENT_RE = re.compile(r"新用户|老用户|未注册|已注册|未登录|已登录|游客|资料|昵称|头像|地址|收货", re.I)


def list_pool_profiles() -> list[dict[str, Any]]:
    out = []
    for pid, facets in POOL_PROFILES.items():
        out.append({"id": pid, "facets": dict(facets), "label": profile_label(pid)})
    return out


def profile_label(profile_id: str) -> str:
    labels = {
        "fresh_user": "新用户（未注册）",
        "registered_guest": "已注册未登录",
        "logged_in_user": "已登录常规号",
        "profile_complete": "已登录+资料",
        "address_complete": "已登录+资料+地址",
    }
    return labels.get(profile_id, profile_id)


def facets_from_profile(profile_id: str) -> dict[str, str]:
    base = dict(DEFAULT_FACETS)
    tpl = POOL_PROFILES.get(str(profile_id or "").strip())
    if tpl:
        base.update({k: str(v) for k, v in tpl.items() if k in DEFAULT_FACETS or k == "health"})
    return normalize_facets(base)


def normalize_facets(raw: Any) -> dict[str, str]:
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_FACETS)
    for key in DEFAULT_FACETS:
        val = str(src.get(key) or out[key]).strip().lower()
        if key == "lifecycle" and val not in LIFECYCLE:
            val = "unknown"
        elif key == "session" and val not in SESSION:
            val = "unknown"
        elif key == "profile_data" and val not in PROFILE_DATA:
            val = "unknown"
        elif key == "address" and val not in ADDRESS:
            val = "unknown"
        elif key == "health" and val not in HEALTH:
            val = "available"
        out[key] = val
    return out


def migrate_tags_to_facets(tags: list[Any], facets: dict[str, str] | None) -> dict[str, str]:
    """旧 tags 一次性合并进 facets（仅填空位，不覆盖已有明确 facet）。"""
    out = normalize_facets(facets or {})
    blob = " ".join(str(t) for t in (tags or []) if str(t).strip())
    if not blob:
        return out

    def set_if_unknown(key: str, val: str) -> None:
        if out.get(key) in ("unknown", "none") or key == "health" and out.get("health") == "available":
            cur = out.get(key)
            if cur in ("unknown", "none", "available"):
                out[key] = val

    if "未注册" in blob or "新用户" in blob:
        set_if_unknown("lifecycle", "unregistered")
    if "老用户" in blob or "已注册" in blob:
        if out.get("lifecycle") == "unknown":
            out["lifecycle"] = "registered"
    if "未登录" in blob or "游客" in blob:
        set_if_unknown("session", "logged_out")
    if "已登录" in blob:
        set_if_unknown("session", "logged_in")
    if re.search(r"资料|昵称|头像|profile", blob, re.I):
        set_if_unknown("profile_data", "filled")
    if re.search(r"地址|收货|address", blob, re.I):
        set_if_unknown("address", "filled")
    return normalize_facets(out)


def account_facets(row: dict[str, Any] | None) -> dict[str, str]:
    row = row if isinstance(row, dict) else {}
    facets = migrate_tags_to_facets(row.get("tags") or [], row.get("facets"))
    pid = str(row.get("profile_id") or "").strip()
    if pid and all(facets.get(k) in ("unknown", "none", "available") for k in ("lifecycle", "session")):
        facets = normalize_facets({**facets_from_profile(pid), **facets})
    return facets


def facet_label(key: str, value: str) -> str:
    return FACET_LABELS.get(key, {}).get(str(value or "").strip().lower(), str(value or ""))


def format_facets_brief(facets: dict[str, str]) -> str:
    parts = []
    for key in ("lifecycle", "session", "profile_data", "address", "health"):
        val = facets.get(key) or "unknown"
        lab = facet_label(key, val)
        if val in ("unknown", "none", "available") and key in ("profile_data", "address", "health"):
            continue
        parts.append(lab)
    return " · ".join(parts) if parts else "未标注状态"


# UI 列表/详情用的结构化展示（Console 可直接渲染 chip）
FACET_UI_ORDER = (
    ("lifecycle", "注册"),
    ("session", "会话"),
    ("profile_data", "资料"),
    ("address", "地址"),
    ("health", "健康"),
)


def facets_display_chips(facets: dict[str, str]) -> list[dict[str, str]]:
    norm = normalize_facets(facets)
    chips: list[dict[str, str]] = []
    for key, title in FACET_UI_ORDER:
        val = norm.get(key) or "unknown"
        if key in ("profile_data", "address") and val == "none":
            continue
        if key == "health" and val == "available":
            continue
        chips.append({
            "key": key,
            "title": title,
            "value": val,
            "label": facet_label(key, val),
        })
    if not chips:
        chips.append({
            "key": "lifecycle",
            "title": "注册",
            "value": norm.get("lifecycle", "unknown"),
            "label": facet_label("lifecycle", norm.get("lifecycle", "unknown")),
        })
    return chips


def lease_display(lease: dict[str, Any] | None) -> dict[str, str]:
    row = lease if isinstance(lease, dict) else {}
    rid = str(row.get("run_id") or "").strip()
    if not rid:
        return {"status": "free", "label": "空闲", "run_id": ""}
    if lease_expired(row):
        return {"status": "expired", "label": "租约已过期（待回收）", "run_id": rid}
    exp = str(row.get("expires_at") or "").strip()
    return {
        "status": "leased",
        "label": f"租用中 · {rid[:16]}",
        "run_id": rid,
        "expires_at": exp,
    }


# --- Requirement DSL ---

def _clause(facet: str, op: str, value: str) -> dict[str, str]:
    return {"facet": facet, "op": op, "value": str(value).strip().lower()}


def empty_requirements() -> dict[str, Any]:
    return {"all": [], "prefer": [], "env": ""}


def compile_requirements_from_text(
    precondition: str,
    *,
    session_hint: str = "",
    want_profile: bool = False,
    want_address: bool = False,
    env: str = "",
) -> dict[str, Any]:
    """从用例前置 / CaseScene 编译 Requirement DSL。"""
    pre = str(precondition or "").strip()
    req = empty_requirements()
    if env:
        req["env"] = str(env).strip().lower()

    session = str(session_hint or "").strip().lower()
    if session == "logged_in":
        req["all"].append(_clause("session", "eq", "logged_in"))
        if "lifecycle" not in pre and "注册" not in pre:
            req["prefer"].append(_clause("lifecycle", "eq", "registered"))
    elif session == "guest":
        req["all"].append(_clause("session", "in", "logged_out,guest,unknown"))
        req["prefer"].append(_clause("lifecycle", "eq", "unregistered"))

    if "新用户" in pre or "未注册" in pre:
        req["all"].append(_clause("lifecycle", "eq", "unregistered"))
        req["prefer"].append(_clause("session", "in", "logged_out,guest,unknown"))
    elif "老用户" in pre or ("已注册" in pre and "未注册" not in pre):
        req["all"].append(_clause("lifecycle", "eq", "registered"))

    if "已登录" in pre:
        req["all"].append(_clause("session", "eq", "logged_in"))
    elif "未登录" in pre or "游客" in pre:
        req["all"].append(_clause("session", "in", "logged_out,guest,unknown"))

    if want_profile or re.search(r"资料|昵称|头像|profile", pre, re.I):
        req["all"].append(_clause("profile_data", "eq", "filled"))
    if want_address or re.search(r"地址|收货|address", pre, re.I):
        req["all"].append(_clause("address", "eq", "filled"))

    req["all"].append(_clause("health", "eq", "available"))
    return req


def compile_requirements_from_case_need(
    need: dict[str, Any],
    *,
    env: str = "",
    env_doc: dict | None = None,
) -> dict[str, Any]:
    from mino_nexus.services.account_pool_templates import (
        augment_requirements_with_template,
        merge_need_requirements,
    )

    req = compile_requirements_from_text(
        str(need.get("prompt") or ""),
        session_hint=str(need.get("session") or ""),
        want_profile=bool(need.get("profile")),
        want_address=bool(need.get("address")),
        env=env,
    )
    extra = need.get("requirements") if isinstance(need.get("requirements"), dict) else {}
    req = merge_need_requirements(req, extra)
    tid = str(need.get("template_id") or "").strip()
    if tid:
        req = augment_requirements_with_template(req, tid, env_doc)
    return req


def _facet_field_def(field_defs: dict[str, dict[str, Any]] | None, facet: str) -> dict[str, Any] | None:
    if not field_defs or not facet:
        return None
    row = field_defs.get(facet)
    return row if isinstance(row, dict) else None


def _facet_values_equivalent(
    field_defs: dict[str, dict[str, Any]] | None,
    facet: str,
    got: str,
    want: str,
) -> bool:
    if got == want:
        return True
    defn = _facet_field_def(field_defs, facet)
    if not defn:
        return False
    labels: dict[str, str] = {}
    for o in defn.get("options") or []:
        if not isinstance(o, dict):
            continue
        val = str(o.get("value") or "").strip().lower()
        if val:
            labels[val] = str(o.get("label") or "").strip()
    if labels.get(got) and labels.get(got) == labels.get(want):
        return True
    return False


def _want_is_absence_state(defn: dict[str, Any] | None, want: str) -> bool:
    """需求值为「未设置/未登录/否」等空态时，库内 unknown 也算满足。"""
    val = str(want or "").strip().lower()
    if not val or val == "unknown":
        return True
    if val in ("no", "none", "logged_out", "guest", "unregistered", "unconfigured", "not_configured", "not_logged_in"):
        return True
    if not defn:
        return False
    for o in defn.get("options") or []:
        if not isinstance(o, dict):
            continue
        if str(o.get("value") or "").strip().lower() != val:
            continue
        lab = str(o.get("label") or "").strip()
        if re.search(r"未|无|否", lab):
            return True
    return False


def _eval_clause(
    facets: dict[str, str],
    clause: dict[str, Any],
    *,
    field_defs: dict[str, dict[str, Any]] | None = None,
) -> bool:
    facet = str(clause.get("facet") or "").strip()
    op = str(clause.get("op") or "eq").strip().lower()
    want = str(clause.get("value") or "").strip().lower()
    got = str(facets.get(facet) or "unknown").strip().lower()
    defn = _facet_field_def(field_defs, facet)
    # 号池未标注时：未注册/未登录类需求仍可选号，避免整池 unknown 导致永远租不到
    if got == "unknown":
        opts_unknown = {x.strip() for x in want.split(",") if x.strip()}
        if op == "in" and "unknown" in opts_unknown:
            return True
        if facet == "lifecycle" and op == "eq" and want == "unregistered":
            return True
        if facet == "session" and op == "in" and "unknown" in opts_unknown:
            return True
        if op == "eq" and _want_is_absence_state(defn, want):
            return True
    if op == "eq":
        if got == want:
            return True
        return _facet_values_equivalent(field_defs, facet, got, want)
    if op == "ne":
        return got != want
    if op == "in":
        opts = {x.strip() for x in want.split(",") if x.strip()}
        return got in opts
    return False


def account_facet_values_for_match(row: dict[str, Any] | None) -> dict[str, str]:
    """租号 / 筛号匹配用：库内 facets 真源 + 五维归一（含模板扩展字段）。"""
    row = row if isinstance(row, dict) else {}
    stored = row.get("facets") if isinstance(row.get("facets"), dict) else {}
    core = account_facets(row)
    out = dict(core)
    for key, val in stored.items():
        k = str(key or "").strip()
        v = str(val or "").strip().lower()
        if k and v:
            out[k] = v
    return out


def merge_observed_facets(
    stored: dict[str, str],
    observed: dict[str, str] | None,
) -> dict[str, str]:
    """探针观测覆盖 session；其余以池内登记为主。"""
    out = dict(stored) if isinstance(stored, dict) else normalize_facets(stored)
    obs = observed if isinstance(observed, dict) else {}
    for key in ("session",):
        val = str(obs.get(key) or "").strip().lower()
        if val and val in SESSION:
            out[key] = val
    return out


def match_requirements(
    facets: dict[str, str],
    requirements: dict[str, Any],
    *,
    observed: dict[str, str] | None = None,
    field_defs: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, int, list[str]]:
    """硬约束 all 不满足则 reject；prefer 只影响得分。"""
    eff = merge_observed_facets(facets if isinstance(facets, dict) else {}, observed)
    defs = field_defs if isinstance(field_defs, dict) else {}
    reasons: list[str] = []
    for clause in requirements.get("all") or []:
        if not isinstance(clause, dict):
            continue
        if not _eval_clause(eff, clause, field_defs=defs):
            facet = clause.get("facet", "?")
            reasons.append(f"不满足 {facet}={clause.get('value')}")
            return False, -100, reasons

    score = 0
    for clause in requirements.get("all") or []:
        if isinstance(clause, dict) and _eval_clause(eff, clause, field_defs=defs):
            score += 8
            reasons.append(f"{clause.get('facet')} ok")

    for clause in requirements.get("prefer") or []:
        if isinstance(clause, dict) and _eval_clause(eff, clause, field_defs=defs):
            score += 6
            reasons.append(f"偏好 {clause.get('facet')}")

    ident_bonus = 0
    if not reasons:
        reasons.append("满足全部约束")
    return True, score + ident_bonus, reasons


def pick_accounts_by_requirements(
    rows: list[dict],
    requirements: dict[str, Any],
    *,
    env: str = "",
    run_id: str = "",
    account_ident_fn: Callable[[dict | None], str],
    observed_by_account: dict[str, dict[str, str]] | None = None,
    ident_query: str = "",
    field_defs: list[dict[str, Any]] | None = None,
) -> list[dict]:
    env_key = str(requirements.get("env") or env or "").strip().lower()
    q = str(ident_query or "").strip().lower()
    scored: list[dict] = []
    obs_map = observed_by_account if isinstance(observed_by_account, dict) else {}
    defs_by_key = {
        str(d.get("key") or ""): d
        for d in (field_defs or [])
        if str(d.get("key") or "")
    }

    for row in rows or []:
        row_env = str(row.get("env") or "")
        if env_key and row_env and row_env != env_key:
            continue
        facets = account_facet_values_for_match(row)
        aid = str(row.get("id") or "")
        observed = obs_map.get(aid)
        ok, score, reasons = match_requirements(
            facets, requirements, observed=observed, field_defs=defs_by_key
        )
        if not ok:
            continue

        if row.get("locked"):
            score -= 8
            reasons.append("占用中")
        lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
        other_run = str(lease.get("run_id") or "").strip()
        rid = str(run_id or "").strip()
        if other_run and other_run != rid:
            score -= 100
            reasons.append("租用中")
        elif other_run and other_run == rid:
            score += 50
            reasons.append("本 run 已租")

        ident = account_ident_fn(row)
        if q and q in ident.lower():
            score += 20
            reasons.append("号码命中")

        scored.append({
            **row,
            "facets": facets,
            "score": int(score),
            "reason": " · ".join(reasons) or "匹配",
        })

    scored.sort(
        key=lambda x: (-int(x.get("score") or 0), account_ident_fn(x), str(x.get("env") or "")),
    )
    return scored[:48]


# --- Facet 转移（用例执行写回号池）---

_ALLOWED_TRANSITIONS: dict[tuple[str, str, str], set[str]] = {
    ("lifecycle", "unregistered", "registered"): {"case_pass", "manual", "provision"},
    ("session", "logged_out", "logged_in"): {"case_pass", "probe", "manual"},
    ("session", "logged_in", "logged_out"): {"case_pass", "reset", "manual"},
    ("session", "guest", "logged_in"): {"case_pass", "probe"},
    ("profile_data", "none", "filled"): {"case_pass", "manual"},
    ("address", "none", "filled"): {"case_pass", "manual"},
    ("health", "available", "dirty"): {"case_pass", "probe"},
    ("health", "dirty", "available"): {"reset", "manual"},
}


def apply_facet_updates(
    facets: dict[str, str],
    updates: dict[str, str],
    *,
    source: str = "case_pass",
    extension_keys: frozenset[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    out = account_facet_values_for_match({"facets": facets})
    errors: list[str] = []
    ext = extension_keys or frozenset()
    for key, new_val in (updates or {}).items():
        key = str(key).strip()
        new_val = str(new_val).strip().lower()
        if key in ext and key not in DEFAULT_FACETS:
            out[key] = new_val
            continue
        if key not in DEFAULT_FACETS:
            continue
        old = out.get(key, "unknown")
        if old == new_val:
            continue
        edge = (key, old, new_val)
        allowed = _ALLOWED_TRANSITIONS.get(edge)
        if allowed and source not in allowed:
            errors.append(f"禁止转移 {key}: {old}→{new_val} source={source}")
            continue
        if key == "lifecycle" and new_val not in LIFECYCLE:
            continue
        if key == "session" and new_val not in SESSION:
            continue
        out[key] = new_val
    core = normalize_facets(out)
    for key in ext:
        if key in out and key not in DEFAULT_FACETS:
            core[key] = out[key]
    return core, errors


def infer_pass_effects(precondition: str, expected: str) -> dict[str, str]:
    """用例成功后的默认 facet 效应（可被 case.meta.facet_effects 覆盖）。"""
    blob = f"{precondition} {expected}"
    effects: dict[str, str] = {}
    if re.search(r"注册", blob) and "未注册" not in expected:
        effects["lifecycle"] = "registered"
    if re.search(r"登录", blob) and "退出" not in blob and "登出" not in blob:
        effects["session"] = "logged_in"
    if re.search(r"退出|登出", blob):
        effects["session"] = "logged_out"
    return effects


def observe_session_from_probe(session_block: str) -> dict[str, str]:
    block = str(session_block or "").lower()
    m = re.search(r"session=(\w+)", block)
    if not m:
        return {}
    cur = str(m.group(1) or "").strip().lower()
    if cur == "logged_in":
        return {"session": "logged_in", "lifecycle": "registered"}
    if cur in ("logged_out", "guest"):
        return {"session": "logged_out" if cur == "logged_out" else "guest"}
    return {}


# --- Lease TTL ---

def lease_expired(lease: dict[str, Any], *, now: Optional[datetime] = None) -> bool:
    if not isinstance(lease, dict):
        return True
    exp = str(lease.get("expires_at") or "").strip()
    if not exp:
        return False
    try:
        dt = datetime.fromisoformat(exp)
    except ValueError:
        return False
    ref = now or datetime.now()
    return ref >= dt


def new_lease_record(run_id: str, *, ttl_sec: int = DEFAULT_LEASE_TTL_SEC) -> dict[str, str]:
    rid = str(run_id or "").strip()
    now = datetime.now()
    exp = now + timedelta(seconds=max(60, int(ttl_sec)))
    return {
        "run_id": rid[:80],
        "leased_at": now.isoformat(timespec="seconds"),
        "expires_at": exp.isoformat(timespec="seconds"),
    }


def sleep_wait(remaining_ms: int) -> None:
    if remaining_ms <= 0:
        return
    time.sleep(min(ACQUIRE_POLL_MS, remaining_ms) / 1000.0)
