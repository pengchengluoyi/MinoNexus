"""同步壳：把协程送到主事件循环执行。

从 `loop/router_proxy.py` 抽出——业务模块（用例源传输、节点插件调用）只需要
"同步等一个协程"，不该为此 import 整个 Scout 网关。
"""
from __future__ import annotations

import asyncio
from typing import Optional

_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def run_sync(coro):
    """同步壳。agent_loop 在后台线程；Scout WS 在主 loop —— 必须桥接。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        main = _MAIN_LOOP
        if main is not None and main.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, main)
            return fut.result()
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError(
        "当前线程已有事件循环：请改用 dispatch_async / observe_async，"
        "不要在协程里调用同步壳"
    )
