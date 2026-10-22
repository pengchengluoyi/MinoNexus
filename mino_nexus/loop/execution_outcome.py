"""Business terminal vocabulary; uncertain observations never become failures."""
TERMINALS = frozenset({'pass', 'product_fail', 'tool_blocked', 'env_blocked', 'unverified'})
_ENV = frozenset({'device_platform_invalid', 'device_channel_unavailable', 'app_build_mismatch',
    'app_build_unverified', 'account_lease_missing', 'login_identity_unverified',
    'prepare_clear_incomplete', 'prepare_launch_unverified', 'login_precondition_unmet',
    'environment_precondition_unmet', 'otp_environment_unavailable', 'platform_not_applicable',
    'prepare_steps_incomplete', 'precondition_coverage_missing', 'precondition_scene_mismatch'})
_UNVERIFIED = frozenset({'case_budget_exhausted', 'task_cancelled', 'observation_missing',
    'check_evidence_incomplete', 'case_checks_incomplete', 'write_outcome_unknown',
    'session_store_unavailable', 'execution_exception', 'model_gave_up',
    'action_result_unverified', 'login_submission_unverified', 'login_transition_observation_error',
    'runtime_incomplete', 'review_observation_missing', 'review_wait_exhausted'})
_UNVERIFIED = _UNVERIFIED | {'check_scope_unverified', 'check_scope_mismatch',
    'check_observation_channel_mismatch', 'required_operation_unverified', 'navigation_target_unresolved',
    'screenshot_missing', 'screenshot_decode_failed', 'screenshot_black', 'foreground_image_withheld',
    'model_image_withheld', 'case_step_budget_exhausted', 'login_program_incomplete',
    'milestone_plan_incomplete', 'action_result_unverified'}


def terminal_for_reason(reason_code, status='tool_blocked'):
    if reason_code in {'required_assertion_failed', 'login_page_unchanged'}:
        return 'product_fail'
    if reason_code in _ENV:
        return 'env_blocked'
    if reason_code in _UNVERIFIED or reason_code.startswith('model_'):
        return 'unverified'
    if status in TERMINALS - {'pass'}:
        return status
    return 'tool_blocked'


def finish_resources(ctx, *, attempt=None, storage_failed=False):
    """All case exits converge here after cleanup and durable finalization.

    Never release unknown writes or an outcome whose recording failed. The
    outer task owner retains its idempotent pre-admission fallback.
    """
    from mino_nexus.casework.run_write_safety import protected
    run_id, sn = ctx.run_id, ctx.sn
    if storage_failed or protected(run_id, sn=sn):
        return False
    from mino_nexus.resources.accounts.lease import release_ctx_lease
    release_ctx_lease(ctx)
    if attempt:
        from mino_nexus.casework.execution_resources import release
        from mino_nexus.resources.device_resource_lease import release_device_lease
        release(run_id, sn, attempt_id=attempt['attempt_id'])
        release_device_lease(sn, ctx.target_package, run_id, attempt_id=attempt['attempt_id'])
    return True
