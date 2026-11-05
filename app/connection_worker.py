"""Durable DB-backed connection-test worker: python -m app.connection_worker."""
import argparse
import asyncio
from copy import deepcopy
from datetime import timedelta
import logging
import signal

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.core.concurrency import is_write_conflict
from app.core.secrets import decrypt_secret
from app.models import auth, project, prompt  # noqa: F401: register referenced tables
from app.models.connection import Connection, ConnectionTest
from app.models.project import Chatbot, Project
from app.services.auth import now
from app.services.chatbot_gateway import execute_chatbot, GatewayResult
from app.services.prompts import get_version

# 3 attempts * 60 seconds + retry backoff fits within this execution lease.
LEASE_SECONDS = 240


def expire_abandoned_jobs(factory):
    """Never replay uncertain calls after a crash; record an explicit terminal failure."""
    with factory() as db:
        db.execute(update(ConnectionTest).where(ConnectionTest.status == 'RUNNING',
            ConnectionTest.started_at < now() - timedelta(seconds=LEASE_SECONDS)).values(
            status='FAILED', finished_at=now(), error={'code': 'WORKER_INTERRUPTED',
            'message': '작업 실행이 중단되었습니다. 새 멱등키로 다시 테스트해 주세요.'}))
        db.commit()


async def process_one(factory=SessionLocal, *, sender=None) -> bool:
    """Claim with compare-and-swap so multiple workers cannot execute one job twice."""
    expire_abandoned_jobs(factory)
    with factory() as db:
        job_id = db.scalar(select(ConnectionTest.id).join(Chatbot, Chatbot.id == ConnectionTest.service_id).join(Project, Project.id == Chatbot.project_id).where(ConnectionTest.status == 'QUEUED', Project.deleted_at.is_(None))
                           .order_by(ConnectionTest.created_at, ConnectionTest.id).limit(1))
        if job_id is None:
            return False
        try:
            claimed = db.execute(update(ConnectionTest).where(ConnectionTest.id == job_id,
                ConnectionTest.status == 'QUEUED').values(status='RUNNING', started_at=now()))
        except OperationalError as exc:
            db.rollback()
            if is_write_conflict(exc):
                return True
            raise
        db.commit()
        if claimed.rowcount != 1:
            return True
    try:
        with factory() as db:
            job = db.get(ConnectionTest, job_id)
            if job is None or job.status != 'RUNNING':
                return True
            c = db.get(Connection, job.connection_id)
            if c is None or c.revision != job.connection_revision:
                raise ApiError(409, 'CONNECTION_CHANGED', '접수 후 연동 설정이 변경되었습니다.')
            config = deepcopy(c.config)
            secret = decrypt_secret(c.secret_ciphertext) if c.secret_ciphertext else None
            content = get_version(db, job.service_id, job.prompt_version_id, allow_deleted=True).content if job.prompt_version_id else None
            question = job.question
        result = await execute_chatbot(config, question, content, secret, sender=sender)
    except ApiError as exc:
        result = GatewayResult(None, 0, 0, {'code': exc.code, 'message': exc.message})
    except Exception as exc:
        # Neither request/response contents nor exception messages are logged.
        logging.getLogger(__name__).error('Connection test failed jobId=%s exceptionType=%s', job_id, type(exc).__name__)
        result = GatewayResult(None, 0, 0, {'code': 'INTERNAL_ERROR', 'message': '연결 테스트 실행 중 오류가 발생했습니다.'})
    with factory() as db:
        job = db.get(ConnectionTest, job_id)
        terminal = db.execute(update(ConnectionTest).where(ConnectionTest.id == job_id,
            ConnectionTest.status == 'RUNNING').values(status='FAILED' if result.error else 'SUCCEEDED',
            answer=result.answer, error=result.error, latency_ms=result.latency_ms,
            attempt_count=result.attempt_count, finished_at=now()))
        if terminal.rowcount == 1 and result.error is None:
            db.execute(update(Connection).where(Connection.id == job.connection_id,
                Connection.revision == job.connection_revision).values(verified_revision=job.connection_revision))
        db.commit()
    return True


async def run(once=False, stop_event=None):
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            worked = await process_one()
        except OperationalError:
            logging.getLogger(__name__).error('Connection worker database temporarily unavailable')
            if once:
                raise SystemExit(1) from None
            await asyncio.sleep(1)
            continue
        if once:
            return
        if not worked:
            await asyncio.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description='Osori 연결 테스트 작업 실행')
    parser.add_argument('--once', action='store_true', help='대기 작업을 최대 한 건 처리하고 종료')
    args = parser.parse_args()
    try:
        asyncio.run(run_with_signals(args.once))
    except KeyboardInterrupt:
        pass


async def run_with_signals(once=False):
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop_event.set)
    await run(once, stop_event)


if __name__ == '__main__':
    main()
