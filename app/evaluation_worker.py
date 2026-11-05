"""Durable evaluation executor: python -m app.evaluation_worker [--once]."""
import argparse
import asyncio
from copy import deepcopy
from datetime import timedelta
import logging
import signal

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.core.concurrency import is_write_conflict
from app.core.database import SessionLocal
from app.core.secrets import decrypt_secret
from app.models import auth, project, prompt, policy  # noqa: F401 - referenced tables
from app.models.evaluation import Evaluation, EvaluationItem
from app.schemas.evaluation import Evidence, Judgement
from app.services.auth import now
from app.services.chatbot_gateway import execute_chatbot
from app.services.evaluation_judge import get_evaluation_judge, JudgeError
from app.services.evaluations import CRITERION_KEYS, side_names
from app.services.policy import uid
from app.services import condition_evaluation

LEASE_SECONDS = 600
JUDGE_TIMEOUT_SECONDS = 30


def ungraded(reason, question_id, error=None, *, inputs=None, question=None):
    """Keep the model's criterion result contract even when no judgement is available."""
    if inputs and inputs['criteria']['version'] == 2:
        return condition_evaluation.unavailable(question, reason, error)
    return {key: {'question_id': question_id, 'status': 'error' if error else 'not_evaluable',
        'verdict': None, 'reason': reason, 'evidence_refs': [], 'error': deepcopy(error)} for key in CRITERION_KEYS}


def failed_side(code, message, question_id, *, inputs=None, question=None):
    """Keep execution failures out of pass-rate denominators."""
    results = ungraded('호출 또는 실행 실패', question_id, inputs=inputs, question=question)
    conditions = results.pop('conditionResults', [])
    return {'callStatus': 'FAILED', 'answer': None, 'evidence': Evidence().model_dump(), 'evidenceCatalog': [],
            'latencyMs': 0, 'attemptCount': 0, 'error': {'code': code, 'message': message},
            'judgeError': None, 'criteria': results, 'conditionResults': conditions}


def pending_side(status, question_id, error=None, *, inputs=None, question=None):
    """Distinguish unstarted, active and uncertain calls from confirmed transport failures."""
    reason = {'NOT_RUN': '기업 챗봇 미호출', 'RUNNING': '기업 챗봇 호출·판정 처리 중',
              'INTERRUPTED': '호출 결과를 저장하기 전에 작업이 중단되어 결과를 확인할 수 없음'}[status]
    results = ungraded(reason, question_id, inputs=inputs, question=question)
    conditions = results.pop('conditionResults', [])
    return {'callStatus': status, 'answer': None, 'evidence': Evidence().model_dump(), 'evidenceCatalog': [],
            'latencyMs': 0 if status == 'NOT_RUN' else None, 'attemptCount': 0 if status == 'NOT_RUN' else None,
            'error': deepcopy(error), 'judgeError': None, 'criteria': results, 'conditionResults': conditions}


def close_interrupted(db, run, code='WORKER_INTERRUPTED'):
    """End uncertain work without replaying external calls; preserve every already saved side."""
    items = db.scalars(select(EvaluationItem).where(EvaluationItem.evaluation_id == run.id).with_for_update()).all()
    for item in items:
        if item.status in ('QUEUED', 'RUNNING'):
            error = {'code': code, 'message': '작업 실행이 중단되었습니다.'}
            for name in side_names(run.inputs):
                side = getattr(item, name)
                # Missing results on legacy RUNNING rows may follow an actual call.
                if side is None:
                    status = 'NOT_RUN' if item.status == 'QUEUED' else 'INTERRUPTED'
                elif side['callStatus'] in ('NOT_RUN', 'RUNNING'):
                    status = 'NOT_RUN' if side['callStatus'] == 'NOT_RUN' else 'INTERRUPTED'
                else:
                    continue  # Preserve already persisted successes and failures.
                setattr(item, name, pending_side(status, item.question_id, error, inputs=run.inputs, question=item.input))
            sides = [getattr(item, name) for name in side_names(run.inputs)]
            if all(side['callStatus'] == 'SUCCEEDED' for side in sides):
                item.status = 'COMPLETED'
            elif all(side['callStatus'] == 'NOT_RUN' for side in sides):
                item.status = 'NOT_RUN'
            elif any(side['callStatus'] == 'FAILED' for side in sides):
                item.status = 'FAILED'
            else:
                item.status = 'INTERRUPTED'
    db.flush()
    finish(db, run, items)


def finish(db, run, items=None):
    """Finalize execution status separately from whether an AI judge supplied grades."""
    items = items if items is not None else db.scalars(select(EvaluationItem).where(EvaluationItem.evaluation_id == run.id)).all()
    closed = ('COMPLETED', 'FAILED', 'NOT_RUN', 'INTERRUPTED')
    run.completed_questions = sum(item.status in closed for item in items)
    run.failed_questions = sum(item.status in closed and item.status != 'COMPLETED' for item in items)
    any_success = any(side and side['callStatus'] == 'SUCCEEDED' for item in items for side in (getattr(item, name) for name in side_names(run.inputs)))
    run.status = ('PARTIAL_FAILED' if any_success else 'FAILED') if run.failed_questions else 'COMPLETED'
    run.finished_at = now()
    # Credentials are no longer needed after terminal execution; inputs remain reproducible.
    run.secret_ciphertext = None


