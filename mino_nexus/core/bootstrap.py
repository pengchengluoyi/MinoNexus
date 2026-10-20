"""Register current definitions only. No historical data migration or backfill."""
from mino_nexus.ai.prompt_repo import load_all


def bootstrap() -> None:
    load_all()
    from mino_nexus.catalog.recovery_seed import seed_recovery_rules, register_current_local_capabilities
    from mino_nexus.catalog.resource_transition_seed import seed_resource_transition_rules
    from mino_nexus.catalog.flow_block_seed import seed_global_flow_blocks
    from mino_nexus.loop.login_state_probe import ensure_read_web_auth_capability
    from mino_nexus.catalog.skill_channel import register_program_capabilities
    from mino_nexus.catalog.development_seed import sync_development_capability
    from mino_nexus.services.v5_capabilities import register_current_web_capabilities
    register_current_web_capabilities()
    seed_recovery_rules()
    seed_resource_transition_rules()
    seed_global_flow_blocks()
    register_current_local_capabilities()
    ensure_read_web_auth_capability()
    register_program_capabilities()
    sync_development_capability()
