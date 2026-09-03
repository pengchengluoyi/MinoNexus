"""沉淀知识 / 知识机审开关：按被测产品登录账号隔离。

不是 Mino Console/Studio 管理员（admin / Mino@local）。
真源是 settings.json 的 knowledge_jobs.accounts[<account_id>]。
旧的顶层 capture_enabled / review_enabled 只当缺省，不删导入知识。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.json_store import load_json, save_json

_FILE = "settings.json"


def _root() -> dict[str, Any]:
    raw = load_json(_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("knowledge_jobs", {})
    return raw


def _save(root: dict[str, Any]) -> None:
    save_json(_FILE, root)


def _legacy_defaults(raw: dict[str, Any] | None) -> dict[str, bool]:
    src = raw if isinstance(raw, dict) else {}
    return {
        "capture_enabled": src.get("capture_enabled", True) is not False,
        "review_enabled": src.get("review_enabled", True) is not False,
    }


def _accounts_map(raw: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    src = raw if isinstance(raw, dict) else {}
    accounts = src.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, val in accounts.items():
        if not isinstance(val, dict):
            continue
        aid = str(val.get("account_id") or key or "").strip()
        if not aid:
            continue
        out[aid] = {
            "account_id": aid,
            "account_ident": str(val.get("account_ident") or "").strip(),
            "capture_enabled": val.get("capture_enabled", True) is not False,
            "review_enabled": val.get("review_enabled", True) is not False,
        }
    return out


def resolve_app_account(
    *,
    account_id: str = "",
    account_ident: str = "",
    project_id: str = "",
    app_id: str = "",
) -> dict[str, str]:
    from mino_nexus.project_store import find_test_account

    row = find_test_account(
        account_id=account_id,
        account_ident=account_ident,
        project_id=project_id,
        app_id=app_id,
    )
    if not row:
        raise ValueError("未找到应用登录账号")
    return {
        "account_id": str(row.get("id") or "").strip(),
        "account_ident": str(row.get("account_ident") or "").strip(),
    }


def list_knowledge_job_settings() -> list[dict[str, Any]]:
    raw = _root().get("knowledge_jobs")
    return list(_accounts_map(raw if isinstance(raw, dict) else {}).values())


def get_knowledge_job_settings(
    *,
    account_id: str = "",
    account_ident: str = "",
) -> dict[str, Any]:
    raw = _root().get("knowledge_jobs")
    if not isinstance(raw, dict):
        raw = {}
    defaults = _legacy_defaults(raw)
    accounts = list_knowledge_job_settings()
    want_id = str(account_id or "").strip()
    want_ident = str(account_ident or "").strip()
    if not want_id and not want_ident:
        return {
            **defaults,
            "account_id": "",
            "account_ident": "",
            "accounts": accounts,
        }
    hit = next((x for x in accounts if want_id and x["account_id"] == want_id), None)
    if not hit and want_ident:
        hit = next((x for x in accounts if x.get("account_ident") == want_ident), None)
    if hit:
        return hit
    return {
        **defaults,
        "account_id": want_id,
        "account_ident": want_ident,
    }


def save_knowledge_job_settings(
    *,
    capture_enabled: bool = True,
    review_enabled: bool = True,
    account_id: str = "",
    account_ident: str = "",
) -> dict[str, Any]:
    aid = str(account_id or "").strip()
    ident = str(account_ident or "").strip()
    if not aid and not ident:
        raise ValueError("请选择应用登录账号")
    root = _root()
    raw = root.get("knowledge_jobs")
    if not isinstance(raw, dict):
        raw = {}
    accounts = _accounts_map(raw)
    key = aid or next((k for k, v in accounts.items() if v.get("account_ident") == ident), "") or ident
    prev = accounts.get(key) or {}
    accounts[key] = {
        "account_id": aid or str(prev.get("account_id") or key),
        "account_ident": ident or str(prev.get("account_ident") or ""),
        "capture_enabled": bool(capture_enabled),
        "review_enabled": bool(review_enabled),
    }
    next_raw: dict[str, Any] = {"accounts": accounts}
    if "capture_enabled" in raw:
        next_raw["capture_enabled"] = raw.get("capture_enabled", True) is not False
    if "review_enabled" in raw:
        next_raw["review_enabled"] = raw.get("review_enabled", True) is not False
    root["knowledge_jobs"] = next_raw
    _save(root)
    saved = accounts[key]
    return get_knowledge_job_settings(account_id=saved["account_id"], account_ident=saved["account_ident"])


def knowledge_capture_enabled(*, account_id: str = "", account_ident: str = "") -> bool:
    return bool(get_knowledge_job_settings(account_id=account_id, account_ident=account_ident).get("capture_enabled", True))


def knowledge_review_enabled(*, account_id: str = "", account_ident: str = "") -> bool:
    return bool(get_knowledge_job_settings(account_id=account_id, account_ident=account_ident).get("review_enabled", True))
