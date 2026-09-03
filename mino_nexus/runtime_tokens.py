"""Studio 安装 Scout 时领取的短 TTL 节点凭证。"""
from __future__ import annotations

import hmac
import secrets
import time
from typing import Any

from mino_nexus.json_store import load_json, save_json

_FILE = "install_tokens.json"
_TTL_SEC = 15 * 60


def _now() -> int:
    return int(time.time())


def _root() -> dict[str, Any]:
    raw = load_json(_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("tokens", [])
    return raw


def _purge(root: dict[str, Any]) -> None:
    now = _now()
    root["tokens"] = [
        t for t in (root.get("tokens") or [])
        if isinstance(t, dict) and int(t.get("expires_at") or 0) > now
    ]


def issue(*, user_id: str = "") -> dict[str, Any]:
    root = _root()
    _purge(root)
    token = secrets.token_urlsafe(24)
    row = {
        "token": token,
        "user_id": str(user_id or ""),
        "expires_at": _now() + _TTL_SEC,
        "created_at": _now(),
    }
    root["tokens"].append(row)
    save_json(_FILE, root)
    return {"token": token, "expires_at": row["expires_at"], "ttl_sec": _TTL_SEC}


def peek_token(token: str) -> dict[str, Any] | None:
    tok = str(token or "").strip()
    if not tok:
        return None
    root = _root()
    _purge(root)
    for row in root.get("tokens") or []:
        if isinstance(row, dict) and hmac.compare_digest(str(row.get("token") or ""), tok):
            return dict(row)
    return None


def install_token_ok(token: str) -> bool:
    return peek_token(token) is not None
