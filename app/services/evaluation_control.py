"""Cancel durable jobs and resume only work proven not to have called the company API."""
from sqlalchemy import select

from app.core.errors import ApiError
from app.models.connection import Connection
from app.models.evaluation import EvaluationItem
from app.services.auth import now
from app.services.policy import uid


def cancel_run(db, run):
    if run.status == 'CANCELLED':
        return
    if run.status not in ('QUEUED', 'RUNNING'):
        raise ApiError(409, 'EVALUATION_NOT_ACTIVE', '대기 또는 실행 중인 평가만 중단할 수 있습니다.')
    from app.evaluation_worker import close_interrupted
    close_interrupted(db, run, 'USER_CANCELLED')
    run.status, run.lease_token, run.finished_at = 'CANCELLED', uid(), now()
    items = db.scalars(select(EvaluationItem).where(EvaluationItem.evaluation_id == run.id)).all()
    run.completed_questions = sum(item.status in ('COMPLETED', 'FAILED') for item in items)
    run.failed_questions = sum(item.status == 'FAILED' for item in items)


def resume_run(db, run, connection_revision, judge):
    from app.services.evaluations import side_names
    if run.status != 'CANCELLED':
        raise ApiError(409, 'EVALUATION_NOT_CANCELLED', '중단한 평가만 이어서 실행할 수 있습니다.')
    connection = db.scalar(select(Connection).where(Connection.service_id == run.service_id).with_for_update())
    if (connection is None or connection.revision != connection_revision
            or connection.verified_revision != connection_revision
            or connection_revision != run.inputs['connection']['revision']
            or connection.config != run.inputs['connection']['config']
            or judge.descriptor() != run.inputs['judge']):
        raise ApiError(409, 'COMPARISON_CONFIGURATION_CHANGED', '접수 당시 연동 설정·인증정보·판정 모델로만 재개할 수 있습니다.')
    items = db.scalars(select(EvaluationItem).where(EvaluationItem.evaluation_id == run.id).with_for_update()).all()
    pending = 0
    for item in items:
        if any(getattr(item, side) is None or getattr(item, side)['callStatus'] == 'NOT_RUN' for side in side_names(run.inputs)):
            item.status = 'QUEUED'
            pending += 1
    if not pending:
        raise ApiError(409, 'NO_PENDING_QUESTIONS', '미호출 문항이 없습니다. 결과가 불확실한 호출은 자동 재실행하지 않습니다.')
    run.status, run.finished_at, run.lease_token = 'QUEUED', None, uid()
    run.secret_ciphertext = connection.secret_ciphertext
    run.completed_questions = sum(item.status in ('COMPLETED', 'FAILED', 'INTERRUPTED') for item in items)
    run.failed_questions = sum(item.status in ('FAILED', 'INTERRUPTED') for item in items)
