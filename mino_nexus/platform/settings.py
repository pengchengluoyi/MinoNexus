"""模型配置唯一读取入口，按执行账号转发 settings_store，无共享密钥回退。"""
from __future__ import annotations

import contextvars
from typing import Any, Optional

from mino_nexus.platform import settings_store
from mino_nexus.core.log import SLog

TAG = "Settings"


def default_provider_id() -> str:
    return str(settings_store.list_ai_providers().get("default_provider") or "openai").lower()


def get_ai_provider_credentials(provider_id: Optional[str] = None) -> dict[str, Any]:
    """只读取当前执行账号；不回退到全局、管理员或环境变量 Key。"""
    cred = dict(settings_store.get_ai_provider_credentials(provider_id))
    cred["source"] = "settings_store"
    return cred


def validate_provider_owner(provider: dict[str, Any]) -> None:
    from mino_nexus.platform.identity import require_user_id
    if str(provider.get("owner_user_id") or "") != require_user_id():
        raise PermissionError("模型配置不属于执行账号")


def _compress_ratio(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if 1.0 <= value <= 10.0:
        return value
    return default


def _provider_for_ratio(provider_id: Optional[str] = None) -> dict[str, Any]:
    pid = str(provider_id or "").strip() or find_case_execution_provider_id()
    return get_ai_provider_credentials(pid or None)


def get_ai_web_compress_ratio(provider_id: Optional[str] = None) -> float:
    """Web 采集压缩比；1.0=不压缩，默认 2.0。随 EXECUTE screenshot 下发给 Scout。"""
    raw = _provider_for_ratio(provider_id).get("web_compress_ratio")
    return _compress_ratio(raw, 2.0)


def get_ai_android_compress_ratio(provider_id: Optional[str] = None) -> float:
    """安卓采集压缩比；1.0=不压缩，默认 1.0。随 EXECUTE screenshot 下发给 Scout。"""
    raw = _provider_for_ratio(provider_id).get("android_compress_ratio")
    return _compress_ratio(raw, 1.0)


def get_ai_pc_compress_ratio(provider_id: Optional[str] = None) -> float:
    """PC 采集压缩比；1.0 保留原图尺寸，不叠加 Plan 压缩。"""
    raw = _provider_for_ratio(provider_id).get("pc_compress_ratio")
    return _compress_ratio(raw, 1.0)


def get_ai_plan_compress_ratio(provider_id: Optional[str] = None) -> float:
    """喂给大模型的统一压缩比；1.0=不缩小，默认 3.0。各渠道都转 JPEG。

    只用于采集端没有再压过的图。Web / 安卓的渠道压缩比已经作用在截图上时，不要再套这个值。
    """
    raw = _provider_for_ratio(provider_id).get("plan_compress_ratio")
    return _compress_ratio(raw, 3.0)


_channel_capture_ratio: contextvars.ContextVar[float] = contextvars.ContextVar(
    "mino_channel_capture_ratio", default=1.0
)
_channel_capture_authoritative: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mino_channel_capture_authoritative", default=False
)


def note_channel_capture_ratio(ratio: float, *, authoritative: bool = False) -> None:
    """记下这张截图在采集端用过的压缩比。调用方线程上记，给后面的喂模缩图用。"""
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        value = 1.0
    _channel_capture_ratio.set(value if value > 0 else 1.0)
    _channel_capture_authoritative.set(authoritative)


def llm_shrink_after_capture() -> float:
    """渠道压缩和 plan_compress_ratio 不叠加。

    采集已经按渠道压缩比缩小过（比 > 1）时，喂模只转 JPEG，不再除以 3。
    PC 的独立压缩设置始终优先，设为 1 时也不叠加 Plan 压缩。
    采集没缩（比 = 1）时，才用 plan_compress_ratio。
    """
    if _channel_capture_authoritative.get() or _channel_capture_ratio.get() > 1.0:
        return 1.0
    return get_ai_plan_compress_ratio()


def channel_screenshot_compress_ratio(platform: str, provider_id: Optional[str] = None) -> float:
    """采集端按 Web、安卓、PC 渠道读取各自的压缩比。"""
    plat = str(platform or "").strip().lower()
    if plat in ("web", "browser", "playwright"):
        return get_ai_web_compress_ratio(provider_id)
    if plat in ("android", "adb"):
        return get_ai_android_compress_ratio(provider_id)
    if plat in ("desktop", "pc", "macos", "windows"):
        return get_ai_pc_compress_ratio(provider_id)
    return 1.0


def find_case_execution_provider_id() -> str:
    return settings_store.find_case_execution_provider_id()


def should_use_ai_planning(channel: str, provider_id: Optional[str] = None) -> dict[str, Any]:
    return settings_store.should_use_ai_planning(channel, provider_id=provider_id)


def summary() -> dict[str, Any]:
    """公共健康状态不披露任何账号的配置。"""
    return {"source": "user_scoped", "providers": [p["id"] for p in settings_store.AI_PROVIDER_PRESETS]}


# ---------------- 未知属性透明转发 ----------------


def __getattr__(name: str) -> Any:
    """本模块没显式包装的东西，一律转发给 `settings_store`。

    为什么要这一层：本模块只需要对**受 env 影响的函数**做包装（provider 凭据、
    默认 provider、用例 provider、AI 闸门）。其余几十个设置项（邮件 /
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
