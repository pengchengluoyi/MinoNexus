"""步骤文本的纯判断：前置里有没有"清缓存"。"""
from __future__ import annotations

import re

_CLEAR_RE = re.compile(r"清缓存|清除缓存|清理缓存|清除应用缓存|清除.{0,6}缓存|clear.?cache", re.I)


def precondition_requires_clear_cache(precondition: str) -> bool:
    return bool(_CLEAR_RE.search(str(precondition or "")))
