"""SMTP 与模型 Key。落 settings 表。"""
from __future__ import annotations

import re
import smtplib
from email.message import EmailMessage
from typing import Any, Optional

AI_PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "name": "OpenAI",
        "api_type": "openai",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "model_options": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "api_type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-20250514",
        "model_options": ["claude-3-5-sonnet-latest", "claude-sonnet-4-20250514"],
    },
    {
        "id": "google",
        "name": "Google Gemini",
        "api_type": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.5-flash",
        "model_options": ["gemini-2.5-flash", "gemini-2.5-pro"],
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "api_type": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "model_options": ["deepseek-chat", "deepseek-reasoner"],
    },
    {
        "id": "qwen",
        "name": "通义千问",
        "api_type": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "model_options": ["qwen-plus", "qwen-max", "qwen-turbo"],
    },
    {
        "id": "volcengine",
        "name": "火山引擎",
        "api_type": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-seed-2-0-lite-260215",
        "model_options": [],
    },
]


def _root() -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.settings import Settings

    ensure_db()
    db = SessionLocal()
    try:
        row = db.get(Settings, "main")
        raw = dict(row.payload or {}) if row and isinstance(row.payload, dict) else {}
    finally:
        db.close()
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("mail", {})
    raw.setdefault("ai", {})
    raw.setdefault("layer_stack", {})
    raw.setdefault("figma", {})
    raw.setdefault("integrations", {})
    raw.setdefault("robots", {"items": []})
    raw.setdefault("knowledge_jobs", {})
    raw.setdefault("knowledge_job_users", {})
    raw.setdefault("case_import", {})
    return raw


def _save(root: dict[str, Any]) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.settings import Settings

    with session_scope() as db:
        row = db.get(Settings, "main")
        if row is None:
            db.add(Settings(id="main", payload=root))
        else:
            row.payload = root


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return secret[:4] + "****" + secret[-4:]


def get_mail_settings() -> dict[str, Any]:
    mail = _root().get("mail") if isinstance(_root().get("mail"), dict) else {}
    password = str(mail.get("password") or "").strip()
    host = str(mail.get("host") or "").strip()
    username = str(mail.get("username") or "").strip()
    from_email = str(mail.get("from_email") or username).strip()
    return {
        "host": host,
        "port": int(mail.get("port") or 587),
        "username": username,
        "password_masked": _mask_secret(password),
        "from_email": from_email,
        "from_name": str(mail.get("from_name") or "Mino").strip() or "Mino",
        "use_tls": mail.get("use_tls") is not False,
        "configured": bool(host and username and password and from_email),
    }


def mail_ready() -> bool:
    return bool(get_mail_settings().get("configured"))


def save_mail_settings(
    *,
    host: str = "",
    port: int = 587,
    username: str = "",
    password: str = "",
    clear_password: bool = False,
    from_email: str = "",
    from_name: str = "",
    use_tls: bool = True,
) -> dict[str, Any]:
    root = _root()
    mail = root.setdefault("mail", {})
    if not isinstance(mail, dict):
        mail = {}
        root["mail"] = mail
    mail["host"] = str(host or "").strip()
    mail["port"] = int(port or 587)
    mail["username"] = str(username or "").strip()
    mail["from_email"] = str(from_email or mail.get("from_email") or mail["username"]).strip()
    mail["from_name"] = str(from_name or "Mino").strip() or "Mino"
    mail["use_tls"] = use_tls is not False
    if clear_password:
        mail["password"] = ""
    elif str(password or "").strip():
        mail["password"] = str(password).strip()
    _save(root)
    return get_mail_settings()


def _mail_credentials() -> dict[str, Any]:
    mail = _root().get("mail") if isinstance(_root().get("mail"), dict) else {}
    public = get_mail_settings()
    return {**public, "password": str(mail.get("password") or "").strip()}


