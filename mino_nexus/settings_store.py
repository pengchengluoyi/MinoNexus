"""SMTP 与模型 Key。文件存储，LLM 循环尚未搬迁时也能让 Keys 页读写。"""
from __future__ import annotations

import re
import smtplib
import uuid
from email.message import EmailMessage
from typing import Any, Optional

from mino_nexus.json_store import load_json, save_json

_FILE = "settings.json"

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
        "default_model": "claude-3-5-sonnet-latest",
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
        "model_options": ["doubao-seed-2-0-lite-260215"],
    },
]


def _root() -> dict[str, Any]:
    raw = load_json(_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("mail", {})
    raw.setdefault("ai", {})
    raw.setdefault("layer_stack", {})
    raw.setdefault("figma", {})
    raw.setdefault("integrations", {})
    raw.setdefault("robots", {"items": []})
    raw.setdefault("knowledge", [])
    return raw


def _save(root: dict[str, Any]) -> None:
    save_json(_FILE, root)


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


def _ai_root() -> dict[str, Any]:
    root = _root()
    ai = root.get("ai")
    if not isinstance(ai, dict):
        ai = {}
        root["ai"] = ai
    return ai


def _provider_public(provider_id: str, raw: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    preset = next((p for p in AI_PROVIDER_PRESETS if p["id"] == provider_id), None)
    raw = raw if isinstance(raw, dict) else {}
    api_key = str(raw.get("api_key") or "").strip()
    model_options = list((preset or {}).get("model_options") or [])
    model = str(raw.get("model") or "").strip() or str((preset or {}).get("default_model") or "")
    configured = bool(api_key)
    enabled = raw.get("enabled", True) is not False
    case_execution_use = raw.get("case_execution_use") is True
    if not configured:
        enabled = False
        case_execution_use = False
    elif not enabled:
        case_execution_use = False
    return {
        "id": provider_id,
        "name": raw.get("name") or (preset or {}).get("name") or provider_id,
        "api_type": str(raw.get("api_type") or (preset or {}).get("api_type") or "openai").strip(),
        "base_url": str(raw.get("base_url") or (preset or {}).get("base_url") or "").strip(),
        "model": model,
        "model_options": model_options,
        "api_key_masked": _mask_secret(api_key),
        "configured": configured,
        "enabled": enabled,
        "case_execution_use": case_execution_use,
        "plan_compress_ratio": float(raw.get("plan_compress_ratio") or 3.0),
        "web_compress_ratio": float(raw.get("web_compress_ratio") or 2.0),
    }


def get_ai_usage() -> dict[str, Any]:
    usage = _ai_root().get("_usage") if isinstance(_ai_root().get("_usage"), dict) else {}
    return {
        "copilot_enabled": bool(usage.get("copilot_enabled", False)),
        "case_execution_enabled": bool(usage.get("case_execution_enabled", False)),
        "case_execution_provider_id": str(usage.get("case_execution_provider_id") or "").strip().lower(),
        "mode": usage.get("mode") or "local_first",
        "plan_compress_image": bool(usage.get("plan_compress_image", True)),
    }


def list_ai_providers() -> dict[str, Any]:
    ai = _ai_root()
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
        "presets": AI_PROVIDER_PRESETS,
        "default_provider": str(ai.get("_default_provider") or "openai"),
        "usage": get_ai_usage(),
    }


def save_ai_provider(provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    pid = str(provider_id or "").strip()
    if not pid:
        raise ValueError("缺少模型 id")
    root = _root()
    ai = root.setdefault("ai", {})
    row = ai.get(pid) if isinstance(ai.get(pid), dict) else {}
    if payload.get("clear_key"):
        row["api_key"] = ""
    elif str(payload.get("api_key") or "").strip():
        row["api_key"] = str(payload.get("api_key")).strip()
    for key in ("name", "api_type", "base_url", "model"):
        if key in payload and payload.get(key) is not None:
            row[key] = str(payload.get(key) or "")
    if "enabled" in payload:
        row["enabled"] = bool(payload.get("enabled"))
    if "case_execution_use" in payload:
        row["case_execution_use"] = bool(payload.get("case_execution_use"))
    if "plan_compress_ratio" in payload:
        row["plan_compress_ratio"] = float(payload.get("plan_compress_ratio") or 3)
    if "web_compress_ratio" in payload:
        row["web_compress_ratio"] = float(payload.get("web_compress_ratio") or 2)
    ai[pid] = row
    if payload.get("set_default"):
        ai["_default_provider"] = pid
    root["ai"] = ai
    _save(root)
    return _provider_public(pid, row)


def delete_ai_provider(provider_id: str) -> None:
    pid = str(provider_id or "").strip()
    root = _root()
    ai = root.get("ai") if isinstance(root.get("ai"), dict) else {}
    ai.pop(pid, None)
    root["ai"] = ai
    _save(root)


def save_ai_usage(payload: dict[str, Any]) -> dict[str, Any]:
    root = _root()
    ai = root.setdefault("ai", {})
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
    root["ai"] = ai
    _save(root)
    return get_ai_usage()


def _role_prompts_root() -> dict[str, str]:
    ai = _ai_root()
    raw = ai.get("_role_prompts")
    if not isinstance(raw, dict):
        raw = {}
        ai["_role_prompts"] = raw
    return raw


def get_role_prompt_override(role_id: str) -> str:
    rid = str(role_id or "").strip()
    if not rid:
        return ""
    return str(_role_prompts_root().get(rid) or "").strip()


def save_role_prompt(role_id: str, *, system_prompt: str = "", reset: bool = False) -> dict[str, Any]:
    from mino_nexus.ai.roles_catalog import get_role

    rid = str(role_id or "").strip()
    role = get_role(rid)
    if not role:
        raise ValueError(f"未知角色：{rid}")
    if not role.get("editable"):
        raise ValueError("这个角色的 prompt 不能在设置里改")
    root = _root()
    ai = root.setdefault("ai", {})
    store = ai.get("_role_prompts") if isinstance(ai.get("_role_prompts"), dict) else {}
    if reset:
        store.pop(rid, None)
    else:
        text = str(system_prompt or "").strip()
        if not text:
            raise ValueError("prompt 不能为空")
        store[rid] = text
    ai["_role_prompts"] = store
    root["ai"] = ai
    _save(root)
    row = get_role(rid)
    return row or {}


def get_ai_provider_credentials(provider_id: str | None = None) -> dict[str, Any]:
    """给 LLM 调用层用，含明文 key，不要回给前端。"""
    ai = _ai_root()
    pid = str(provider_id or "").strip().lower() or str(ai.get("_default_provider") or "openai").strip().lower()
    raw = ai.get(pid) if isinstance(ai.get(pid), dict) else {}
    preset = next((p for p in AI_PROVIDER_PRESETS if p["id"] == pid), None)
    api_key = str(raw.get("api_key") or "").strip()
    return {
        "id": pid,
        "name": raw.get("name") or (preset or {}).get("name") or pid,
        "configured": bool(api_key),
        "enabled": raw.get("enabled", True) is not False if api_key else False,
        "api_key": api_key,
        "base_url": str(raw.get("base_url") or (preset or {}).get("base_url") or "").rstrip("/"),
        "model": str(raw.get("model") or (preset or {}).get("default_model") or "").strip(),
        "api_type": str(raw.get("api_type") or (preset or {}).get("api_type") or "openai").strip(),
        "case_execution_use": bool(raw.get("case_execution_use")) if api_key else False,
        "plan_compress_ratio": float(raw.get("plan_compress_ratio") or 3.0),
        "web_compress_ratio": float(raw.get("web_compress_ratio") or 2.0),
    }


def find_case_execution_provider_id() -> str:
    usage = get_ai_usage()
    usage_pid = str(usage.get("case_execution_provider_id") or "").strip().lower()
    if usage_pid:
        cred = get_ai_provider_credentials(usage_pid)
        if cred.get("configured") and cred.get("enabled") and cred.get("case_execution_use"):
            return usage_pid
    ai = _ai_root()
    for pid, raw in ai.items():
        if str(pid).startswith("_") or not isinstance(raw, dict):
            continue
        cred = get_ai_provider_credentials(str(pid))
        if cred.get("configured") and cred.get("enabled") and cred.get("case_execution_use"):
            return str(pid)
    return usage_pid


def should_use_ai_planning(channel: str, provider_id: str | None = None) -> dict[str, Any]:
    usage = get_ai_usage()
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
        selected = find_case_execution_provider_id() or None
    provider = get_ai_provider_credentials(selected)
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


def get_layer_stack() -> dict[str, Any]:
    raw = _root().get("layer_stack")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "skill_drivers": raw.get("skill_drivers") or {},
        "role_skills": raw.get("role_skills") or {},
        "trigger_roles": raw.get("trigger_roles") or {},
        "trigger_skills": raw.get("trigger_skills") or {},
        "skills": raw.get("skills") or [],
        "roles": raw.get("roles") or [],
        "skill_categories": raw.get("skill_categories") or [],
        "drivers": raw.get("drivers") or [],
        "triggers": raw.get("triggers") or [],
    }


def save_layer_stack(payload: dict[str, Any], *, reset: bool = False) -> dict[str, Any]:
    root = _root()
    if reset:
        root["layer_stack"] = {}
        _save(root)
        return get_layer_stack()
    cur = get_layer_stack()
    for key in (
        "skill_drivers", "role_skills", "trigger_roles", "trigger_skills",
        "skills", "roles", "skill_categories", "drivers", "triggers",
    ):
        if key in payload and payload[key] is not None:
            cur[key] = payload[key]
    root["layer_stack"] = cur
    _save(root)
    return get_layer_stack()


def _normalize_knowledge(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    extra = {
        k: v for k, v in raw.items()
        if k not in {
            "score", "match_pct", "used", "skip_reason",
            "id", "title", "content", "category", "tags", "app_ids", "enabled",
            "source", "review_status", "account_id", "account_ident",
        } and v not in (None, "", [], {})
    }
    src = str(raw.get("source") or "").strip().lower() or "manual"
    st = str(raw.get("review_status") or "").strip().lower() or "approved"
    account_id = str(raw.get("account_id") or "").strip()
    account_ident = str(raw.get("account_ident") or "").strip()
    return {
        **extra,
        "id": str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12],
        "title": title,
        "content": str(raw.get("content") or "").strip(),
        "category": str(raw.get("category") or "").strip() or "其他",
        "tags": [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()],
        "app_ids": [str(a).strip() for a in (raw.get("app_ids") or []) if str(a).strip()],
        "enabled": raw.get("enabled", True) is not False,
        "source": src,
        "review_status": st if st in {"approved", "pending", "rejected"} else "approved",
        "account_id": account_id,
        "account_ident": account_ident,
    }


def list_testing_knowledge(*, app_id: str = "", account_id: str = "", account_ident: str = "") -> list[dict[str, Any]]:
    items = _root().get("knowledge")
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    want = str(app_id or "").strip()
    want_acc = str(account_id or "").strip()
    want_ident = str(account_ident or "").strip()
    for raw in items:
        row = _normalize_knowledge(raw) if isinstance(raw, dict) else None
        if not row:
            continue
        if want and row["app_ids"] and want not in row["app_ids"]:
            continue
        row_acc = str(row.get("account_id") or "").strip()
        row_ident = str(row.get("account_ident") or "").strip()
        # 未打账号的旧条目继续可见，避免导入知识被藏起来
        if (want_acc or want_ident) and (row_acc or row_ident):
            if want_acc and row_acc and row_acc != want_acc:
                continue
            if want_ident and row_ident and row_ident != want_ident:
                continue
            if want_acc and not row_acc and row_ident and want_ident and row_ident != want_ident:
                continue
        out.append(row)
    return out


def save_testing_knowledge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for raw in items or []:
        row = _normalize_knowledge(raw)
        if row:
            cleaned.append(row)
    root = _root()
    root["knowledge"] = cleaned
    _save(root)
    return cleaned


def upsert_knowledge_item(item: dict[str, Any]) -> dict[str, Any]:
    row = _normalize_knowledge(item)
    if not row:
        raise ValueError("知识条目缺少标题")
    items = list_testing_knowledge()
    kid = row["id"]
    for i, old in enumerate(items):
        if str(old.get("id")) == kid:
            items[i] = row
            break
    else:
        items.append(row)
    save_testing_knowledge(items)
    return row


def delete_knowledge_item(kid: str) -> bool:
    kid = str(kid or "").strip()
    if not kid:
        return False
    items = list_testing_knowledge()
    nxt = [x for x in items if str(x.get("id")) != kid]
    if len(nxt) == len(items):
        return False
    save_testing_knowledge(nxt)
    return True


def review_knowledge_item(kid: str, *, action: str, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    kid = str(kid or "").strip()
    act = str(action or "").strip().lower()
    if act not in {"approve", "reject"}:
        raise ValueError("action 必须是 approve 或 reject")
    existing = next((x for x in list_testing_knowledge() if str(x.get("id")) == kid), None)
    if not existing:
        raise KeyError(kid)
    if act == "reject":
        delete_knowledge_item(kid)
        return {**existing, "review_status": "rejected", "deleted": True}
    payload = dict(existing)
    if isinstance(updates, dict):
        payload.update({k: v for k, v in updates.items() if v is not None})
    payload["id"] = kid
    payload["review_status"] = "approved"
    return upsert_knowledge_item(payload)


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


def _knowledge_jobs_file(user_id: str) -> str:
    return f"users/{_require_user_id(user_id)}/knowledge_jobs.json"


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
    return _public_knowledge_jobs(load_json(_knowledge_jobs_file(uid), {}), user_id=uid)


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
    save_json(_knowledge_jobs_file(uid), payload)
    return _public_knowledge_jobs(payload, user_id=uid)


def knowledge_capture_enabled(user_id: str = "", **_ignored) -> bool:
    return bool(get_knowledge_job_settings(user_id).get("capture_enabled", True))


def knowledge_review_enabled(user_id: str = "", **_ignored) -> bool:
    return bool(get_knowledge_job_settings(user_id).get("review_enabled", True))
