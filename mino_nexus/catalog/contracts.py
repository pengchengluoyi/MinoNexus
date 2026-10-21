"""Shared persisted capability contract for catalog writes and reads."""
def validate_capability_payload(payload, *, platforms, lifecycle):
    from mino_nexus.core.platform_contract import platform_set
    from mino_nexus.catalog.tool_schema import params_schema_for
    if not isinstance(payload,dict): raise ValueError("catalog_payload_required")
    caller, phases=payload.get("caller"),payload.get("phases")
    if caller not in {"case","program","system","base"}: raise ValueError("catalog_caller_required")
    allowed_phases = {"prep", "do", "check", "cleanup"} if caller == "system" else {"prep", "do", "check"}
    if not isinstance(phases,list) or any(p not in allowed_phases for p in phases) or len(phases)!=len(set(phases)):
        raise ValueError("catalog_phases_invalid")
    if caller in {"case","program"} and not phases: raise ValueError("catalog_phases_required")
    if "parameters" in payload: raise ValueError("catalog_parameters_not_storage")
    params_schema_for("",payload.get("params"))
    platform_set(platforms)
    if lifecycle not in {"active","draft","deprecated"}: raise ValueError("catalog_lifecycle_invalid")
    return caller,phases
