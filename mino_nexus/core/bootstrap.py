"""空库不再灌能力目录。只搬旧知识进 knowledge_entries。"""
from __future__ import annotations

from mino_nexus.core.log import SLog

TAG = "Bootstrap"


def bootstrap() -> None:
    from mino_nexus.core.database import SessionLocal

    db = SessionLocal()
    try:
        from mino_nexus.services.knowledge_store import migrate_legacy

        kn = migrate_legacy(db)
        if kn:
            SLog.i(TAG, f"knowledge migrated {kn} entries")
        from mino_nexus.services.skill_store import seed_skills

        n = seed_skills()
        if n:
            SLog.i(TAG, f"skills seeded {n}")
        from mino_nexus.services.skill_store import upgrade_run_case_sop_guards

        sg = upgrade_run_case_sop_guards()
        if sg:
            SLog.i(TAG, "run-case sop prep guards upgraded")
        from mino_nexus.catalog.recovery_seed import seed_recovery_rules

        rn = seed_recovery_rules()
        if rn:
            SLog.i(TAG, f"recovery rules seeded {rn}")
        from mino_nexus.catalog.recovery_seed import upgrade_recovery_rules

        un = upgrade_recovery_rules()
        if un:
            SLog.i(TAG, f"recovery rules upgraded {un}")
        from mino_nexus.catalog.recovery_seed import upgrade_system_permission_recovery_rules

        pn = upgrade_system_permission_recovery_rules()
        if pn:
            SLog.i(TAG, f"system permission recovery rules upgraded {pn}")
        from mino_nexus.catalog.recovery_seed import upgrade_account_capabilities

        an = upgrade_account_capabilities()
        if an:
            SLog.i(TAG, f"account capabilities upgraded {an}")
        from mino_nexus.catalog.recovery_seed import upgrade_check_run_env_capability

        cn = upgrade_check_run_env_capability()
        if cn:
            SLog.i(TAG, f"check_run_env capability upgraded {cn}")
        from mino_nexus.catalog.recovery_seed import upgrade_fsm_navigate_capability

        fn = upgrade_fsm_navigate_capability()
        if fn:
            SLog.i(TAG, f"fsm_navigate capability upgraded {fn}")
        from mino_nexus.services.job_store import upgrade_jobs_prompt_version, upgrade_jobs_strip_when

        jn = upgrade_jobs_strip_when()
        if jn:
            SLog.i(TAG, f"llm_jobs stripped when blocks {jn}")
        pv = upgrade_jobs_prompt_version()
        if pv:
            SLog.i(TAG, f"llm_jobs prompt_version migrated {pv}")
        from mino_nexus.ai.job_upgrades import upgrade_agent_decide_to_v8, upgrade_assert_vision_to_v2

        v8 = upgrade_agent_decide_to_v8()
        if v8:
            SLog.i(TAG, "agent-decide upgraded to prompt v8")
        av2 = upgrade_assert_vision_to_v2()
        if av2:
            SLog.i(TAG, "assert-vision upgraded to prompt v2")
        from mino_nexus.ai.job_upgrades import ensure_nav_widget_state_job

        nw = ensure_nav_widget_state_job()
        if nw:
            SLog.i(TAG, "nav-widget-state job seeded")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
