"""前置 → ResourceClaim v1；合并进 case_scene / lease_requirements。"""
from __future__ import annotations

import copy
import re
from typing import Any

from mino_nexus.services.account_requirement_compile import (
    TITLE_ACCOUNT_LOGIN,
    TITLE_DEVICE_LOGIN,
    TITLE_ENV_PERM,
    TITLE_OTHER,
    augment_requirements_from_precondition,
    classify_precondition_title,
)
from mino_nexus.services.case_resource_key_catalog import CATALOG_VERSION
from mino_nexus.services.resource_pool import empty_requirements

_CLEAR_CACHE_RE = re.compile(r"清除.{0,6}缓存|清缓存|clear.{0,8}cache", re.I)
_LINE_NUM = re.compile(r"^\s*\d+[.)、．]\s*")


def _case_resource_key(case: dict[str, Any]) -> dict[str, Any] | None:
    rk = case.get("resource_key")
    if isinstance(rk, dict):
        return rk
    meta = case.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("resource_key"), dict):
        return meta["resource_key"]
    return None


def _set_case_resource_key(case: dict[str, Any], key: dict[str, Any]) -> None:
    case["resource_key"] = key
    meta = case.get("meta")
    if isinstance(meta, dict):
        meta["resource_key"] = key
    elif meta is None:
        case["meta"] = {"resource_key": key}


