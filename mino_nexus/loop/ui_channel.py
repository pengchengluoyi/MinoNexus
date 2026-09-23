"""UI 几何/控件识别渠道：安卓 accessibility hierarchy vs Web DOM 节点。

Nexus 侧禁止用 `android.widget.EditText` 语义去匹配 Web DOM；调用方只传 channel，
具体规则在 `ui_sms_request`（安卓）与 `ui_dom`（Web）中分流。
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class UiChannel(str, Enum):
    ANDROID = "android"
    IOS = "ios"
    WEB = "web"


def ui_channel_from_ctx(ctx: Any | None) -> UiChannel:
    if ctx is None:
        return UiChannel.ANDROID
    from mino_nexus.runtime.run_context import is_web_slot

    sn = str(getattr(ctx, "sn", "") or "")
    plat = str(getattr(ctx, "platform", "") or "").strip().lower()
    if is_web_slot(sn, plat) or plat in ("web", "playwright"):
        return UiChannel.WEB
    if plat == "ios":
        return UiChannel.IOS
    return UiChannel.ANDROID


def ui_channel_label(channel: UiChannel | str) -> str:
    c = str(channel or UiChannel.ANDROID.value).strip().lower()
    if c == UiChannel.WEB.value:
        return "web_dom"
    if c == UiChannel.IOS.value:
        return "ios"
    return "android_hierarchy"
