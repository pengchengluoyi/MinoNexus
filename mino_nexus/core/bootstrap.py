"""空库不再灌能力目录。只搬旧知识进 knowledge_entries。"""
from __future__ import annotations

from mino_nexus.core.log import SLog

TAG = "Bootstrap"

def bootstrap() -> None:
    from mino_nexus.ai.prompt_repo import load_all

    load_all()
    from mino_nexus.core.database import SessionLocal

    db = SessionLocal()
    try:
        from mino_nexus.services.knowledge_store import migrate_legacy

        kn = migrate_legacy(db)
        if kn:
            SLog.i(TAG, f"knowledge migrated {kn} entries")
        from mino_nexus.catalog.recovery_seed import seed_recovery_rules

        rn = seed_recovery_rules()
        if rn:
            SLog.i(TAG, f"recovery rules seeded {rn}")
        from mino_nexus.catalog.resource_transition_seed import seed_resource_transition_rules

        trn = seed_resource_transition_rules()
        if trn:
            SLog.i(TAG, f"resource transition rules seeded {trn}")
        from mino_nexus.catalog.recovery_seed import upgrade_recovery_rules

        un = upgrade_recovery_rules()
        if un:
            SLog.i(TAG, f"recovery rules upgraded {un}")
        from mino_nexus.catalog.recovery_seed import upgrade_system_permission_recovery_rules

        pn = upgrade_system_permission_recovery_rules()
        if pn:
            SLog.i(TAG, f"system permission recovery rules upgraded {pn}")
        from mino_nexus.catalog.recovery_seed import upgrade_system_media_picker_recovery_rules

        mn = upgrade_system_media_picker_recovery_rules()
        if mn:
            SLog.i(TAG, f"system media picker recovery rules upgraded {mn}")
        from mino_nexus.catalog.recovery_seed import upgrade_account_capabilities

        an = upgrade_account_capabilities()
        if an:
            SLog.i(TAG, f"account capabilities upgraded {an}")
        from mino_nexus.catalog.recovery_seed import upgrade_get_otp_capability

        ot = upgrade_get_otp_capability()
        if ot:
            SLog.i(TAG, f"get_otp capability upgraded {ot}")
        from mino_nexus.catalog.recovery_seed import upgrade_consent_ime_capabilities

        ci = upgrade_consent_ime_capabilities()
        if ci:
            SLog.i(TAG, f"consent/ime capabilities upgraded {ci}")
        from mino_nexus.catalog.flow_block_seed import seed_global_flow_blocks

        fb = seed_global_flow_blocks()
        if fb:
            SLog.i(TAG, f"global flow blocks seeded {fb}")
        from mino_nexus.services.flow_block_key_ref_backfill import backfill_flow_block_key_refs

        bk = backfill_flow_block_key_refs()
        if bk:
            SLog.i(TAG, f"flow block key_ref backfilled {bk}")
        from mino_nexus.catalog.recovery_seed import upgrade_check_run_env_capability

        cn = upgrade_check_run_env_capability()
        if cn:
            SLog.i(TAG, f"check_run_env capability upgraded {cn}")
        from mino_nexus.catalog.recovery_seed import upgrade_fsm_navigate_capability

        fn = upgrade_fsm_navigate_capability()
        if fn:
            SLog.i(TAG, f"fsm_navigate capability upgraded {fn}")
        from mino_nexus.catalog.recovery_seed import upgrade_package_param_capabilities

        pp = upgrade_package_param_capabilities()
        if pp:
            SLog.i(TAG, f"package param capabilities upgraded {pp}")
        from mino_nexus.catalog.recovery_seed import upgrade_capability_empty_params

        sp = upgrade_capability_empty_params()
        if sp:
            SLog.i(TAG, f"capability empty params upgraded {sp}")
        # guard.* entries now require explicit recovery policy contracts.
        # Do not recreate obsolete base rows on a fresh database. Runtime
        # guard advice retains its defaults and existing stored overrides.
        from mino_nexus.loop.login_state_probe import ensure_read_web_auth_capability

        if ensure_read_web_auth_capability():
            SLog.i(TAG, "read_web_auth capability seeded")
        from mino_nexus.catalog.skill_channel import upgrade_skill_channel_layout

        if upgrade_skill_channel_layout():
            SLog.i(TAG, "skill catalog split into generic/check and base")
        db.commit()
        from mino_nexus.catalog.development_seed import sync_development_capability
        sync_development_capability()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
