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
        from mino_nexus.services.skill_store import (
            upgrade_run_case_sop_guards,
            upgrade_run_case_sop_vision_v1,
        )

        sg = upgrade_run_case_sop_guards()
        if sg:
            SLog.i(TAG, "run-case sop prep guards upgraded")
        sv = upgrade_run_case_sop_vision_v1()
        if sv:
            SLog.i(TAG, "run-case sop vision orchestration upgraded")
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
            upgrade_agent_decide_to_v16,
            upgrade_agent_decide_to_v17,
            upgrade_agent_decide_to_v18,
            upgrade_agent_decide_to_v19,
            upgrade_agent_decide_to_v20,
            upgrade_agent_decide_to_v21,
            upgrade_agent_decide_to_v22,
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
        v16 = upgrade_agent_decide_to_v16()
        if v16:
            SLog.i(TAG, "agent-decide upgraded to prompt v16")
        v17 = upgrade_agent_decide_to_v17()
        if v17:
            SLog.i(TAG, "agent-decide upgraded to prompt v17")
        v18 = upgrade_agent_decide_to_v18()
        if v18:
            SLog.i(TAG, "agent-decide upgraded to prompt v18")
        v19 = upgrade_agent_decide_to_v19()
        if v19:
            SLog.i(TAG, "agent-decide upgraded to prompt v19")
        v20 = upgrade_agent_decide_to_v20()
        if v20:
            SLog.i(TAG, "agent-decide upgraded to prompt v20")
        v21 = upgrade_agent_decide_to_v21()
        if v21:
            SLog.i(TAG, "agent-decide upgraded to prompt v21")
        v22 = upgrade_agent_decide_to_v22()
        if v22:
            SLog.i(TAG, "agent-decide upgraded to prompt v22")
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

        from mino_nexus.ai.job_upgrades import (
            ensure_agent_vision_assert_job,
            ensure_agent_vision_exec_job,
            ensure_agent_vision_plan_job,
            upgrade_agent_decide_to_v23,
            upgrade_agent_decide_to_v24,
            upgrade_agent_vision_plan_to_v2,
            upgrade_agent_vision_plan_to_v3,
            upgrade_agent_vision_plan_to_v4,
            upgrade_agent_vision_plan_to_v5,
            upgrade_agent_vision_plan_to_v6,
            upgrade_agent_vision_plan_to_v7,
            upgrade_agent_vision_plan_to_v8,
            upgrade_agent_vision_plan_to_v9,
            upgrade_agent_vision_plan_to_v10,
            upgrade_agent_vision_plan_to_v11,
            upgrade_agent_vision_exec_to_v2,
            upgrade_agent_vision_exec_to_v3,
            upgrade_agent_vision_exec_to_v4,
            upgrade_vision_assert_language_neutral,
        )

        vp = ensure_agent_vision_plan_job()
        if vp:
            SLog.i(TAG, "agent-vision-plan job seeded")
        ve = ensure_agent_vision_exec_job()
        if ve:
            SLog.i(TAG, "agent-vision-exec job seeded")
        va = ensure_agent_vision_assert_job()
        if va:
            SLog.i(TAG, "agent-vision-assert job seeded")
        va_lang = upgrade_vision_assert_language_neutral()
        if va_lang:
            SLog.i(TAG, "vision assert prompt ignores UI language vs expected wording")
        v23 = upgrade_agent_decide_to_v23()
        if v23:
            SLog.i(TAG, "agent-decide upgraded to prompt v23")
        v24 = upgrade_agent_decide_to_v24()
        if v24:
            SLog.i(TAG, "agent-decide upgraded to prompt v24 no milestone_updates")
        vp2 = upgrade_agent_vision_plan_to_v2()
        if vp2:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v2 prep program plan")
        vp3 = upgrade_agent_vision_plan_to_v3()
        if vp3:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v3 do/check program plan")
        vp4 = upgrade_agent_vision_plan_to_v4()
        if vp4:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v4 success_criteria context")
        ve2 = upgrade_agent_vision_exec_to_v2()
        if ve2:
            SLog.i(TAG, "agent-vision-exec upgraded to prompt v2 standalone executor")
        vp5 = upgrade_agent_vision_plan_to_v5()
        if vp5:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v5 milestones-only")
        ve3 = upgrade_agent_vision_exec_to_v3()
        if ve3:
            SLog.i(TAG, "agent-vision-exec upgraded to prompt v3 milestones-only")
        vp6 = upgrade_agent_vision_plan_to_v6()
        if vp6:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v6 plan-exec FSM")
        vp7 = upgrade_agent_vision_plan_to_v7()
        if vp7:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v7 no plan milestone_updates")
        vp8 = upgrade_agent_vision_plan_to_v8()
        if vp8:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v8 exit_allowed")
        vp9 = upgrade_agent_vision_plan_to_v9()
        if vp9:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v9 registered caps")
        vp10 = upgrade_agent_vision_plan_to_v10()
        if vp10:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v10 exit_allowed restored")
        vp11 = upgrade_agent_vision_plan_to_v11()
        if vp11:
            SLog.i(TAG, "agent-vision-plan upgraded to prompt v11 no invented clicks")
        from mino_nexus.loop.login_state_probe import ensure_read_web_auth_capability

        if ensure_read_web_auth_capability():
            SLog.i(TAG, "read_web_auth capability seeded")
        from mino_nexus.catalog.skill_channel import upgrade_skill_channel_layout

        if upgrade_skill_channel_layout():
            SLog.i(TAG, "skill catalog split into generic/check and base")
        ve4 = upgrade_agent_vision_exec_to_v4()
        if ve4:
            SLog.i(TAG, "agent-vision-exec upgraded to prompt v4 in_progress focus")
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
