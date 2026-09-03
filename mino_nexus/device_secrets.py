"""设备锁屏密码。明文落盘，跟 SMTP 一样，不另做加密。"""
from __future__ import annotations

from typing import Any

from mino_nexus.json_store import load_json, save_json

_FILE = "device_secrets.json"


def _root() -> dict[str, Any]:
    raw = load_json(_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    pwds = raw.get("lock_passwords")
    if not isinstance(pwds, dict):
        pwds = {}
    return {"lock_passwords": pwds}


def get_lock_password(sn: str) -> str:
    key = str(sn or "").strip()
    if not key:
        return ""
    return str(_root()["lock_passwords"].get(key) or "").strip()


def lock_password_public(sn: str) -> dict[str, Any]:
    return {"password_configured": bool(get_lock_password(sn))}


def set_lock_password(sn: str, password: str) -> dict[str, Any]:
    key = str(sn or "").strip()
    if not key:
        raise ValueError("缺少设备 SN")
    root = _root()
    pwd = str(password or "").strip()
    if pwd:
        root["lock_passwords"][key] = pwd
    else:
        root["lock_passwords"].pop(key, None)
    save_json(_FILE, root)
    return lock_password_public(key)
