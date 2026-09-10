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
        from mino_nexus.catalog.recovery_seed import upgrade_account_capabilities

        an = upgrade_account_capabilities()
        if an:
            SLog.i(TAG, f"account capabilities upgraded {an}")
        from mino_nexus.services.job_store import upgrade_jobs_prompt_version, upgrade_jobs_strip_when

        jn = upgrade_jobs_strip_when()
        if jn:
            SLog.i(TAG, f"llm_jobs stripped when blocks {jn}")
        pv = upgrade_jobs_prompt_version()
        if pv:
            SLog.i(TAG, f"llm_jobs prompt_version migrated {pv}")
        from mino_nexus.ai.job_upgrades import upgrade_agent_decide_to_v6

        v6 = upgrade_agent_decide_to_v6()
        if v6:
            SLog.i(TAG, "agent-decide upgraded to prompt v6")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
