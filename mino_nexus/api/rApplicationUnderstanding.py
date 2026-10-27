"""Application knowledge reviews and diagnostic impact; no device operations."""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from mino_nexus.core.http_util import ok
from mino_nexus.api.deps import current_session, project_maintainer
from mino_nexus.casework import application_understanding as svc

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


@router.get('/pages/{state_id}/case-refs')
def case_refs(project_id: str, app_id: str, state_id: str):
    return call(svc.case_refs, project_id, app_id, state_id)

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
    from mino_nexus.resources.account_fact_review import listing
    return call(listing, project_id, limit=limit, offset=offset, status=status)

@resource_router.post('/{candidate_id}', dependencies=[Depends(project_maintainer)])
def account_fact_decide(project_id: str, candidate_id: str, body: FactDecision):
    from mino_nexus.resources.account_fact_review import decide
    return call(decide, project_id, candidate_id, body)

@router.get("/compile-metrics")
def compile_metrics(project_id: str, app_id: str, platform: str = Query(default="", max_length=40), days: int = Query(default=30, ge=1, le=90)):
    from mino_nexus.casework.compile_metrics import summary
    return call(summary, project_id, app_id, platform=platform, days=days)


@router.get("/coverage")
def coverage(project_id: str, app_id: str, include_events: bool = Query(default=True)):
    """新覆盖口径：业务意图闭合率（主）+ 事件×渠道可达性。"""
    from mino_nexus.casework import coverage as svc_coverage
    if include_events:
        return call(svc_coverage.summary, project_id, app_id)
    return call(svc_coverage.intent_coverage, project_id, app_id)


@router.get("/intent-coverage")
def intent_coverage(project_id: str, app_id: str):
    from mino_nexus.casework import coverage as svc_coverage
    return call(svc_coverage.intent_coverage, project_id, app_id)


@router.get("/event-channel-matrix")
def event_channel_matrix(project_id: str, app_id: str):
    from mino_nexus.casework import coverage as svc_coverage
    return call(svc_coverage.event_channel_matrix, project_id, app_id)


@router.get("/intent-graph")
def intent_graph(project_id: str, app_id: str):
    from mino_nexus.knowledge import intent_graph as svc_graph
    return call(svc_graph.describe, app_id=app_id, project_id=project_id)


@router.get("/failure-attribution")
def failure_attribution(project_id: str, app_id: str):
    from mino_nexus.contract import attribution
    return call(attribution.describe)


@router.get("/private-blocks")
def private_blocks(project_id: str, app_id: str):
    from mino_nexus.casework import private_blocks as svc_blocks
    return call(svc_blocks.reverse_metrics, project_id, app_id)


@router.delete("/private-blocks", dependencies=[Depends(project_maintainer)])
def private_blocks_clear(project_id: str, app_id: str):
    from mino_nexus.casework import private_blocks as svc_blocks
    return call(svc_blocks.clear_private_blocks, project_id, app_id)


@router.post("/private-blocks/mine", dependencies=[Depends(project_maintainer)])
def private_blocks_mine(project_id: str, app_id: str, min_cases: int = Query(default=2, ge=1, le=50)):
    from mino_nexus.casework import private_blocks as svc_blocks
    return call(svc_blocks.seed_private_blocks, project_id, app_id, min_cases=min_cases)


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
    from mino_nexus.resources.account_fact_verification import rules, options
    return call(lambda: {**rules(project_id), **options(project_id)})


@resource_router.put('/rules/{rule_id}', dependencies=[Depends(project_maintainer)])
def fact_rule_save(project_id: str, rule_id: str, body: FactRuleBody):
    from mino_nexus.resources.account_fact_verification import save_rule
    return call(save_rule, project_id, rule_id, body)


@resource_router.post('/{candidate_id}/verify', dependencies=[Depends(project_maintainer)])
def fact_verify(project_id: str, candidate_id: str):
    from mino_nexus.resources.account_fact_verification import verify_candidate
    return call(verify_candidate, project_id, candidate_id)


class IntentChoiceBody(StrictBody):
    question: Text
    context: str = Field(default='', max_length=4000)
    domain: str = Field(default='', max_length=80)
    image_base64: str = Field(default='', max_length=12_000_000)
    image_mime: str = Field(default='image/png', max_length=80)


class RecognitionLabel(StrictBody):
    request_id: Text
    expected_revision: int = Field(ge=0)
    expected_state_id: str = Field(default='', max_length=300)
    reason: Text


@router.get('/intent-options')
def intent_options(project_id: str, app_id: str, domain: str = Query(default='', max_length=80)):
    from mino_nexus.knowledge import intent_graph
    return call(intent_graph.intent_options, domain=domain, app_id=app_id, project_id=project_id)


@router.post('/intent-choice')
def intent_choice(project_id: str, app_id: str, body: IntentChoiceBody):
    """用现有 LLM/VLM 做意图判断，替代 Jev/Laya 的本地决策模型。"""
    from mino_nexus.ai import intent_router
    choice = intent_router.choose_intent(
        question=body.question, app_id=app_id,
        domain=body.domain, context=body.context,
        image_base64=body.image_base64, image_mime=body.image_mime,
    )
    if choice.get("error"):
        raise HTTPException(status_code=409, detail=choice["error"])
    return ok(choice)


@router.get('/comparisons')
def recognition_comparisons(project_id: str, app_id: str,
        platform: str = Query('', max_length=40), build: str = Query('', max_length=300),
        limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1900)):
    from mino_nexus.casework.page_recognition_comparison import listing
    return call(listing, project_id, app_id, platform=platform, build=build, limit=limit, offset=offset)


@router.post('/comparisons/{session_id}/events/{seq}', dependencies=[Depends(project_maintainer)])
def recognition_label(project_id: str, app_id: str, session_id: str, seq: int, body: RecognitionLabel):
    from mino_nexus.casework.page_recognition_comparison import label
    return call(label, project_id, app_id, session_id, seq, body)


@resource_router.get('/{candidate_id}/observations')
def fact_observations(project_id: str, candidate_id: str):
    from mino_nexus.resources.account_fact_review import observations
    return call(observations, project_id, candidate_id)
