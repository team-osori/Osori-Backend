from copy import deepcopy

from sqlalchemy import select

from app.core.errors import ApiError
from app.models.connection import Connection
from app.models.evaluation import Evaluation, EvaluationItem
from app.services.evaluation_judge import CRITERIA
from app.services.policy import get_evaluation_snapshot, iso
from app.services.prompts import get_version, select_ab_versions
from app.services import condition_evaluation

CRITERION_KEYS = tuple(CRITERIA['definitions'])
TERMINAL = ('COMPLETED', 'PARTIAL_FAILED', 'FAILED', 'CANCELLED')


def side_names(inputs):
    return ('a',) if inputs.get('mode') == 'SINGLE' else ('a', 'b')


def get_run(db, service_id, run_id):
    """Require service membership in addition to the route's ownership check."""
    row = db.scalar(select(Evaluation).where(Evaluation.id == run_id, Evaluation.service_id == service_id))
    if row is None:
        raise ApiError(404, 'RESOURCE_NOT_FOUND', '평가를 찾을 수 없습니다.')
    return row


def build_inputs(db, service, body, judge):
    """Freeze verified connection settings, source policies, questions and both prompt originals."""
    connection = db.scalar(select(Connection).where(Connection.service_id == service.id).with_for_update())
    if connection is None:
        raise ApiError(409, 'CONNECTION_REQUIRED', '챗봇 연동 설정을 먼저 등록하세요.')
    if connection.revision != body.connectionRevision:
        raise ApiError(409, 'CONNECTION_CHANGED', '최신 연동 revision을 확인하세요.')
    if connection.verified_revision != connection.revision:
        raise ApiError(409, 'CONNECTION_NOT_VERIFIED', '현재 연동 설정으로 연결 테스트를 완료하세요.')
    mode = body.resolved_mode
    inject_prompt = mode == 'COMPARE' or body.promptVersionId is not None
    if inject_prompt and connection.config['executionMode'] != 'PROMPT_BODY':
        raise ApiError(409, 'UNSUPPORTED_EXECUTION_MODE', 'A/B 평가는 PROMPT_BODY 모드가 필요합니다.')
    if connection.config['auth']['type'] != 'NONE' and not connection.secret_ciphertext:
        raise ApiError(409, 'CONNECTION_SECRET_REQUIRED', '기업 챗봇 인증정보를 등록하세요.')
    if mode == 'COMPARE':
        a, b = select_ab_versions(db, service.id, body.aPromptVersionId, body.bPromptVersionId)
        versions = (('a', a), ('b', b))
    elif body.promptVersionId:
        versions = (('a', get_version(db, service.id, body.promptVersionId)),)
    else:
        versions = ()
    source = None
    if body.sourceEvaluationId:
        source = get_run(db, service.id, body.sourceEvaluationId)
        if source.status not in TERMINAL or source.inputs.get('mode') != 'SINGLE' or source.inputs['criteria']['version'] != 2:
            raise ApiError(409, 'INVALID_REEVALUATION_SOURCE', '종료한 단일 평가를 선택하세요.')
        if source.inputs['testSet']['testSetVersionId'] != body.testSetVersionId:
            raise ApiError(409, 'COMPARISON_INPUT_MISMATCH', '동일한 질문·기준 스냅샷이 필요합니다.')
        if source.inputs['connection']['config'] != connection.config or source.inputs['judge'] != judge.descriptor():
            raise ApiError(409, 'COMPARISON_CONFIGURATION_CHANGED', '연동 설정 또는 판정 모델이 변경되었습니다. 새 평가를 시작하세요.')
    testset = deepcopy(source.inputs['testSet']) if source else get_evaluation_snapshot(db, service.project_id, body.testSetVersionId)
    if not 1 <= len(testset['questions']) <= 200:
        raise ApiError(409, 'INVALID_TESTSET', '평가 가능한 확정 문항이 필요합니다.')
    criteria = CRITERIA
    if not source and (mode == 'SINGLE' or body.mode == 'COMPARE'):
        try:
            testset = condition_evaluation.prepare_questions(testset)
        except (ValueError, TypeError, KeyError):
            raise ApiError(409, 'INVALID_EXPECTED_CONDITIONS', '기대 조건과 정책 출처를 확인해 주세요.') from None
        criteria = condition_evaluation.CRITERIA
    if source:
        criteria = source.inputs['criteria']
    assurance = {'sameQuestion': True, 'sameConnectionConfig': True, 'sameCriteria': True,
                 'promptAppliedVerified': False, 'retrievalInputsControlled': False}
    inputs = {'schemaVersion': 1, 'mode': mode, 'useConfiguredPrompt': not inject_prompt,
        'sourceEvaluationId': source.id if source else None,
        'connection': {'id': connection.id, 'revision': connection.revision,
        'config': deepcopy(connection.config)},
        'prompts': {side: {'id': version.id, 'promptId': version.prompt_id, 'version': version.version,
                    'content': version.content, 'label': version.label} for side, version in versions},
        'testSet': deepcopy(testset), 'criteria': deepcopy(criteria),
        'judge': deepcopy(judge.descriptor()), 'comparisonAssurance': assurance}
    return inputs, connection.secret_ciphertext


