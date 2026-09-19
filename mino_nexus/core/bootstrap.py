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
        from mino_nexus.services.job_store import upgrade_jobs_prompt_version, upgrade_jobs_strip_when

        jn = upgrade_jobs_strip_when()
        if jn:
            SLog.i(TAG, f"llm_jobs stripped when blocks {jn}")
        pv = upgrade_jobs_prompt_version()
        if pv:
            SLog.i(TAG, f"llm_jobs prompt_version migrated {pv}")
        from mino_nexus.ai.job_upgrades import (
            upgrade_agent_decide_to_v8,
            upgrade_agent_decide_to_v9,
            upgrade_agent_decide_to_v10,
            upgrade_agent_decide_to_v11,
            upgrade_agent_decide_to_v12,
            upgrade_agent_decide_to_v13,
            upgrade_agent_decide_to_v14,
            upgrade_agent_decide_to_v15,
            upgrade_assert_vision_to_v2,
            upgrade_assert_vision_to_v3,
            upgrade_inspect_session_to_v2,
            upgrade_inspect_session_to_v3,
        )

        v8 = upgrade_agent_decide_to_v8()
        if v8:
            SLog.i(TAG, "agent-decide upgraded to prompt v8")
        v9 = upgrade_agent_decide_to_v9()
        if v9:
            SLog.i(TAG, "agent-decide upgraded to prompt v9")
        v10 = upgrade_agent_decide_to_v10()
        if v10:
            SLog.i(TAG, "agent-decide upgraded to prompt v10")
        v11 = upgrade_agent_decide_to_v11()
        if v11:
            SLog.i(TAG, "agent-decide upgraded to prompt v11")
        v12 = upgrade_agent_decide_to_v12()
        if v12:
            SLog.i(TAG, "agent-decide upgraded to prompt v12")
        v13 = upgrade_agent_decide_to_v13()
        if v13:
            SLog.i(TAG, "agent-decide upgraded to prompt v13")
        v14 = upgrade_agent_decide_to_v14()
        if v14:
            SLog.i(TAG, "agent-decide upgraded to prompt v14")
        v15 = upgrade_agent_decide_to_v15()
        if v15:
            SLog.i(TAG, "agent-decide upgraded to prompt v15")
        av2 = upgrade_assert_vision_to_v2()
        if av2:
            SLog.i(TAG, "assert-vision upgraded to prompt v2")
        av3 = upgrade_assert_vision_to_v3()
        if av3:
            SLog.i(TAG, "assert-vision upgraded to prompt v3")
        is2 = upgrade_inspect_session_to_v2()
        if is2:
            SLog.i(TAG, "inspect-session upgraded to prompt v2")
        is3 = upgrade_inspect_session_to_v3()
        if is3:
            SLog.i(TAG, "inspect-session upgraded to prompt v3")
        from mino_nexus.ai.job_upgrades import ensure_nav_widget_state_job

        nw = ensure_nav_widget_state_job()
        if nw:
            SLog.i(TAG, "nav-widget-state job seeded")
        from mino_nexus.ai.job_upgrades import ensure_nav_atlas_morph_job

        nm = ensure_nav_atlas_morph_job()
        if nm:
            SLog.i(TAG, "nav-atlas-morph job seeded")
        from mino_nexus.ai.job_upgrades import ensure_account_facet_commit_job

        af = ensure_account_facet_commit_job()
        if af:
            SLog.i(TAG, "account-facet-commit job seeded")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