def send_mail(*, to: str, subject: str, body: str) -> None:
    cfg = _mail_credentials()
    if not cfg.get("configured"):
        raise RuntimeError("还没有配置发信邮箱。到控制台 → 密钥 → 发信邮箱填 SMTP。")
    to = str(to or "").strip()
    if not to:
        raise ValueError("缺少收件人")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f'{cfg["from_name"]} <{cfg["from_email"]}>'
    msg["To"] = to
    msg.set_content(body)
    host = cfg["host"]
    port = int(cfg.get("port") or 587)
    if port == 465:
        client = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        client = smtplib.SMTP(host, port, timeout=20)
        if cfg.get("use_tls") is not False:
            client.starttls()
    try:
        client.login(cfg["username"], cfg["password"])
        client.send_message(msg)
    finally:
        try:
            client.quit()
        except Exception:
            pass


def test_mail(to: str = "") -> dict[str, Any]:
    dest = str(to or "").strip() or _mail_credentials().get("from_email") or ""
    send_mail(to=dest, subject="Mino 发信测试", body="这是一封测试信。配置可用。\n")
    return {"to": dest}


def _ai_root(user_id: str | None = None) -> dict[str, Any]:
    from mino_nexus.core.execution_user import require_user_id
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.settings import Settings
    uid = require_user_id(user_id)
    with session_scope() as db:
        row = db.get(Settings, f"ai-user:{uid}")
        return dict(row.payload or {}) if row and isinstance(row.payload, dict) else {}


def _save_ai(ai: dict[str, Any], user_id: str | None = None) -> None:
    from mino_nexus.core.execution_user import require_user_id
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.settings import Settings
    uid = require_user_id(user_id)
    with session_scope() as db:
        db.merge(Settings(id=f"ai-user:{uid}", payload=ai))


import threading
from functools import wraps
_ai_lock = threading.RLock()


