from __future__ import annotations

from typing import Any


def identity_channel(channel: str) -> str:
    value = str(channel or "").removeprefix("langbot:")
    return {"lark": "feishu", "wecombot": "wecom", "weixin": "wechat"}.get(value, value)


from mino_nexus.platform.im_channels import normalize_im_channels  # noqa: F401,E402
