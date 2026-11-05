from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.api.dependencies import CurrentUser, Db, OwnedService, ResourceId
from app.api.projects import iso
from app.core.errors import ApiError
from app.core.pagination import Cursor, Limit, page_query
from app.models.evaluation import Evaluation
from app.models.prompt_suggestion import PromptSuggestion
from app.schemas.prompt_suggestion import (SuggestionCreate, SuggestionSelection, SuggestionResponse,
    SuggestionSummary, SuggestionRerunResponse, SuggestionCapabilities)
from app.schemas.responses import Data, Page, ERROR_RESPONSES
from app.services import prompt_suggestions as service
from app.services.evaluations import TERMINAL
from app.services.prompt_suggestion_generator import SuggestionGenerator, get_suggestion_generator

router = APIRouter(prefix='/api/projects/{projectId}/services/{serviceId}/prompt-suggestions',
                   tags=['prompt-suggestions'], responses=ERROR_RESPONSES)
Generator = Annotated[SuggestionGenerator, Depends(get_suggestion_generator)]


def summary(row):
    return {'id': row.id, 'evaluationId': row.evaluation_id, 'baseVersionId': row.base_version_id,
        'appliedPromptVersionId': row.applied_version_id, 'status': row.status, 'revision': row.revision,
        'createdAt': iso(row.created_at)}


def data(row, chatbot):
    root = f'/api/projects/{chatbot.project_id}/services/{chatbot.id}'
    return {**summary(row), 'updatedAt': iso(row.updated_at), 'snapshot': row.snapshot,
        'suggestions': row.suggestions, 'selectedSuggestionIds': row.selected_ids,
        'previewContent': service.preview(row) if row.status in ('READY', 'APPLIED') else None,
        'estimate': service.estimate(row), 'error': row.error,
        'links': {'saveVersion': f'{root}/prompts/{row.snapshot["promptId"]}/versions',
                  'rerun': f'{root}/evaluations/{row.evaluation_id}/reruns',
                  'results': f'{root}/prompt-suggestions/{row.id}/results',
                  'baseline': f'{root}/evaluations/{row.evaluation_id}'}}


@router.get('/capabilities', response_model=Data[SuggestionCapabilities])
def capabilities(chatbot: OwnedService, generator: Generator):
    descriptor = generator.descriptor()
    return {'data': {'generationSupported': descriptor['provider'] == 'bedrock',
        'provider': descriptor['provider'], 'diffSupported': True, 'expectedImprovementCountSupported': False,
        'reason': '근거가 연결된 수정 초안만 제공합니다. 개선 개수는 재시험 전 추정할 수 없습니다.'}}


@router.post('', status_code=201, response_model=Data[SuggestionResponse],
    responses={200: {'model': Data[SuggestionResponse]}, 202: {'model': Data[SuggestionResponse]}})
def create(body: SuggestionCreate, chatbot: OwnedService, user: CurrentUser, db: Db,
           response: Response, generator: Generator,
           idempotency_key: Annotated[str, Header(alias='Idempotency-Key', min_length=1,
                                                 max_length=100, pattern=r'^[!-~]+$')]):
    row, created = service.generate_suggestions(db, chatbot.id, user.id, body, idempotency_key, generator)
    response.status_code = 202 if row.status == 'GENERATING' else 201 if created else 200
    response.headers['Location'] = f'/api/projects/{chatbot.project_id}/services/{chatbot.id}/prompt-suggestions/{row.id}'
    if row.status == 'GENERATING':
        response.headers['Retry-After'] = '2'
    return {'data': data(row, chatbot)}


@router.get('', response_model=Data[Page[SuggestionSummary]])
def list_suggestions(chatbot: OwnedService, db: Db, limit: Limit = 20, cursor: Cursor = None):
    """Page lightweight metadata without loading model output or frozen evidence."""
    statement = select(PromptSuggestion).options(load_only(PromptSuggestion.id,
        PromptSuggestion.evaluation_id, PromptSuggestion.base_version_id, PromptSuggestion.applied_version_id,
        PromptSuggestion.status, PromptSuggestion.revision, PromptSuggestion.created_at))
    rows, cursor = page_query(db, statement.where(PromptSuggestion.service_id == chatbot.id),
        PromptSuggestion, f'promptSuggestions:{chatbot.id}', limit, cursor)
    return {'data': {'items': [summary(row) for row in rows], 'nextCursor': cursor}}


@router.get('/{suggestionId}', response_model=Data[SuggestionResponse])
def detail(suggestionId: ResourceId, chatbot: OwnedService, db: Db):
    return {'data': data(service.get_suggestion(db, chatbot.id, suggestionId), chatbot)}


@router.patch('/{suggestionId}', response_model=Data[SuggestionResponse])
def select_suggestions(suggestionId: ResourceId, body: SuggestionSelection, chatbot: OwnedService, db: Db):
    row = service.get_suggestion(db, chatbot.id, suggestionId)
    if row.status != 'READY':
        raise ApiError(409, 'SUGGESTION_CONFLICT', '생성 완료한 미적용 제안만 선택할 수 있습니다.')
    service.preview(row, body.selectedSuggestionIds)
    service.guarded_update(db, row, body.expectedRevision, selected_ids=sorted(body.selectedSuggestionIds))
    db.commit()
    db.refresh(row)
    return {'data': data(row, chatbot)}


@router.get('/{suggestionId}/results', response_model=Data[Page[SuggestionRerunResponse]])
def results(suggestionId: ResourceId, chatbot: OwnedService, db: Db, limit: Limit = 20, cursor: Cursor = None):
    """Link real reruns without loading their input snapshots or encrypted credentials."""
    row = service.get_suggestion(db, chatbot.id, suggestionId)
    if row.applied_version_id is None:
        return {'data': {'items': [], 'nextCursor': None}}
    # Relations derive from immutable rerun inputs; no new execution or comparison implementation.
    statement = select(Evaluation).options(load_only(Evaluation.id, Evaluation.status,
        Evaluation.created_at)).where(Evaluation.service_id == chatbot.id,
        Evaluation.inputs['sourceEvaluationId'].as_string() == row.evaluation_id,
        Evaluation.inputs['prompts']['a']['id'].as_string() == row.applied_version_id)
    runs, cursor = page_query(db, statement, Evaluation, f'suggestionResults:{row.id}', limit, cursor)
    root = f'/api/projects/{chatbot.project_id}/services/{chatbot.id}/evaluations'
    return {'data': {'items': [{'suggestionId': row.id, 'baselineEvaluationId': row.evaluation_id,
        'evaluationId': run.id, 'promptVersionId': row.applied_version_id, 'status': run.status,
        'comparisonAvailable': run.status in TERMINAL,
        'comparisonUrl': f'{root}/{run.id}/comparison'} for run in runs], 'nextCursor': cursor}}
