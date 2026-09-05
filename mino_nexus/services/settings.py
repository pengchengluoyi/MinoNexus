"""LLM provider 凭据的**唯一读取入口** —— 转发 `settings_store` + 支持环境变量覆盖。

## 为什么有两个 settings 模块

| 模块 | 角色 |
|---|---|
| `settings_store.py` | **唯一真源。** UI（`rSettings`）读写，落 `settings` 表。含 6 个 provider 预设、邮件、Figma、role prompt override、`case_execution_use` 等业务开关 |
| `settings.py`（本文件） | **只做两件事**：① 环境变量覆盖 ② 转发给 `settings_store`。**自己不存任何配置。** |

`llm_client` 与 `runtime/run_context` 只 import 本模块，于是"key 从哪来"这件事收在一处。

## 修正记录（重要）

搬迁初期本模块自己读 `ai_providers.json` + env，**和 UI 写的 settings 表是两套**，
后果是：在设置页填了 key，`llm_client` 却读不到，所有 LLM 调用报"未配置 provider"。

更隐蔽的第二层：`llm_client` 会检查 `provider["case_execution_use"]`（"这个 provider
允许用于跑用例"的开关），而当时本模块**根本不返回这个 key** → `.get()` 得到 None →
即使读对了文件也会被拒。

所以现在的铁律：**provider 字段的形状由 `settings_store.get_ai_provider_credentials`
定义，本模块不得自行拼装**。env 覆盖也必须在它的返回值上做叠加，不能另起一个 dict。
`tests/test_settings.py` 钉住了这一点。

## 环境变量（给 CI 和本地调试用，不经 UI）

    MINO_AI_PROVIDER   provider id，默认 openai
    MINO_AI_API_KEY    有它才触发覆盖
    MINO_AI_BASE_URL   缺省用 settings_store 预设里的
    MINO_AI_MODEL      缺省同上
"""
from __future__ import annotations

import os
from typing import Any, Optional

from mino_nexus.services import settings_store
from mino_nexus.core.log import SLog

TAG = "Settings"


def _env_override() -> tuple[str, dict[str, str]]:
    """返回 (provider_id, 要叠加的字段)。没配 MINO_AI_API_KEY 就返回空。"""
    key = os.environ.get("MINO_AI_API_KEY", "").strip()
    if not key:
        return "", {}
    pid = (os.environ.get("MINO_AI_PROVIDER") or "openai").strip().lower()
    patch = {"api_key": key}
    for env_name, field in (("MINO_AI_BASE_URL", "base_url"), ("MINO_AI_MODEL", "model")):
        val = os.environ.get(env_name, "").strip()
        if val:
            patch[field] = val
    return pid, patch


def default_provider_id() -> str:
    pid, _ = _env_override()
    if pid:
        return pid
    return str(settings_store.list_ai_providers().get("default_provider") or "openai").lower()


def get_ai_provider_credentials(provider_id: Optional[str] = None) -> dict[str, Any]:
    """含明文 key，**只给 LLM 调用层**，不要回给前端。

    形状完全由 `settings_store` 决定（含 `case_execution_use` / `api_type` /
    `plan_compress_ratio` 等 llm_client 会读的字段），本模块只叠加 env 覆盖。
    """
    env_pid, patch = _env_override()
    pid = str(provider_id or "").strip().lower() or env_pid or default_provider_id()

    cred = dict(settings_store.get_ai_provider_credentials(pid))

    if patch and pid == env_pid:
        cred.update(patch)
        # env 里配了 key 就是"我要用它跑"，把三个开关一并打开 ——
        # 否则 llm_client 的 configured / enabled / case_execution_use 三道检查过不去。
        cred["configured"] = True
        cred["enabled"] = True
        cred["case_execution_use"] = True
        if not cred.get("base_url"):
            SLog.w(TAG, f"env 覆盖了 {pid} 的 key，但没有 base_url —— 设 MINO_AI_BASE_URL")
        cred["source"] = "env"
    else:
        cred["source"] = "settings_store"

    return cred


