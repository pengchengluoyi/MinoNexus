"""Read-only identity for wheel/containers; deployments are managed externally."""

def identity() -> dict:
    from mino_nexus.core.build_info import identity as build_identity
    from mino_nexus.core.runtime_mode import is_development
    return {**build_identity(), "health_contract_version": 1,
            "deployment_managed": False, "deployment_kind": "wheel",
            "environment": "development" if is_development() else "production"}