def _ai_write(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _ai_lock:
            return fn(*args, **kwargs)
    return wrapped


def _provider_public(provider_id: str, raw: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    preset = next((p for p in AI_PROVIDER_PRESETS if p["id"] == provider_id), None)
    raw = raw if isinstance(raw, dict) else {}
    api_key = str(raw.get("api_key") or "").strip()
    from mino_nexus.ai.model_sdk import sdk_provider, catalog, model_info
    native = sdk_provider({"id": provider_id, **raw})
    models = catalog(native)
    model_options = [m["id"] for m in models["models"] if m["mode"] == "chat"]
    model = str(raw.get("model") or "").strip() or str((preset or {}).get("default_model") or "")
    configured = bool(api_key)
    enabled = raw.get("enabled", True) is not False
    case_execution_use = raw.get("case_execution_use") is True
    if not configured:
        enabled = False
        case_execution_use = False
    elif not enabled:
        case_execution_use = False
    validation = raw.get("_validation") or {"status": "unverified"}
    selected_info = model_info(native, model)
    selected_info["account_access"] = "verified_text_connection" if validation.get("status") == "verified" else "unverified"
    return {
        "id": provider_id,
        "name": raw.get("name") or (preset or {}).get("name") or provider_id,
        "api_type": str(raw.get("api_type") or (preset or {}).get("api_type") or "openai").strip(),
        "base_url": str(raw.get("base_url") or (preset or {}).get("base_url") or "").strip(),
        "model": model,
        "model_options": model_options,
        "sdk": models["sdk"], "sdk_version": models["sdk_version"], "sdk_provider": native,
        "model_info": selected_info,
        "embedding_model": str(raw.get("embedding_model") or ""),
        "model_parameters": raw.get("model_parameters") or {},
        "pricing": raw.get("pricing") or {},
        "embedding_pricing": raw.get("embedding_pricing") or {},
        "validation": raw.get("_validation") or {"status": "unverified"},
        "api_key_masked": _mask_secret(api_key),
        "configured": configured,
        "enabled": enabled,
        "case_execution_use": case_execution_use,
        "plan_compress_ratio": float(raw.get("plan_compress_ratio") or 3.0),
        "web_compress_ratio": float(raw.get("web_compress_ratio") or 2.0),
        "android_compress_ratio": float(raw.get("android_compress_ratio") or 1.0),
        "pc_compress_ratio": float(raw.get("pc_compress_ratio") or 1.0),
    }


def get_ai_usage(user_id: str | None = None) -> dict[str, Any]:
    usage = _ai_root(user_id).get("_usage") if isinstance(_ai_root(user_id).get("_usage"), dict) else {}
    return {
        "copilot_enabled": bool(usage.get("copilot_enabled", False)),
        "case_execution_enabled": bool(usage.get("case_execution_enabled", False)),
        "case_execution_provider_id": str(usage.get("case_execution_provider_id") or "").strip().lower(),
        "mode": usage.get("mode") or "local_first",
        "plan_compress_image": bool(usage.get("plan_compress_image", True)),
    }


def list_ai_providers(user_id: str | None = None) -> dict[str, Any]:
    from mino_nexus.ai.model_sdk import KEY_PROVIDERS

    ai = _ai_root(user_id)
    providers = [_provider_public(p["id"], ai.get(p["id"])) for p in AI_PROVIDER_PRESETS]
    custom_ids = [
        x for x in ai.keys()
        if x not in {p["id"] for p in AI_PROVIDER_PRESETS} and not str(x).startswith("_")
    ]
    for provider_id in sorted(custom_ids):
        row = ai.get(provider_id)
        if isinstance(row, dict):
            providers.append(_provider_public(provider_id, row))
    return {
        "providers": providers,
        "sdk_providers": list(KEY_PROVIDERS),
        "presets": [{**p, "model_options": next(row["model_options"] for row in providers if row["id"] == p["id"])} for p in AI_PROVIDER_PRESETS],
        "default_provider": str(ai.get("_default_provider") or "openai"),
        "usage": get_ai_usage(user_id),
    }


@_ai_write
def save_ai_provider(provider_id: str, payload: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
    pid = str(provider_id or "").strip().lower()
    if not pid or pid.startswith("_") or len(pid) > 128:
        raise ValueError("缺少模型 id")
    ai = _ai_root(user_id)
    row = ai.get(pid) if isinstance(ai.get(pid), dict) else {}
    if payload.get("clear_key"):
        row["api_key"] = ""
    elif str(payload.get("api_key") or "").strip():
        row["api_key"] = str(payload.get("api_key")).strip()
    for key in ("name", "api_type", "base_url", "model", "sdk_provider", "embedding_model"):
        if key in payload and payload.get(key) is not None:
            row[key] = str(payload.get(key) or "")
    from mino_nexus.ai.model_sdk import sdk_provider
    row["sdk_provider"] = sdk_provider({"id": pid, **row})
    if "model_parameters" in payload:
        params = payload.get("model_parameters") or {}
        allowed = {"temperature", "max_tokens", "top_p", "thinking"}
        if not isinstance(params, dict) or set(params) - allowed:
            raise ValueError("模型参数仅支持 temperature/max_tokens/top_p/thinking")
        import math
        for name, value in params.items():
            if name == "thinking":
                if value not in ({"type": "enabled"}, {"type": "disabled"}, {"type": "auto"}):
                    raise ValueError("thinking.type 须为 enabled/disabled/auto")
            elif not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError("模型参数须为有限数字")
            elif name == "temperature" and not 0 <= value <= 2 or name == "top_p" and not 0 <= value <= 1 or name == "max_tokens" and (value < 1 or int(value) != value):
                raise ValueError("模型参数超出范围")
        row["model_parameters"] = params
    for field in ("pricing", "embedding_pricing"):
        if field in payload:
            price = payload.get(field) or {}
            import math
            if not isinstance(price, dict) or set(price) - {"input_per_million_usd", "output_per_million_usd"}:
                raise ValueError("单价须为 USD / 百万 token")
            if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in price.values() if v is not None):
                raise ValueError("单价须为非负有限数字")
            row[field] = {k: v for k, v in price.items() if v is not None}
    if any(k in payload for k in ("api_key", "clear_key", "model", "base_url", "sdk_provider", "model_parameters", "embedding_model")):
        # 编辑配置使上次验证失效。不能把旧模型的成功状态挂到新模型上。
        row.pop("_validation", None)
    if "enabled" in payload:
        row["enabled"] = bool(payload.get("enabled"))
    if "case_execution_use" in payload:
        row["case_execution_use"] = bool(payload.get("case_execution_use"))
    if "plan_compress_ratio" in payload:
        row["plan_compress_ratio"] = float(payload.get("plan_compress_ratio") or 3)
    if "web_compress_ratio" in payload:
        row["web_compress_ratio"] = float(payload.get("web_compress_ratio") or 2)
    if "android_compress_ratio" in payload:
        row["android_compress_ratio"] = float(payload.get("android_compress_ratio") or 1)
    if "pc_compress_ratio" in payload:
        ratio = float(payload["pc_compress_ratio"])
        if not 1.0 <= ratio <= 10.0:
            raise ValueError("PC 压缩比必须在 1 到 10 之间")
        row["pc_compress_ratio"] = ratio
    ai[pid] = row
    if payload.get("set_default"):
        ai["_default_provider"] = pid
    _save_ai(ai, user_id)
    return _provider_public(pid, row)


@_ai_write
def record_ai_validation(provider_id: str, fingerprint: str, result: dict, user_id: str | None = None) -> bool:
    from mino_nexus.ai.model_sdk import config_fingerprint
    ai = _ai_root(user_id)
    current = get_ai_provider_credentials(provider_id, user_id)
    if config_fingerprint(current) != fingerprint:
        return False  # 验证期间配置已变化，拒绝写回旧结果
    row = ai.get(provider_id) if isinstance(ai.get(provider_id), dict) else {}
    row["_validation"] = result
    ai[provider_id] = row
    _save_ai(ai, user_id)
    return True


@_ai_write
def delete_ai_provider(provider_id: str, user_id: str | None = None) -> None:
    pid = str(provider_id or "").strip()
    ai = _ai_root(user_id)
    ai.pop(pid, None)
    _save_ai(ai, user_id)


@_ai_write
def save_ai_usage(payload: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
    ai = _ai_root(user_id)
    mode = payload.get("mode") or "local_first"
    if mode not in ("local_first", "ai_first"):
        mode = "local_first"
    ai["_usage"] = {
        "copilot_enabled": bool(payload.get("copilot_enabled", False)),
        "case_execution_enabled": bool(payload.get("case_execution_enabled", False)),
        "case_execution_provider_id": str(payload.get("case_execution_provider_id") or "").strip().lower(),
        "mode": mode,
        "plan_compress_image": bool(payload.get("plan_compress_image", True)),
    }
    _save_ai(ai, user_id)
    return get_ai_usage(user_id)


def get_ai_provider_credentials(provider_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    """给 LLM 调用层用，含明文 key，不要回给前端。"""
    from mino_nexus.core.execution_user import require_user_id
    uid = require_user_id(user_id)
    ai = _ai_root(uid)
    pid = str(provider_id or "").strip().lower() or str(ai.get("_default_provider") or "openai").strip().lower()
    raw = ai.get(pid) if isinstance(ai.get(pid), dict) else {}
    preset = next((p for p in AI_PROVIDER_PRESETS if p["id"] == pid), None)
    api_key = str(raw.get("api_key") or "").strip()
    from mino_nexus.ai.model_sdk import sdk_provider
    effective_base = str(raw.get("base_url") or (preset or {}).get("base_url") or "").rstrip("/")
    return {
        "owner_user_id": uid,
        "id": pid,
        "name": raw.get("name") or (preset or {}).get("name") or pid,
        "configured": bool(api_key),
        "enabled": raw.get("enabled", True) is not False if api_key else False,
        "api_key": api_key,
        "sdk_provider": sdk_provider({"id": pid, **raw}),
        "model_parameters": raw.get("model_parameters") or {},
        "pricing": raw.get("pricing") or {},
        "embedding_pricing": raw.get("embedding_pricing") or {},
        "custom_pricing_required": not preset or effective_base != str(preset.get("base_url") or "").rstrip("/"),
        "base_url": str(raw.get("base_url") or (preset or {}).get("base_url") or "").rstrip("/"),
        "model": str(raw.get("model") or (preset or {}).get("default_model") or "").strip(),
        "api_type": str(raw.get("api_type") or (preset or {}).get("api_type") or "openai").strip(),
        "case_execution_use": bool(raw.get("case_execution_use")) if api_key else False,
        "embedding_model": str(raw.get("embedding_model") or "").strip(),
        "plan_compress_ratio": float(raw.get("plan_compress_ratio") or 3.0),
        "web_compress_ratio": float(raw.get("web_compress_ratio") or 2.0),
        "android_compress_ratio": float(raw.get("android_compress_ratio") or 1.0),
        "pc_compress_ratio": float(raw.get("pc_compress_ratio") or 1.0),
    }


def find_case_execution_provider_id(user_id: str | None = None) -> str:
    usage = get_ai_usage(user_id)
    usage_pid = str(usage.get("case_execution_provider_id") or "").strip().lower()
    if usage_pid:
        cred = get_ai_provider_credentials(usage_pid, user_id)
        if cred.get("configured") and cred.get("enabled") and cred.get("case_execution_use"):
            return usage_pid
    ai = _ai_root(user_id)
    for pid, raw in ai.items():
        if str(pid).startswith("_") or not isinstance(raw, dict):
            continue
        cred = get_ai_provider_credentials(str(pid), user_id)
        if cred.get("configured") and cred.get("enabled") and cred.get("case_execution_use"):
            return str(pid)
    return usage_pid


def should_use_ai_planning(channel: str, provider_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    usage = get_ai_usage(user_id)
    normalized = (channel or "copilot").strip().lower()
    explicit = bool(str(provider_id or "").strip())
    if normalized in {"case", "case_execution", "regression", "feishu"}:
        scope_enabled = bool(usage.get("case_execution_enabled"))
    elif normalized == "copilot" and explicit:
        scope_enabled = True
    else:
        scope_enabled = bool(usage.get("copilot_enabled"))
    selected = str(provider_id or "").strip() or None
    if normalized in {"case", "case_execution", "regression", "feishu"} and not selected:
        selected = find_case_execution_provider_id(user_id) or None
    provider = get_ai_provider_credentials(selected, user_id)
    ok = bool(
        scope_enabled
        and provider.get("configured")
        and provider.get("enabled")
        and provider.get("case_execution_use")
    )
    reason = ""
    if not scope_enabled:
        reason = "未开启「使用大模型能力」（请到密钥配置 → 大模型 Key）"
    elif not provider.get("configured"):
        reason = f"还没有配置模型 Key：{provider.get('id')}"
    elif not provider.get("enabled"):
        reason = f"模型已关闭：{provider.get('id')}"
    elif not provider.get("case_execution_use"):
        reason = "未找到「可用 + 用例」的大模型（请到密钥配置 → 大模型 Key 设置）"
    public = {k: v for k, v in provider.items() if k != "api_key"}
    return {
        "enabled": ok,
        "reason": reason,
        "mode": usage.get("mode") or "local_first",
        "channel": normalized,
        "provider": public,
    }


def get_figma_settings() -> dict[str, Any]:
    raw = _root().get("figma") if isinstance(_root().get("figma"), dict) else {}
    token = str(raw.get("access_token") or "").strip()
    return {
        "access_token_masked": _mask_secret(token),
        "configured": bool(token),
        "default_file_url": str(raw.get("default_file_url") or "").strip(),
    }


def get_figma_access_token() -> str:
    raw = _root().get("figma") if isinstance(_root().get("figma"), dict) else {}
    return str(raw.get("access_token") or "").strip()


def save_figma_settings(
    *,
    access_token: str = "",
    clear_token: bool = False,
    default_file_url: Optional[str] = None,
) -> dict[str, Any]:
    root = _root()
    row = root.get("figma") if isinstance(root.get("figma"), dict) else {}
    if clear_token:
        row["access_token"] = ""
    elif str(access_token or "").strip():
        row["access_token"] = str(access_token).strip()
    if default_file_url is not None:
        row["default_file_url"] = str(default_file_url or "").strip()
    root["figma"] = row
    _save(root)
    return get_figma_settings()


def list_testing_knowledge(*, app_id: str = "", account_id: str = "", account_ident: str = "") -> list[dict[str, Any]]:
    from mino_nexus.services import knowledge_store as ks

    return ks.list_testing_knowledge(app_id=app_id, account_id=account_id, account_ident=account_ident)


def save_testing_knowledge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from mino_nexus.services import knowledge_store as ks

    return ks.save_testing_knowledge(items)


def upsert_knowledge_item(item: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services import knowledge_store as ks

    return ks.upsert_knowledge_item(item)


def delete_knowledge_item(kid: str) -> bool:
    from mino_nexus.services import knowledge_store as ks

    return ks.delete_knowledge_item(kid)


def review_knowledge_item(kid: str, *, action: str, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    from mino_nexus.services import knowledge_store as ks

    return ks.review_knowledge_item(kid, action=action, updates=updates)


_USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
KNOWLEDGE_JOB_DEFAULTS = {
    "capture_enabled": True,
    "review_enabled": True,
}


def _require_user_id(user_id: str) -> str:
    uid = str(user_id or "").strip()
    if not uid or not _USER_ID_RE.fullmatch(uid):
        raise ValueError("缺少登录用户")
    return uid


def _public_knowledge_jobs(raw, *, user_id: str) -> dict:
    row = raw if isinstance(raw, dict) else {}
    return {
        "user_id": user_id,
        "capture_enabled": row.get("capture_enabled", True) is not False,
        "review_enabled": row.get("review_enabled", True) is not False,
    }


def get_knowledge_job_settings(user_id: str = "", **_ignored) -> dict:
    """沉淀/机审开关。按 Nexus 登录用户隔离，不是 DUT 号池账号。"""
    uid = _require_user_id(user_id)
    users = _root().get("knowledge_job_users")
    row = users.get(uid) if isinstance(users, dict) else {}
    return _public_knowledge_jobs(row if isinstance(row, dict) else {}, user_id=uid)


def save_knowledge_job_settings(
    user_id: str = "",
    *,
    capture_enabled: bool = True,
    review_enabled: bool = True,
    **_ignored,
) -> dict:
    uid = _require_user_id(user_id)
    payload = {
        "capture_enabled": bool(capture_enabled),
        "review_enabled": bool(review_enabled),
    }
    root = _root()
    users = root.get("knowledge_job_users")
    if not isinstance(users, dict):
        users = {}
    users[uid] = payload
    root["knowledge_job_users"] = users
    _save(root)
    return _public_knowledge_jobs(payload, user_id=uid)


def get_case_api_settings() -> dict[str, str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.settings import Settings
    from mino_nexus.services.case_source_api import origin

    with session_scope() as db:
        row = db.get(Settings, "case-api-integration")
        payload = dict(row.payload or {}) if row else {}
    return {"base_url": origin(payload.get("base_url") or "http://192.168.22.9:3001/")}


def save_case_api_settings(base_url: str) -> dict[str, str]:
    from sqlalchemy import text
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.settings import Settings
    from mino_nexus.services.case_source_api import origin

    base_url = origin(base_url.strip())
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(Settings, "case-api-integration")
        if row is None:
            db.add(Settings(id="case-api-integration", payload={"base_url": base_url}))
        else:
            row.payload = {**(row.payload or {}), "base_url": base_url}
    return {"base_url": base_url}


def get_case_import_settings() -> dict[str, Any]:
    ci = _root().get("case_import") if isinstance(_root().get("case_import"), dict) else {}
    try:
        batch = int(ci.get("preview_batch_size") or 5)
    except (TypeError, ValueError):
        batch = 5
    batch = min(20, max(1, batch))
    return {
        "block_on_step_key_issues_default": bool(ci.get("block_on_step_key_issues_default")),
        "preview_batch_size": batch,
    }


def save_case_import_settings(payload: dict[str, Any]) -> dict[str, Any]:
    root = _root()
    ci = root.setdefault("case_import", {})
    if not isinstance(ci, dict):
        ci = {}
        root["case_import"] = ci
    if "block_on_step_key_issues_default" in payload:
        ci["block_on_step_key_issues_default"] = bool(payload.get("block_on_step_key_issues_default"))
    if "preview_batch_size" in payload:
        try:
            batch = int(payload.get("preview_batch_size") or 5)
        except (TypeError, ValueError):
            batch = 5
        ci["preview_batch_size"] = min(20, max(1, batch))
    _save(root)
    return get_case_import_settings()


def resolve_block_on_step_key_issues(explicit: bool | None) -> bool:
    if explicit is True:
        return True
    if explicit is False:
        return False
    return bool(get_case_import_settings().get("block_on_step_key_issues_default"))


def knowledge_capture_enabled(user_id: str = "", **_ignored) -> bool:
    return bool(get_knowledge_job_settings(user_id).get("capture_enabled", True))


def knowledge_review_enabled(user_id: str = "", **_ignored) -> bool:
    return bool(get_knowledge_job_settings(user_id).get("review_enabled", True))
