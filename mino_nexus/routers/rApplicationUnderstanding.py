"""Application knowledge reviews and diagnostic impact; no device operations."""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session, project_maintainer
from mino_nexus.services import application_understanding as svc

router = APIRouter(prefix='/project/{project_id}/apps/{app_id}/understanding', tags=['Application understanding'], dependencies=[Depends(current_session)])
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]

class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Example(StrictBody):
    session_id: Text
    seq: int = Field(ge=0)
    polarity: Literal['positive', 'negative']

class Proposal(StrictBody):
    request_id: Text
    version: Text = 'v1'
    expected_revision: int = Field(ge=0)
    platform: Literal['web', 'android', 'ios', 'pc']
    build: Text
    reason: Text
    aliases: list[Text] = Field(default_factory=list, max_length=30)
    positive_terms: list[Text] = Field(min_length=1, max_length=30)
    negative_terms: list[Text] = Field(min_length=1, max_length=30)
    examples: list[Example] = Field(min_length=2, max_length=30)

class Decision(StrictBody):
    version: Text = 'v1'
    expected_revision: int = Field(ge=0)
    approve: bool
    reason: Text

class Revalidation(StrictBody):
    request_id: Text
    version: Text = 'v1'
    review_id: str = Field(default='', max_length=300)
    impact_token: Text
    case_ids: list[Text] = Field(min_length=1, max_length=100)
    action: Literal['recheck', 'compile']
    reason: Text

def call(fn, *args, **kwargs):
    try:
        return ok(fn(*args, **kwargs))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.get('/pages')
def pages(project_id: str, app_id: str):
    return call(svc.pages, project_id, app_id)

@router.get('/trends')
def trends(project_id: str, app_id: str, platform: str = Query(default='', max_length=40)):
    return call(svc.trends, project_id, app_id, platform=platform)

@router.post('/pages/{state_id}/reviews', dependencies=[Depends(project_maintainer)])
def propose(project_id: str, app_id: str, state_id: str, body: Proposal):
    return call(svc.propose, project_id, app_id, state_id, body)

@router.post('/pages/{state_id}/reviews/{review_id}', dependencies=[Depends(project_maintainer)])
def decide(project_id: str, app_id: str, state_id: str, review_id: str, body: Decision):
    return call(svc.decide, project_id, app_id, state_id, review_id, body)

@router.get('/pages/{state_id}/impact')
def impact(project_id: str, app_id: str, state_id: str, version: str = Query(default='v1', max_length=300), review_id: str = Query(default='', max_length=300)):
    return call(svc.impact, project_id, app_id, state_id, version=version, review_id=review_id)

@router.post('/pages/{state_id}/revalidate', dependencies=[Depends(project_maintainer)])
def revalidate(project_id: str, app_id: str, state_id: str, body: Revalidation):
    return call(svc.revalidate, project_id, app_id, state_id, body)

# Project resources use a separate prefix; application scope cannot confer ownership.
resource_router = APIRouter(prefix='/project/{project_id}/account-facts', dependencies=[Depends(current_session)])

class FactDecision(StrictBody):
    approve: bool
    reason: Text
    expected_digest: Text
    evidence_seq: int | None = Field(default=None, ge=0)

@resource_router.get('')
def account_facts(project_id: str):
    from mino_nexus.services.account_fact_review import listing
    return call(listing, project_id)

@resource_router.post('/{candidate_id}', dependencies=[Depends(project_maintainer)])
def account_fact_decide(project_id: str, candidate_id: str, body: FactDecision):
    from mino_nexus.services.account_fact_review import decide
    return call(decide, project_id, candidate_id, body)
