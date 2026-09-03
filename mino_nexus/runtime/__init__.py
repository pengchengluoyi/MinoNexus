"""Runtime context：把 Scout 上报的 manifest 翻译成 RunContext。

与上游最大的差别：**这里不探测**。连通性来自 Scout 的 REGISTER / HEARTBEAT
（见 run_context.py 的模块注释）。

    from mino_nexus.runtime import build_run_context, RunContext

    ctx = build_run_context(sn=sn, run_id=run_id)
    brief = ctx.to_prompt_brief()    # 注入 prompt
    flags = ctx.connectivity_flags   # 喂给 catalog.registry 过滤菜单
"""
from __future__ import annotations

from mino_nexus.runtime.run_context import (  # noqa: F401
    RunContext,
    build_run_context,
    device_platform_kind,
    from_node,
    is_web_slot,
    stamp_app_version,
)
from mino_nexus.runtime.menu import (  # noqa: F401
    available_capabilities,
    available_menu_brief,
    capability_menu_diagnostics,
)
