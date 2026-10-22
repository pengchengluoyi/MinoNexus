"""Confirmed observations are scoped overlays; manual pool facts stay independent."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from mino_nexus.platform.identity import require_user_id
from mino_nexus.models.account_fact_candidate import AccountFactCandidate

_scope = ContextVar("account_fact_scope", default=None)


@contextmanager
def fact_scope(ctx):
    scope = {"app_id": str(getattr(ctx, "app_id", "") or ""),
        "platform": str(getattr(ctx, "platform", "") or ""),
        "build": str(getattr(ctx, "app_version", "") or ""),
        "env": str(getattr(ctx, "env_profile", "") or ""), "owner": require_user_id()}
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def overlay(db, project_id, account_id, facets):
    scope = _scope.get()
    if scope is None or not all(scope.values()):
        return facets
    rows = db.query(AccountFactCandidate).filter_by(project_id=project_id, account_id=account_id,
        owner_user_id=scope["owner"], env=scope["env"], status="confirmed").order_by(
            AccountFactCandidate.created_at, AccountFactCandidate.candidate_id).all()
    result = dict(facets)
    latest = {}
    for row in rows:
        if any((row.evidence_context or {}).get(key) != scope[key] for key in ("app_id", "platform", "build")):
            continue
        # A later manual correction invalidates the observation's baseline.
        if any(facets.get(key, "unknown") != (row.before or {}).get(key, "unknown") for key in row.proposed):
            continue
        expires_at = (row.decision or {}).get("expires_at", 0)
        if (row.decision or {}).get("verification_kind") == "automatic":
            from mino_nexus.models.account_fact_candidate import AccountFactRule
            for proof in (row.decision or {}).get("evidence") or []:
                rule = db.get(AccountFactRule, proof.get("rule_id"))
                if rule is None or rule.definition.get("enabled") is not True or rule.definition.get("revision") != proof.get("rule_revision"):
                    expires_at = 0
        for key, value in row.proposed.items():
            latest[key] = (value, expires_at)
    for key, (value, expires_at) in latest.items():
        # Expiration of a newer correction must not resurrect an older observation.
        if expires_at > time.time():
            result[key] = value
    return result
