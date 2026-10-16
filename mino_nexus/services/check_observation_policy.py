"""Reviewed instantaneous absence checks; no temporal monitoring or text guessing."""
from copy import deepcopy

CONTRACT = "single_observation.v1"

def has_absence(expected):
    if not isinstance(expected, dict):
        return False
    return expected.get("predicate") == "not_visible" or any(
        has_absence(child) for key in ("all", "any") for child in expected.get(key, []))

def freeze_policy(expected, wait):
    policy = deepcopy(wait or {"timeout_ms": 3000, "max_reads": 3})
    if has_absence(expected):
        policy["max_reads"] = 1
        policy["observation_contract"] = CONTRACT
    return policy

def validate_frozen(check):
    if has_absence(check.get("expected")):
        policy = check.get("wait_policy") or {}
        if policy.get("observation_contract") != CONTRACT or type(policy.get("max_reads")) is not int or policy["max_reads"] != 1:
            raise ValueError("single_observation_republish_required")

def source_ready(check, facts):
    return not has_absence(check.get("expected")) or (
        facts.get("observation_contract") == CONTRACT and facts.get("page_ready") is True)
