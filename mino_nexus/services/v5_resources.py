"""Validate the existing ResourceClaim contract, rather than inventing a schema."""
from mino_nexus.services.case_resource_key_catalog import CATALOG_VERSION


def validate(resource):
    if not isinstance(resource, dict) or type(resource.get("version")) is not int or resource["version"] < CATALOG_VERSION:
        raise ValueError("resource_version_missing")
    if resource.get("platform") != "web":
        raise ValueError("unsupported_resource_platform")
    device = resource.get("device_app") or {}
    if device.get("required_session") not in {"logged_out", "guest"} or device.get("prep"):
        raise ValueError("unsupported_resource_precondition")
    baseline = resource.get("v5_baseline") or {}
    if not isinstance(baseline.get("control_id"), str) or not baseline["control_id"]:
        raise ValueError("baseline_missing")
    return {"control_id": baseline["control_id"], "expected": {"predicate": "not_visible"}}


def validate_input_ref(project_id, ref):
    from mino_nexus.services.project_store import find_test_account
    if not isinstance(ref, dict) or ref.get("field") not in {"username", "email", "phone", "password", "otp"} or not ref.get("account_id"):
        raise ValueError("secret_source_unavailable")
    account = find_test_account(project_id=project_id, account_id=ref["account_id"])
    if not account or account.get("locked") or not account.get(ref["field"]):
        raise ValueError("secret_source_unavailable")
