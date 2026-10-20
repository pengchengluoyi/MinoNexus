"""Strict primitives must be explicitly configured in the existing catalog."""
from mino_nexus.catalog import registry

CAPABILITIES = ("tap_target", "input_target", "read_web_target_facts")


def snapshot():
    result = {}
    for cid in CAPABILITIES:
        cap = registry.get_capability(cid)
        if not cap or not cap.enabled or cap.lifecycle != "active" or not registry.platform_ok(cap.platforms, "web"):
            raise ValueError("capability_unavailable")
        implementations = [impl for impl in cap.implementations
                           if impl.executor == "playwright" and impl.id == "web.strict.v1" and not impl.needs_vlm]
        if len(implementations) != 1:
            raise ValueError("capability_unavailable")
        result[cid] = {"id": cid, "platforms": cap.platforms, "params": cap.params,
                       "selected_impl": implementations[0].model_dump(), "enabled": cap.enabled,
                       "lifecycle": cap.lifecycle}
    return result


def register_current_web_capabilities() -> None:
    """Register the explicit Web primitive contract before loading the catalog."""
    from mino_nexus.core.database import session_scope
    from mino_nexus.catalog.system_registration import sync_system_capability

    control = {"name": "control", "type": "object", "required": True,
               "description": "Published control with control_id, revision and locator_spec"}
    timeout = {"name": "timeout_profile", "type": "object"}
    specs = (
        ("tap_target", "generic", "点击 Web 控件", ["do"], [control, timeout]),
        ("input_target", "generic", "输入 Web 控件", ["do"], [control, timeout,
            {"name": "payload", "type": "object", "required": True,
             "properties": {"text": {"type": "string"}}}]),
        ("read_web_target_facts", "check", "读取 Web 控件事实", ["prep", "do", "check"],
            [control, {"name": "requested_facts", "type": "array", "items": {"type": "string"}},
             {"name": "read_limit", "type": "integer", "minimum": 0, "maximum": 4096},
             {"name": "observation_contract", "type": "string", "enum": ["single_observation.v1"]}]),
    )
    with session_scope() as db:
        for cid, kind, title, phases, params in specs:
            sync_system_capability(db, cid=cid, kind=kind, title=title, platforms=["web"],
                fields={"event_kind": cid, "caller": "program", "phases": phases,
                        "params": params, "observe": "program", "channel_contract_version": 1,
                        "implementations": [{"id": "web.strict.v1", "executor": "playwright",
                            "runs_on": "scout", "platforms": ["web"], "needs_vlm": False,
                            "requires_caps": ["ui_native_input"], "low_level": {},
                            "params_contract": {"version": 1}, "result_contract": {"version": 1},
                            "device_hint_template": {"execution_mode": "v5"}}]})