def _parse_precondition_lines(pre: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in re.split(r"[\n\r]+", str(pre or "")):
        chunk = _LINE_NUM.sub("", line.strip())
        if not chunk:
            continue
        sep = "：" if "：" in chunk else (":" if ":" in chunk else "")
        if sep:
            title, val = chunk.split(sep, 1)
            out.append((title.strip(), val.strip()))
        else:
            out.append(("", chunk))
    return out


def _required_session_from_value(val: str) -> str:
    from mino_nexus.services.session_match import precondition_device_session

    return precondition_device_session(val)


def _account_session_from_value(val: str) -> str:
    from mino_nexus.services.session_match import precondition_account_session

    return precondition_account_session(val)


def compile_resource_key_from_precondition(
    precondition: str,
    *,
    env_doc: dict | None = None,
    platform: str = "",
    package: str = "",
    env: str = "test",
) -> dict[str, Any]:
    pre = str(precondition or "").strip()
    req = empty_requirements()
    if env:
        req["env"] = str(env).strip().lower()
    if pre:
        req = augment_requirements_from_precondition(req, pre, env_doc)

    required_session = "any"
    account_required_session = "any"
    prep_items: list[dict[str, Any]] = []
    for title, val in _parse_precondition_lines(pre):
        blob = f"{title} {val}"
        kind = classify_precondition_title(title) if title else TITLE_OTHER
        if kind == TITLE_DEVICE_LOGIN:
            rs = _required_session_from_value(val)
            if rs != "any":
                required_session = rs
        elif kind == TITLE_ACCOUNT_LOGIN:
            ars = _account_session_from_value(val)
            if ars != "any":
                account_required_session = ars
        elif not title and re.search(r"未登录|已登录|已登出|已退出|游客", val):
            rs = _required_session_from_value(val)
            if rs != "any" and required_session == "any":
                required_session = rs
        if kind == TITLE_ENV_PERM or _CLEAR_CACHE_RE.search(blob):
            if _CLEAR_CACHE_RE.search(blob):
                prep_items.append(
                    {
                        "kind": "clear_cache",
                        "phase": "before_launch",
                        "text": val or "清除应用缓存",
                    }
                )

    plat = str(platform or "").strip().lower()
    if re.search(r"\bios\b|iphone|苹果", pre, re.I):
        plat = "ios"
    elif re.search(r"\bweb\b|浏览器|chromium", pre, re.I):
        plat = "web"
    elif re.search(r"安卓|android", pre, re.I):
        plat = plat or "android"

    device_app_session = "logged_out"
    if required_session == "guest":
        device_app_session = "logged_out"
    elif required_session == "logged_in":
        device_app_session = "logged_in"

    if required_session == "logged_in":
        session_prep = "skip"
    elif required_session in ("guest", "logged_out"):
        session_prep = "logout"
    else:
        session_prep = "skip"

    return {
        "version": CATALOG_VERSION,
        "platform": plat,
        "device_need": "app",
        "target_app": {"package": str(package or "").strip()},
        "device_app": {
            "required_session": device_app_session,
            "allow": ["logged_out", "guest"]
            if required_session == "guest"
            else (["logged_in"] if required_session == "logged_in" else ["logged_out", "guest", "logged_in"]),
            "prep": prep_items,
            "binding": "must_match_lease",
        },
        "account": {
            "env": str(env or "test").strip().lower(),
            "required_session": account_required_session,
            "requirements": req,
        },
        "case_scene": {
            "required_session": required_session if required_session != "any" else "guest"
            if prep_items and required_session == "any"
            else required_session,
            "session_prep": session_prep,
            "platform": plat,
            "device_need": "app",
            "prep_items": prep_items,
            "lease_requirements": req,
        },
    }


def ensure_resource_key_on_case(
    case: dict[str, Any],
    *,
    env_doc: dict | None = None,
    env: str = "test",
    package: str = "",
) -> dict[str, Any]:
    """若无 resource_key，从 precondition 编译并写回 case。"""
    if not isinstance(case, dict):
        return {}
    existing = _case_resource_key(case)
    if existing and int(existing.get("version") or 1) >= CATALOG_VERSION:
        return existing
    pre = str(case.get("precondition") or case.get("precondition_raw") or "").strip()
    if not pre:
        return {}
    plat = str(case.get("platform") or "").strip().lower()
    key = compile_resource_key_from_precondition(
        pre,
        env_doc=env_doc,
        platform=plat,
        package=package,
        env=env,
    )
    _set_case_resource_key(case, key)
    return key


def apply_claim_to_merged_scene(
    case: dict[str, Any],
    merged: dict[str, Any],
    *,
    env_doc: dict | None = None,
    env: str = "test",
    package: str = "",
) -> dict[str, Any]:
    """把 Claim 并入即将 clamp 的 scene 字典（已有 scene 字段优先）。"""
    out = dict(merged or {})
    key = _case_resource_key(case) or ensure_resource_key_on_case(
        case, env_doc=env_doc, env=env, package=package
    )
    if not key:
        return out
    scene_patch = key.get("case_scene") if isinstance(key.get("case_scene"), dict) else {}
    for field in ("platform", "device_need"):
        if not out.get(field) and scene_patch.get(field):
            out[field] = scene_patch[field]
    if str(out.get("required_session") or "any") in ("", "any") and scene_patch.get("required_session"):
        out["required_session"] = scene_patch["required_session"]
    if not out.get("prep_items") and scene_patch.get("prep_items"):
        out["prep_items"] = copy.deepcopy(scene_patch["prep_items"])
    lr = out.get("lease_requirements")
    patch_lr = scene_patch.get("lease_requirements")
    key_ver = int(key.get("version") or 1)
    if key_ver >= 2 and isinstance(patch_lr, dict) and (patch_lr.get("all") or patch_lr.get("prefer")):
        out["lease_requirements"] = copy.deepcopy(patch_lr)
    elif not isinstance(lr, dict) or not lr.get("all"):
        if isinstance(patch_lr, dict) and patch_lr.get("all"):
            out["lease_requirements"] = copy.deepcopy(patch_lr)
    acc = key.get("account") if isinstance(key.get("account"), dict) else {}
    if isinstance(acc.get("requirements"), dict) and not out.get("lease_requirements"):
        out["lease_requirements"] = copy.deepcopy(acc["requirements"])
    return out


def project_package_ids(project_id: str, *, app_id: str = "") -> list[str]:
    """项目下被测 App 包名集合（Android 优先，用于机态列表过滤）。"""
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.app_automation import package_for_app

    pid = str(project_id or "").strip()
    out: list[str] = []
    seen: set[str] = set()
    aid = str(app_id or "").strip()
    if aid:
        try:
            app = ps.require_app(aid)
            if not pid or str(app.get("project_id") or "") == pid:
                for plat in ("android", "ios", "web"):
                    pkg = str(package_for_app(app, platform=plat) or "").strip()
                    if pkg and pkg not in seen:
                        seen.add(pkg)
                        out.append(pkg)
        except KeyError:
            pass
    if pid:
        for app in ps.list_apps(project_id=pid) or []:
            for plat in ("android", "ios"):
                pkg = str(package_for_app(app, platform=plat) or "").strip()
                if pkg and pkg not in seen:
                    seen.add(pkg)
                    out.append(pkg)
    return out


def default_package_for_project(project_id: str, *, app_id: str = "") -> str:
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.app_automation import package_for_app

    pid = str(project_id or "").strip()
    aid = str(app_id or "").strip()
    if aid:
        try:
            app = ps.require_app(aid)
            if str(app.get("project_id") or "") == pid or not pid:
                pkg = str(package_for_app(app, platform="android") or "").strip()
                if pkg:
                    return pkg
        except KeyError:
            pass
    if not pid:
        return ""
    for app in ps.list_apps(project_id=pid) or []:
        pkg = str(package_for_app(app, platform="android") or "").strip()
        if pkg:
            return pkg
    return ""


def sync_case_resource_metadata(
    row: dict[str, Any],
    *,
    project_id: str = "",
    env_doc: dict | None = None,
    package: str = "",
    env: str = "test",
    force_recompile: bool = False,
) -> dict[str, Any]:
    """保存用例前编译 resource_key，并补全缺失的 case_scene 字段。"""
    out = dict(row)
    if force_recompile:
        out.pop("resource_key", None)
        meta = out.get("meta")
        if isinstance(meta, dict):
            meta.pop("resource_key", None)
    pid = str(project_id or out.get("project_id") or "").strip()
    if env_doc is None and pid:
        try:
            from mino_nexus.services import project_store as ps

            env_doc = ps.project_env(pid)
        except KeyError:
            env_doc = None
    pkg = str(package or out.get("target_package") or "").strip()
    if not pkg and pid:
        pkg = default_package_for_project(pid, app_id=str(out.get("app_id") or ""))
    key = ensure_resource_key_on_case(out, env_doc=env_doc, env=env, package=pkg)
    if not key:
        return out
    scene_existing = out.get("case_scene") if isinstance(out.get("case_scene"), dict) else {}
    merged = apply_claim_to_merged_scene(
        out,
        dict(scene_existing),
        env_doc=env_doc,
        env=env,
        package=pkg,
    )
    from mino_nexus.runtime.session_gate import clamp_case_scene

    if not scene_existing:
        out["case_scene"] = clamp_case_scene(merged)
    else:
        patched = dict(scene_existing)
        for field in ("prep_items", "lease_requirements", "required_session", "platform", "device_need"):
            if not patched.get(field) and merged.get(field):
                patched[field] = merged[field]
        out["case_scene"] = clamp_case_scene(patched)
    return out


def preflight_device_app_gap(
    claim: dict[str, Any] | None,
    *,
    sn: str,
    package_id: str,
) -> list[str]:
    """对照登记簿返回未满足项（供日志 / 后续硬门槛）。"""
    if not claim or not sn or not package_id:
        return []
    da = claim.get("device_app") if isinstance(claim.get("device_app"), dict) else {}
    req = str(da.get("required_session") or "").strip().lower()
    allow = [str(x).strip().lower() for x in (da.get("allow") or []) if str(x).strip()]
    from mino_nexus.services.device_app_session_store import get_session

    row = get_session(sn, package_id) or {}
    from mino_nexus.services.session_match import normalize_device_session

    cur = normalize_device_session(row.get("session") or "")
    gaps: list[str] = []
    if req and req not in ("any", ""):
        from mino_nexus.services.session_match import device_session_meets

        ok = device_session_meets(req, cur, allow)
        if not ok:
            gaps.append(f"device_app.session 需要 {req}（允许 {allow or [req]}），当前 {cur}")
    for prep in da.get("prep") or []:
        if not isinstance(prep, dict):
            continue
        if str(prep.get("kind") or "") == "clear_cache" and cur in ("logged_in",) and not row.get("stale"):
            gaps.append("需要 clear_cache（当前机态可能仍带登录缓存）")
    return gaps