def expire_abandoned(factory):
    """A stale lease becomes terminal failure, never an automatic duplicate company request."""
    with factory() as db:
        ids = db.scalars(select(Evaluation.id).where(Evaluation.status == 'RUNNING',
            Evaluation.heartbeat_at < now() - timedelta(seconds=LEASE_SECONDS))).all()
        for id_ in ids:
            row = db.scalar(select(Evaluation).where(Evaluation.id == id_).with_for_update())
            if row.status == 'RUNNING' and row.heartbeat_at.replace(tzinfo=None) < (now() - timedelta(seconds=LEASE_SECONDS)).replace(tzinfo=None):
                close_interrupted(db, row)
        db.commit()


def evidence_catalog(inputs, answer, evidence):
    """Give the judge bounded, server-owned reference IDs tied to exact snapshot sources."""
    sources = [{'kind': 'answer', 'source_id': None, 'text': answer}]
    sources.extend({'kind': 'policy', 'source_id': p['id'], 'text': p['content']}
                   for p in inputs['testSet']['policies'])
    sources.extend({'kind': 'retrieval', 'source_id': doc['id'], 'text': doc['text']}
                   for doc in evidence.get('documents') or [])
    return [{'evidence_id': f'ev_{index:03d}', **source} for index, source in enumerate(sources, 1)]


async def judge_response(judge, inputs, question, answer, evidence):
    """Validate question identity, result states and references to the server-owned catalog."""
    error = None
    catalog = evidence_catalog(inputs, answer, evidence)
    allowed = {source['evidence_id'] for source in catalog}
    try:
        async with asyncio.timeout(getattr(judge, 'timeout_seconds', JUDGE_TIMEOUT_SECONDS)):
            output = await judge.judge(question=deepcopy(question), answer=answer, evidence=deepcopy(evidence),
                policies=deepcopy(inputs['testSet']['policies']), criteria=deepcopy(inputs['criteria']),
                evidence_catalog=deepcopy(catalog))
        if inputs['criteria']['version'] == 2:
            output = condition_evaluation.validate_judgement(output, question, catalog)
        else:
            output = Judgement.model_validate(output).model_dump()
            for result in output.values():
                if result['question_id'] != question['id'] or any(ref['evidence_id'] not in allowed for ref in result['evidence_refs']):
                    raise ValueError('Question or evidence identity mismatch')
    except TimeoutError:
        error = {'code': 'JUDGE_TIMEOUT', 'message': '평가 모델 응답 시간이 초과되었습니다.'}
        output = ungraded('평가 모델 응답 시간 초과', question['id'], error, inputs=inputs, question=question)
    except JudgeError as exc:
        error = {'code': exc.code, 'message': exc.message}
        output = ungraded(exc.message, question['id'], error, inputs=inputs, question=question)
    except Exception:
        error = {'code': 'INVALID_JUDGE_OUTPUT', 'message': '유효한 평가 판정을 받지 못했습니다.'}
        output = ungraded('평가 모델 오류 또는 잘못된 판정 근거', question['id'], error, inputs=inputs, question=question)
    if error is not None and inputs['criteria']['version'] == 2:
        output = condition_evaluation.unavailable(question, output['policy']['reason'], error)
    if error is None and inputs['criteria']['version'] == 1 and evidence.get('documents') is None:
        for key in ('retrieval', 'grounding'):
            if output[key]['status'] != 'error':
                output[key] = ungraded('기업 API의 검색 근거 자료 미제공', question['id'])[key]
    if error is None and inputs['criteria']['version'] == 2 and evidence.get('documents') is None:
        conditions = {condition['id']: condition for condition in question['expectedConditions']}
        for result in output['conditionResults']:
            if conditions[result['conditionId']]['criterion'] in ('retrieval', 'grounding') and result['status'] != 'error':
                result.update(status='not_evaluable', verdict=None, reason='기업 API의 검색 근거 자료 미제공',
                              evidence_refs=[], error=None, issue=None)
    if error is None and inputs['judge']['adapter'] == 'unconfigured':
        error = {'code': 'JUDGE_NOT_CONFIGURED', 'message': '평가 모델이 아직 연결되지 않았습니다.'}
    return output, error