def run_data(row):
    """Expose progress and judge readiness, never encrypted credentials or full inputs."""
    return {'id': row.id, 'serviceId': row.service_id, 'status': row.status,
        'mode': row.inputs.get('mode', 'COMPARE'),
        'sourceEvaluationId': row.inputs.get('sourceEvaluationId'),
        'totalQuestions': row.total_questions, 'completedQuestions': row.completed_questions,
        'failedQuestions': row.failed_questions, 'judge': row.inputs['judge'],
        'comparisonAssurance': row.inputs['comparisonAssurance'],
        'createdAt': iso(row.created_at), 'finishedAt': iso(row.finished_at)}


def verdict(side, criterion):
    """Call failures and unfinished sides cannot become policy failures or successes."""
    if side is None or side['callStatus'] != 'SUCCEEDED':
        return None
    result = side['criteria'][criterion]
    return result['verdict'].upper() if result['status'] == 'judged' else 'NOT_EVALUABLE'


def change(a, b, criterion):
    """Compare only pairs with two real PASS/FAIL judgements."""
    left, right = verdict(a, criterion), verdict(b, criterion)
    if left not in ('PASS', 'FAIL') or right not in ('PASS', 'FAIL'):
        return 'NOT_COMPARABLE'
    return {('FAIL', 'PASS'): 'IMPROVED', ('PASS', 'FAIL'): 'WORSENED',
            ('PASS', 'PASS'): 'UNCHANGED_PASS', ('FAIL', 'FAIL'): 'UNCHANGED_FAIL'}[(left, right)]


def item_summary(row, inputs=None):
    """Keep list responses bounded by excluding answer and document bodies."""
    single = inputs is not None and inputs.get('mode') == 'SINGLE'
    return {'id': row.id, 'questionId': row.question_id, 'question': row.input['question'],
        'status': row.status, 'aCallStatus': row.a['callStatus'] if row.a else 'NOT_RUN',
        'bCallStatus': 'NOT_APPLICABLE' if single else row.b['callStatus'] if row.b else 'NOT_RUN',
        'changes': {} if single else {key: change(row.a, row.b, key) for key in
            (inputs['criteria']['definitions'] if inputs else CRITERION_KEYS)}}


def criterion_summary(inputs, rows):
    """Compute per-criterion rates with only PASS+FAIL in the denominator, never a total score."""
    results = {}
    for key in inputs.get('criteria', CRITERIA)['definitions']:
        entry = {}
        for side_name in side_names(inputs):
            counts = {'pass': 0, 'fail': 0, 'notEvaluable': 0, 'judgeFailed': 0,
                      'callFailed': 0, 'callInterrupted': 0, 'inProgress': 0, 'notRun': 0}
            for row in rows:
                side = getattr(row, side_name)
                if side is None or side['callStatus'] == 'NOT_RUN':
                    counts['notRun'] += 1
                elif side['callStatus'] == 'RUNNING':
                    counts['inProgress'] += 1
                elif side['callStatus'] == 'INTERRUPTED':
                    counts['callInterrupted'] += 1
                elif side['callStatus'] == 'FAILED':
                    counts['callFailed'] += 1
                elif side['criteria'][key]['status'] == 'error':
                    counts['judgeFailed'] += 1
                else:
                    counts[{'PASS': 'pass', 'FAIL': 'fail', 'NOT_EVALUABLE': 'notEvaluable'}[verdict(side, key)]] += 1
            denominator = counts['pass'] + counts['fail']
            entry[side_name] = {**counts, 'denominator': denominator,
                'passRate': round(counts['pass'] / denominator * 100, 2) if denominator else None}
        if inputs.get('mode') == 'SINGLE':
            results[key] = {**entry, 'b': None, 'comparison': None}
            continue
        changes = {label: 0 for label in ('IMPROVED', 'WORSENED', 'UNCHANGED_PASS', 'UNCHANGED_FAIL', 'NOT_COMPARABLE')}
        for row in rows:
            changes[change(row.a, row.b, key)] += 1
        paired = sum(count for label, count in changes.items() if label != 'NOT_COMPARABLE')
        entry['comparison'] = {**changes, 'pairedDenominator': paired,
            'aPairedPassRate': round((changes['WORSENED'] + changes['UNCHANGED_PASS']) / paired * 100, 2) if paired else None,
            'bPairedPassRate': round((changes['IMPROVED'] + changes['UNCHANGED_PASS']) / paired * 100, 2) if paired else None}
        results[key] = entry
    return results


def aggregate(run, rows):
    return {'evaluation': run_data(run), 'criteria': criterion_summary(run.inputs, rows),
            'formula': 'PASS / (PASS + FAIL) * 100', 'provisional': run.status not in TERMINAL}
