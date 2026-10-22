"""设备身份谓词：纯字符串判断，无 I/O，供各层共用。

这些函数原先住在 `runtime/run_context.py`，被 `loop/ui_channel.py` 反向依赖，
构成 `contract -> runtime -> services` 的潜在环。下沉到契约层后，任何层都可以用，
且不引入对运行时装配的依赖。
"""
from __future__ import annotations


def is_web_slot(sn: str = "", platform: str = "") -> bool:
    """`web` + [a-z0-9]+ 的 Playwright 槽位；`web-local` 这类遗留名不算。"""
    s = str(sn or "")
    valid = s.startswith("web") and len(s) > 3 and s[3:].isascii() and s[3:].isalnum() and s == s.lower()
    return valid and (not platform or platform == "web")


def is_per_node_web_sn(sn: str = "") -> bool:
    """`web` + [a-z0-9]+ —— 升级后的 Playwright 槽位，不是 `web-local`。"""
    s = str(sn or "").strip().lower()
    return s.startswith("web") and len(s) > 3 and s[3:].isalnum()