async def execute_side(inputs, question, side, secret, judge, sender):
    """Call both variants with the same frozen request settings and isolated network sessions."""
    try:
        result = await execute_chatbot(deepcopy(inputs['connection']['config']), question['question'],
            inputs['prompts'].get(side, {}).get('content'), secret, sender=sender,
            apply_prompt=not inputs.get('useConfiguredPrompt', False))
        if result.error:
            failed = failed_side(result.error['code'], result.error['message'], question['id'], inputs=inputs, question=question)
            failed.update(latencyMs=result.latency_ms, attemptCount=result.attempt_count)
            return failed
        evidence = Evidence.model_validate(result.evidence or {}).model_dump()
        criteria, judge_error = await judge_response(judge, inputs, question, result.answer, evidence)
        conditions = criteria.pop('conditionResults', [])
        return {'callStatus': 'SUCCEEDED', 'answer': result.answer, 'evidence': evidence, 'conditionResults': conditions,
                'evidenceCatalog': [{k: v for k, v in source.items() if k != 'text'}
                                    for source in evidence_catalog(inputs, result.answer, evidence)],
                'latencyMs': result.latency_ms, 'attemptCount': result.attempt_count, 'error': None,
                'judgeError': judge_error, 'criteria': criteria}
    except Exception:
        return failed_side('EXECUTION_ERROR', '문항 실행 중 오류가 발생했습니다.', question['id'], inputs=inputs, question=question)


def locked_run(db, run_id, token):
    """Fence stale workers before each write so an expired process cannot overwrite results."""
    return db.scalar(select(Evaluation).where(Evaluation.id == run_id, Evaluation.status == 'RUNNING',
        Evaluation.lease_token == token).with_for_update())


async def process_one(factory=SessionLocal, *, sender=None, judge=None, stop_event=None):
    """Claim one run atomically, persist each response, and keep unrelated question failures isolated."""
    expire_abandoned(factory)
    token = uid()
    with factory() as db:
        id_ = db.scalar(select(Evaluation.id).where(Evaluation.status == 'QUEUED').order_by(Evaluation.created_at, Evaluation.id).limit(1))
        if id_ is None:
            return False
        try:
            result = db.execute(update(Evaluation).where(Evaluation.id == id_, Evaluation.status == 'QUEUED')
                .values(status='RUNNING', lease_token=token, heartbeat_at=now()))
            db.commit()
        except OperationalError as exc:
            db.rollback()
            if is_write_conflict(exc):
                return True
            raise
        if result.rowcount != 1:
            return True
    judge = judge or get_evaluation_judge()
    with factory() as db:
        row = db.get(Evaluation, id_)
        inputs = deepcopy(row.inputs)
        item_ids = db.scalars(select(EvaluationItem.id).where(EvaluationItem.evaluation_id == id_)
                              .order_by(EvaluationItem.created_at, EvaluationItem.id)).all()
        try:
            if judge.descriptor() != inputs['judge']:
                raise ValueError('Judge settings changed')
            secret = decrypt_secret(row.secret_ciphertext) if row.secret_ciphertext else None
        except Exception:
            close_interrupted(db, row, 'EXECUTION_CONFIG_UNAVAILABLE')
            db.commit()
            return True
    for item_id in item_ids:
        if stop_event and stop_event.is_set():
            with factory() as db:
                row = locked_run(db, id_, token)
                if row:
                    close_interrupted(db, row, 'WORKER_STOPPED')
                    db.commit()
            return True
        with factory() as db:
            row = locked_run(db, id_, token)
            if row is None:
                return True
            item = db.get(EvaluationItem, item_id)
            item.status = 'RUNNING'
            # Persist intent before A; B is explicitly known to be unstarted.
            item.a = pending_side('RUNNING', item.question_id, inputs=inputs, question=item.input)
            item.b = pending_side('NOT_RUN', item.question_id, inputs=inputs, question=item.input) if 'b' in side_names(inputs) else None
            question = deepcopy(item.input)
            row.heartbeat_at = now()
            db.commit()
        for side in side_names(inputs):
            if side == 'b':
                with factory() as db:
                    row = locked_run(db, id_, token)
                    if row is None:
                        return True
                    item = db.get(EvaluationItem, item_id)
                    item.b = pending_side('RUNNING', item.question_id, inputs=inputs, question=item.input)
                    row.heartbeat_at = now()
                    db.commit()
            outcome = await execute_side(inputs, question, side, secret, judge, sender)
            with factory() as db:
                row = locked_run(db, id_, token)
                if row is None:
                    return True
                item = db.get(EvaluationItem, item_id)
                setattr(item, side, outcome)
                row.heartbeat_at = now()
                if side == side_names(inputs)[-1]:
                    sides = [getattr(item, name) for name in side_names(inputs)]
                    item.status = 'FAILED' if any(s['callStatus'] == 'FAILED' for s in sides) else 'COMPLETED'
                    row.completed_questions += 1
                    row.failed_questions += int(item.status == 'FAILED')
                db.commit()
    with factory() as db:
        row = locked_run(db, id_, token)
        if row:
            finish(db, row)
            db.commit()
    return True


async def run(once=False):
    """Stop after the current question on SIGTERM and mark uncertain remaining work explicitly."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    while not stop.is_set():
        try:
            worked = await process_one(stop_event=stop)
        except OperationalError:
            logging.getLogger(__name__).error('Evaluation worker database temporarily unavailable')
            if once:
                raise
            await asyncio.sleep(1)
            continue
        if once:
            return
        if not worked:
            await asyncio.sleep(0.5)


def main():
    """Start the durable evaluation worker using the same DB and encryption key as the API."""
    parser = argparse.ArgumentParser(description='Osori A/B 평가 워커')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    asyncio.run(run(args.once))


if __name__ == '__main__':
    main()
