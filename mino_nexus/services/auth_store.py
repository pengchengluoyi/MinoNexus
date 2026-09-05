"""账号。形状对齐 Console / Studio 的 `/auth/*`，落 users / sessions 表。"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Any, Optional

from mino_nexus.core.log import SLog

TAG = "Auth"

_ALG = "pbkdf2_sha256"
_ITER = 210000
_SESSION_TTL = 30 * 24 * 3600
_MAX_SESSIONS_PER_USER = 16
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,31}$")
_DISPOSABLE = {
    "mailinator.com", "tempmail.com", "10minutemail.com", "guerrillamail.com",
    "trashmail.com", "yopmail.com", "temp-mail.org", "sharklasers.com",
}
_CODE_TTL = 600
_SEND_COOLDOWN = 60
_MAX_SEND_HOUR = 5
_MAX_ATTEMPTS = 5
# 账号角色只有两种：管理员可进 Console；用户仅 Studio。
_ROLES = ("admin", "user")
_ROLE_ALIASES = {
    "platform_admin": "admin",
    "org_admin": "admin",
    "qa_lead": "user",
    "operator": "user",
    "viewer": "user",
}


def normalize_role(role: str = "") -> str:
    raw = str(role or "").strip()
    mapped = _ROLE_ALIASES.get(raw, raw)
    return mapped if mapped in _ROLES else "user"


def is_admin_role(role: str = "") -> bool:
    return normalize_role(role) == "admin"


def _now() -> int:
    return int(time.time())


def _hash_password(password: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITER)
    return f"{_ALG}${_ITER}${salt}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _alg, it, salt, hx = str(stored or "").split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), hx)
    except Exception:
        return False


def _root() -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.auth import AuthSession, AuthState, User

    ensure_db()
    db = SessionLocal()
    try:
        users = [dict(u.extra or {}) or {
            "user_id": u.user_id, "username": u.username, "name": u.name,
            "role": u.role, "password_hash": u.password_hash,
        } for u in db.query(User).all()]
        sessions = [dict(s.extra or {}) or {
            "token": s.token, "user_id": s.user_id, "expires_at": s.expires_at,
        } for s in db.query(AuthSession).all()]
        state = db.get(AuthState, "main")
        extra = dict(state.payload or {}) if state and isinstance(state.payload, dict) else {}
        return {
            "users": users,
            "sessions": sessions,
            "codes": extra.get("codes") or [],
            "agent_sessions": extra.get("agent_sessions") or {},
        }
    finally:
        db.close()


def _save(root: dict[str, Any]) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.auth import AuthSession, AuthState, User

    with session_scope() as db:
        keep_u = set()
        for row in root.get("users") or []:
            if not isinstance(row, dict):
                continue
            uid = str(row.get("user_id") or row.get("id") or "")
            if not uid:
                continue
            keep_u.add(uid)
            db.merge(User(
                user_id=uid,
                username=str(row.get("username") or ""),
                name=str(row.get("name") or ""),
                role=str(row.get("role") or "user"),
                password_hash=str(row.get("password_hash") or row.get("password") or ""),
                extra=dict(row),
            ))
        for row in db.query(User).all():
            if row.user_id not in keep_u:
                db.delete(row)
        keep_s = set()
        for row in root.get("sessions") or []:
            if not isinstance(row, dict) or not row.get("token"):
                continue
            tok = str(row["token"])
            keep_s.add(tok)
            db.merge(AuthSession(
                token=tok,
                user_id=str(row.get("user_id") or ""),
                extra=dict(row),
                expires_at=int(row.get("expires_at") or 0),
            ))
        for row in db.query(AuthSession).all():
            if row.token not in keep_s:
                db.delete(row)
        db.merge(AuthState(id="main", payload={
            "codes": root.get("codes") or [],
            "agent_sessions": root.get("agent_sessions") or {},
        }))


def _purge(root: dict[str, Any]) -> None:
    now = _now()
    root["sessions"] = [
        s for s in (root.get("sessions") or [])
        if isinstance(s, dict) and int(s.get("expires_at") or 0) > now
    ]


def _norm_email(email: str) -> str:
    return str(email or "").strip().lower()


def _norm_username(value: str) -> str:
    return str(value or "").strip().lower()


def _norm_name(name: str) -> str:
    return str(name or "").strip()


def _valid_username(username: str) -> bool:
    return bool(_USER_RE.match(username))


def _valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 120


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def _hash_code(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def users() -> list[dict[str, Any]]:
    return [u for u in (_root().get("users") or []) if isinstance(u, dict)]


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    email = _norm_email(user.get("email") or "")
    username = _norm_username(user.get("username") or "")
    name = _norm_name(user.get("name") or username or "")
    handle = username or name or (email.split("@")[0] if email else "")
    role = normalize_role(user.get("role") or "user")
    created = user.get("created_at")
    try:
        created_at = int(created) if created not in (None, "") else None
    except (TypeError, ValueError):
        created_at = None
    return {
        "user_id": str(user.get("id") or user.get("user_id") or ""),
        "email": email,
        "name": name or handle,
        "username": handle,
        "email_verified": user.get("email_verified") is not False,
        "role": role,
        "created_at": created_at,
        "status": "active",
    }


def _find_user(ident: str) -> Optional[dict[str, Any]]:
    email_key = _norm_email(ident)
    name_key = _norm_username(ident)
    if not email_key and not name_key:
        return None
    for user in users():
        if email_key and _norm_email(user.get("email") or "") == email_key:
            return user
        if name_key and _norm_username(user.get("username") or "") == name_key:
            return user
        if name_key and _norm_name(user.get("name") or "").lower() == name_key:
            return user
    return None


def session_of(token: str) -> Optional[dict[str, Any]]:
    tok = str(token or "").strip()
    if not tok:
        return None
    root = _root()
    _purge(root)
    for row in root.get("sessions") or []:
        if isinstance(row, dict) and hmac.compare_digest(str(row.get("token") or ""), tok):
            return row
    return None


def _issue_session(user: dict[str, Any]) -> dict[str, Any]:
    root = _root()
    _purge(root)
    token = secrets.token_hex(24)
    pub = _public_user(user)
    sess = {
        "token": token,
        "user_id": pub["user_id"],
        "email": pub["email"],
        "name": pub["name"],
        "username": pub["username"],
        "role": pub["role"],
        "email_verified": pub.get("email_verified") is not False,
        "expires_at": _now() + _SESSION_TTL,
    }
    uid = pub["user_id"]
    kept = [s for s in (root.get("sessions") or []) if isinstance(s, dict) and str(s.get("user_id") or "") != uid]
    mine = [s for s in (root.get("sessions") or []) if isinstance(s, dict) and str(s.get("user_id") or "") == uid]
    mine.append(sess)
    if len(mine) > _MAX_SESSIONS_PER_USER:
        mine = mine[-_MAX_SESSIONS_PER_USER:]
    root["sessions"] = kept + mine
    _save(root)
    return {**pub, "token": token, "ws_token": token, "expires_at": sess["expires_at"]}


def require_session(token: str = "") -> dict[str, Any]:
    sess = session_of(token)
    if not sess:
        raise PermissionError("请先登录")
    return sess


def needs_setup() -> bool:
    return not any(_norm_email(u.get("email") or "") for u in users())


def mail_configured() -> bool:
    from mino_nexus.services.settings_store import mail_ready
    return mail_ready()


def status(token: str = "", client: str = "") -> dict[str, Any]:
    ensure_seed_users()
    sess = session_of(token)
    if sess and str(client or "").strip().lower() == "console" and not is_admin_role(sess.get("role") or ""):
        raise PermissionError("仅管理员可登录控制台")
    pub = _public_user(sess or {}) if sess else {
        "user_id": "", "email": "", "name": "", "username": "", "role": "",
    }
    return {
        "logged_in": bool(sess),
        "needs_setup": needs_setup(),
        "mail_configured": mail_configured(),
        "ws_token": str(sess.get("token") or "") if sess else "",
        **pub,
    }


def bootstrap(token: str = "", client: str = "") -> dict[str, Any]:
    sess = require_session(token)
    pub = _public_user(sess)
    role = pub["role"]
    if str(client or "").strip().lower() == "console" and not is_admin_role(role):
        raise PermissionError("仅管理员可登录控制台")
    can_admin = is_admin_role(role)
    from mino_nexus.services import studio_nav

    return {
        **status(token, client=client),
        "client": client or "",
        "capabilities": {
            "manage_users": can_admin,
            "write_mail": can_admin,
            "write_packs": can_admin,
            "write_keys": True,
            "write_plugins": can_admin,
            "run_cases": True,
            "install_scout": True,
            "write_studio_nav": can_admin,
        },
        "studio_nav": studio_nav.public_nav(),
    }


def _purge_codes(root: dict[str, Any]) -> None:
    now = _now()
    root["codes"] = [
        c for c in (root.get("codes") or [])
        if isinstance(c, dict) and int(c.get("expires_at") or 0) > now
    ]


def _codes_for(email: str, purpose: str) -> list[dict[str, Any]]:
    em = _norm_email(email)
    return [
        c for c in (_root().get("codes") or [])
        if isinstance(c, dict)
        and _norm_email(c.get("email") or "") == em
        and str(c.get("purpose") or "") == purpose
    ]


def send_code(email: str, purpose: str = "register") -> dict[str, Any]:
    from mino_nexus.services.settings_store import send_mail

    em = _norm_email(email)
    purpose = str(purpose or "register").strip() or "register"
    if purpose not in ("register",):
        raise ValueError("不支持的验证码用途")
    if not _valid_email(em):
        raise ValueError("请填写有效邮箱")
    if _email_domain(em) in _DISPOSABLE:
        raise ValueError("请用常用邮箱，不要用临时邮箱")
    if purpose == "register" and any(_norm_email(u.get("email") or "") == em for u in users()):
        raise ValueError("这个邮箱已经注册")
    if not mail_configured():
        raise RuntimeError("还没有配置发信邮箱。到控制台 → 密钥 → 发信邮箱填 SMTP。")
    now = _now()
    recent = [c for c in _codes_for(em, purpose) if now - int(c.get("sent_at") or 0) < 3600]
    last = max((int(c.get("sent_at") or 0) for c in recent), default=0)
    if last and now - last < _SEND_COOLDOWN:
        raise ValueError(f"请 {_SEND_COOLDOWN - (now - last)} 秒后再发")
    if len(recent) >= _MAX_SEND_HOUR:
        raise ValueError("这个邮箱一小时内发得太勤，稍后再试")
    code = f"{secrets.randbelow(1000000):06d}"
    salt = secrets.token_hex(8)
    root = _root()
    _purge_codes(root)
    root["codes"] = [
        c for c in (root.get("codes") or [])
        if not (isinstance(c, dict) and _norm_email(c.get("email") or "") == em and c.get("purpose") == purpose)
    ]
    root["codes"].append({
        "email": em,
        "purpose": purpose,
        "salt": salt,
        "code_hash": _hash_code(code, salt),
        "sent_at": now,
        "expires_at": now + _CODE_TTL,
        "attempts": 0,
    })
    _save(root)
    send_mail(
        to=em,
        subject="Mino 邮箱验证码",
        body=f"你的验证码是 {code}，10 分钟内有效。\n如果不是你在注册，忽略这封信即可。\n",
    )
    return {"email": em, "ttl_sec": _CODE_TTL, "resend_sec": _SEND_COOLDOWN}


def _consume_code(email: str, purpose: str, code: str) -> None:
    em = _norm_email(email)
    raw = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", raw):
        raise ValueError("请填写 6 位验证码")
    root = _root()
    _purge_codes(root)
    idx = next(
        (
            i for i, c in enumerate(root.get("codes") or [])
            if isinstance(c, dict)
            and _norm_email(c.get("email") or "") == em
            and c.get("purpose") == purpose
        ),
        -1,
    )
    if idx < 0:
        raise ValueError("请先获取验证码")
    row = root["codes"][idx]
    if int(row.get("attempts") or 0) >= _MAX_ATTEMPTS:
        root["codes"].pop(idx)
        _save(root)
        raise ValueError("验证码试错太多次，请重新获取")
    expected = str(row.get("code_hash") or "")
    got = _hash_code(raw, str(row.get("salt") or ""))
    if not hmac.compare_digest(expected, got):
        row["attempts"] = int(row.get("attempts") or 0) + 1
        _save(root)
        raise ValueError("验证码不对")
    root["codes"].pop(idx)
    _save(root)


def register(email: str = "", password: str = "", name: str = "", username: str = "", code: str = "") -> dict[str, Any]:
    """邮箱注册：只校验邮箱格式与是否已注册，不校验验证码 / captcha。"""
    ensure_seed_users()
    em = _norm_email(email)
    pwd = str(password or "")
    display = _norm_name(name) or (em.split("@")[0] if em else "")
    if not _valid_email(em):
        raise ValueError("请填写有效邮箱")
    if len(pwd) < 8:
        raise ValueError("密码至少 8 位")
    if len(display) > 32:
        raise ValueError("名称最多 32 个字符")
    if any(_norm_email(u.get("email") or "") == em for u in users()):
        raise ValueError("这个邮箱已经注册")
    uname = _norm_username(username) or _norm_username(em.split("@")[0])
    if not _valid_username(uname):
        uname = f"u{secrets.token_hex(3)}"
    base = uname
    n = 0
    while any(_norm_username(u.get("username") or "") == uname for u in users()):
        n += 1
        uname = f"{base}{n}"
    # 首个邮箱账号若尚无用户则升管理员；否则一律普通用户。种子 admin 通常已存在。
    role = "admin" if not users() else "user"
    root = _root()
    user = {
        "id": secrets.token_hex(8),
        "email": em,
        "username": uname,
        "name": display,
        "password_hash": _hash_password(pwd),
        "email_verified": True,
        "role": role,
        "created_at": _now(),
    }
    root["users"] = [*(root.get("users") or []), user]
    _save(root)
    return _issue_session(user)


def login(email: str = "", password: str = "", username: str = "", client: str = "") -> dict[str, Any]:
    ensure_seed_users()
    ident = _norm_username(username) or _norm_email(email) or _norm_name(username or email)
    pwd = str(password or "")
    hit = _find_user(ident)
    if not hit or not _verify_password(pwd, str(hit.get("password_hash") or "")):
        raise PermissionError("账号或密码不对")
    if str(client or "").strip().lower() == "console" and not is_admin_role(hit.get("role") or ""):
        raise PermissionError("仅管理员可登录控制台")
    return _issue_session(hit)


def create_local_user(
    username: str = "",
    password: str = "",
    name: str = "",
    email: str = "",
    role: str = "user",
) -> dict[str, Any]:
    uname = _norm_username(username or email)
    pwd = str(password or "")
    display = _norm_name(name) or uname
    em = _norm_email(email)
    role = normalize_role(role or "user")
    if role not in _ROLES:
        raise ValueError("未知角色")
    if em and not _valid_username(uname):
        uname = _norm_username(em.split("@")[0])
    if not _valid_username(uname):
        raise ValueError("账号用 2–32 位字母开头，可含数字、点、下划线或短横线")
    if em and not _valid_email(em):
        raise ValueError("邮箱格式不对")
    if len(pwd) < 8:
        raise ValueError("密码至少 8 位")
    if len(display) > 32:
        raise ValueError("名称最多 32 个字符")
    if any(_norm_username(u.get("username") or "") == uname for u in users()):
        raise ValueError("这个账号已经存在")
    if em and any(_norm_email(u.get("email") or "") == em for u in users()):
        raise ValueError("这个邮箱已经占用")
    root = _root()
    user = {
        "id": secrets.token_hex(8),
        "email": em,
        "username": uname,
        "name": display,
        "password_hash": _hash_password(pwd),
        "email_verified": bool(em),
        "role": role,
        "created_at": _now(),
    }
    root["users"] = [*(root.get("users") or []), user]
    _save(root)
    return _public_user(user)


def ensure_seed_users() -> None:
    if users():
        return
    password = str(os.environ.get("MINO_BOOTSTRAP_PASSWORD") or "Mino@local")
    created = create_local_user(
        username="admin",
        password=password,
        name="管理员",
        role="admin",
    )
    SLog.i(TAG, f"已写入初始账号 {created.get('username')}（改密码：环境变量 MINO_BOOTSTRAP_PASSWORD）")


def public_user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    uid = str(user_id or "").strip()
    if not uid:
        return None
    for user in users():
        if str(user.get("id") or user.get("user_id") or "") == uid:
            return _public_user(user)
    return None


def seed_admin_user() -> dict[str, Any]:
    """导入数据缺创建人时回填的默认账号：Nexus 种子用户 `admin`。"""
    ensure_seed_users()
    for user in users():
        if _norm_username(user.get("username") or "") == "admin":
            return _public_user(user)
    rows = users()
    if rows:
        return _public_user(rows[0])
    return {"user_id": "", "name": "管理员", "username": "admin", "role": "admin"}


def list_accounts() -> list[dict[str, Any]]:
    ensure_seed_users()
    return [_public_user(u) for u in users()]


def delete_account(user_id: str, *, actor_id: str = "") -> None:
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("缺少账号")
    if actor_id and uid == str(actor_id):
        raise ValueError("不能删当前登录的账号")
    rows = users()
    if len(rows) <= 1:
        raise ValueError("至少保留一个账号")
    if not any(str(u.get("id") or u.get("user_id") or "") == uid for u in rows):
        raise ValueError("没有这个账号")
    root = _root()
    root["users"] = [
        u for u in (root.get("users") or [])
        if not (isinstance(u, dict) and str(u.get("id") or u.get("user_id") or "") == uid)
    ]
    root["sessions"] = [
        s for s in (root.get("sessions") or [])
        if not (isinstance(s, dict) and str(s.get("user_id") or "") == uid)
    ]
    _save(root)


def logout(token: str = "") -> None:
    tok = str(token or "").strip()
    if not tok:
        return
    root = _root()
    root["sessions"] = [
        s for s in (root.get("sessions") or [])
        if not (isinstance(s, dict) and hmac.compare_digest(str(s.get("token") or ""), tok))
    ]
    _save(root)


def list_agent_sessions(token: str) -> list[dict[str, Any]]:
    sess = require_session(token)
    root = _root()
    rows = (root.get("agent_sessions") or {}).get(sess["user_id"]) or []
    return [r for r in rows if isinstance(r, dict)]


def save_agent_sessions(token: str, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sess = require_session(token)
    rows = [s for s in (sessions or []) if isinstance(s, dict)]
    root = _root()
    store = root.get("agent_sessions")
    if not isinstance(store, dict):
        store = {}
        root["agent_sessions"] = store
    store[sess["user_id"]] = rows
    _save(root)
    return rows
