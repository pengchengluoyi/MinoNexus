"""装配根：启动时登记当前定义。

为什么不在 `core/`：种子要 import `ai` / `catalog` / `loop` / `services`，
而 `core` 被所有层依赖。放在 core 里等于让底座反向依赖业务，整个仓库就成环。
这里只做"登记"，`core/database.py` 只保留一个 `register_bootstrap()` 钩子。

入口（`app.py` / `cli.py`）必须在任何 `ensure_db()` 之前 import 本模块。
"""
from __future__ import annotations

from mino_nexus import models  # noqa: F401 — 注册 ORM 表，ensure_db 建表依赖它
from mino_nexus.core.database import register_bootstrap


def bootstrap() -> None:
    """Register current definitions only. No historical data migration or backfill."""
    from mino_nexus.platform.llm.repo import load_all
    load_all()
    from mino_nexus.catalog.recovery_seed import seed_recovery_rules, register_current_local_capabilities
    from mino_nexus.catalog.resource_transition_seed import seed_resource_transition_rules
    from mino_nexus.catalog.flow_block_seed import seed_global_flow_blocks
    from mino_nexus.loop.login_state_probe import ensure_read_web_auth_capability
    from mino_nexus.catalog.skill_channel import register_program_capabilities
    from mino_nexus.catalog.development_seed import sync_development_capability
    from mino_nexus.casework.v5_capabilities import register_current_web_capabilities
    register_current_web_capabilities()
    seed_recovery_rules()
    seed_resource_transition_rules()
    seed_global_flow_blocks()
    register_current_local_capabilities()
    ensure_read_web_auth_capability()
    register_program_capabilities()
    sync_development_capability()


register_bootstrap(bootstrap)