def get_ai_web_compress_ratio(provider_id: Optional[str] = None) -> float:
    """Web 截图压缩比；1.0=不压缩，默认 2.0。随 EXECUTE screenshot 的 params 下发给 Scout（协议 §4.4）。"""
    raw = get_ai_provider_credentials(provider_id).get("web_compress_ratio")
    try:
        v = float(raw)
        return v if 1.0 <= v <= 8.0 else 2.0
    except (TypeError, ValueError):
        return 2.0


def find_case_execution_provider_id() -> str:
    """跑用例该用哪个 provider。env 覆盖优先，否则问 settings_store。"""
    env_pid, _ = _env_override()
    if env_pid:
        return env_pid
    return settings_store.find_case_execution_provider_id()


def should_use_ai_planning(channel: str, provider_id: Optional[str] = None) -> dict[str, Any]:
    """这条通道现在能不能用大模型。

    `settings_store` 的版本除了看 provider 本身，还看 `ai_usage` 里的
    「使用大模型能力」总开关（`case_execution_enabled` / `copilot_enabled`）——
    那是 UI 上的勾选。env 覆盖的语义是"我要用它跑"，所以**连总开关一起认为已开**，
    否则本地/CI 配了 key 仍然会被这道闸门拦住。
    """
    env_pid, patch = _env_override()
    gate = dict(settings_store.should_use_ai_planning(channel, provider_id=provider_id))
    if not patch:
        return gate

    pid = str(provider_id or "").strip().lower() or env_pid
    if pid != env_pid:
        return gate

    cred = get_ai_provider_credentials(pid)
    gate["enabled"] = True
    gate["reason"] = ""
    gate["provider"] = {k: v for k, v in cred.items() if k != "api_key"}
    gate["source"] = "env"
    return gate


def summary() -> dict[str, Any]:
    """给 /health 用，**不含明文 key**。"""
    listing = settings_store.list_ai_providers()
    env_pid, patch = _env_override()
    out: dict[str, Any] = {
        "default": default_provider_id(),
        "source": "env" if patch else "settings_store",
        "providers": {
            str(p.get("id")): {
                "configured": bool(p.get("configured")),
                "enabled": bool(p.get("enabled")),
                "case_execution_use": bool(p.get("case_execution_use")),
                "model": p.get("model") or "",
            }
            for p in listing.get("providers") or []
        },
    }
    if patch:
        row = out["providers"].setdefault(env_pid, {})
        row.update({"configured": True, "enabled": True, "case_execution_use": True,
                    "model": patch.get("model") or row.get("model") or "", "via": "env"})
    return out


# ---------------- 未知属性透明转发 ----------------


def __getattr__(name: str) -> Any:
    """本模块没显式包装的东西，一律转发给 `settings_store`。

    为什么要这一层：本模块只需要对**受 env 影响的函数**做包装（provider 凭据、
    默认 provider、用例 provider、AI 闸门）。其余几十个设置项（邮件 / Figma /
    layer_stack / role prompt / usage …）与 env 无关，逐个 re-export 只会漏。

    已经漏过一次：`llm_client` 调 `ss.should_use_ai_planning`，而本模块当时没包装，
    `AttributeError` 让 `/case-runner/run` 直接 500 —— 而且是 HTTP 契约检查发现的，
    不是单测。有了这个兜底，同类遗漏最坏也只是"没享受到 env 覆盖"，不会崩。

    显式包装的那几个在上面定义，会优先命中，不会走到这里。
    """
    if name.startswith("_"):
        raise AttributeError(name)
    try:
        return getattr(settings_store, name)
    except AttributeError:
        raise AttributeError(
            f"mino_nexus.settings 和 settings_store 都没有 {name!r}"
            "（settings.py 只做 env 覆盖 + 转发，真源是 settings_store，见 CLAUDE.md §8）"
        ) from None
