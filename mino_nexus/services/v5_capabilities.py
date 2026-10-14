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
