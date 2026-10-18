"""Confirmed observations are scoped overlays; manual pool facts stay independent."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from mino_nexus.core.execution_user import require_user_id
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
        owner_user_id=scope["owner"], env=scope["env"], status="confirmed").order_by(AccountFactCandidate.created_at).all()
    result = dict(facets)
    for row in rows:
        if (row.decision or {}).get("expires_at", 0) <= time.time():
            continue
        if any((row.evidence_context or {}).get(key) != scope[key] for key in ("app_id", "platform", "build")):
            continue
        # A later manual correction invalidates the observation's baseline.
        if any(facets.get(key, "unknown") != (row.before or {}).get(key, "unknown") for key in row.proposed):
            continue
        result.update(row.proposed)
    return result
