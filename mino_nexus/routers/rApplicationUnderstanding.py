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
    valid_for_seconds: int = Field(default=86400, ge=60, le=2592000)

@resource_router.get('')
def account_facts(project_id: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  status: str = Query('', pattern='^(|candidate|confirmed|rejected)$')):
    from mino_nexus.services.account_fact_review import listing
    return call(listing, project_id, limit=limit, offset=offset, status=status)

@resource_router.post('/{candidate_id}', dependencies=[Depends(project_maintainer)])
def account_fact_decide(project_id: str, candidate_id: str, body: FactDecision):
    from mino_nexus.services.account_fact_review import decide
    return call(decide, project_id, candidate_id, body)

@router.get("/compile-metrics")
def compile_metrics(project_id: str, app_id: str, platform: str = Query(default="", max_length=40), days: int = Query(default=30, ge=1, le=90)):
    from mino_nexus.services.compile_metrics import summary
    return call(summary, project_id, app_id, platform=platform, days=days)


class FactRuleBody(StrictBody):
    expected_revision: int = Field(ge=0)
    name: Text
    app_id: Text
    env: Text
    platform: Literal['web', 'android', 'ios', 'pc']
    build: Text
    case_id: Text
    point_id: Text
    build_point_id: Text
    facet_key: Text
    facet_value: Text
    valid_for_seconds: int = Field(default=86400, ge=60, le=2592000)
    enabled: bool = True


@resource_router.get('/rules')
def fact_rules(project_id: str):
    from mino_nexus.services.account_fact_verification import rules, options
    return call(lambda: {**rules(project_id), **options(project_id)})


@resource_router.put('/rules/{rule_id}', dependencies=[Depends(project_maintainer)])
def fact_rule_save(project_id: str, rule_id: str, body: FactRuleBody):
    from mino_nexus.services.account_fact_verification import save_rule
    return call(save_rule, project_id, rule_id, body)


@resource_router.post('/{candidate_id}/verify', dependencies=[Depends(project_maintainer)])
def fact_verify(project_id: str, candidate_id: str):
    from mino_nexus.services.account_fact_verification import verify_candidate
    return call(verify_candidate, project_id, candidate_id)


class RecognitionLabel(StrictBody):
    request_id: Text
    expected_revision: int = Field(ge=0)
    expected_state_id: str = Field(default='', max_length=300)
    reason: Text


@router.get('/comparisons')
def recognition_comparisons(project_id: str, app_id: str,
        platform: str = Query('', max_length=40), build: str = Query('', max_length=300),
        limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1900)):
    from mino_nexus.services.page_recognition_comparison import listing
    return call(listing, project_id, app_id, platform=platform, build=build, limit=limit, offset=offset)


@router.post('/comparisons/{session_id}/events/{seq}', dependencies=[Depends(project_maintainer)])
def recognition_label(project_id: str, app_id: str, session_id: str, seq: int, body: RecognitionLabel):
    from mino_nexus.services.page_recognition_comparison import label
    return call(label, project_id, app_id, session_id, seq, body)


@resource_router.get('/{candidate_id}/observations')
def fact_observations(project_id: str, candidate_id: str):
    from mino_nexus.services.account_fact_review import observations
    return call(observations, project_id, candidate_id)
