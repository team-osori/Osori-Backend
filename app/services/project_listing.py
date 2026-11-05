"""Batch list projections; never return credentials, snapshots or answer bodies."""
from collections import defaultdict
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy import case, exists, func, or_, select

from app.models.connection import Connection, ConnectionTest
from app.models.evaluation import Evaluation, EvaluationItem
from app.models.policy import TestSet, TestSetVersion
from app.models.project import Chatbot, Project, ProjectMember
from app.services.evaluations import criterion_summary
from app.services.policy import iso


def accessible_projects(user_id):
    membership = exists().where(ProjectMember.project_id == Project.id,
        ProjectMember.user_id == user_id, ProjectMember.status == 'active')
    return select(Project).where(Project.deleted_at.is_(None), or_(Project.owner_id == user_id, membership))


def project_status():
    latest = (select(Evaluation.status).join(Chatbot, Chatbot.id == Evaluation.service_id)
        .where(Chatbot.project_id == Project.id).order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
        .limit(1).correlate(Project).scalar_subquery())
    confirmed = exists().where(TestSet.project_id == Project.id, TestSet.deleted.is_(False),
        TestSetVersion.testset_id == TestSet.id, TestSetVersion.status == 'confirmed')
    return case((latest.in_(('COMPLETED', 'PARTIAL_FAILED')), 'DONE'),
        (or_(latest.is_not(None), confirmed), 'RUNNING'), else_='SETUP')


def latest_connection_tests(db, service_ids):
    if not service_ids:
        return {}
    ranked = select(ConnectionTest.id,
        func.row_number().over(partition_by=ConnectionTest.service_id,
            order_by=(ConnectionTest.created_at.desc(), ConnectionTest.id.desc())).label('rank'))\
        .where(ConnectionTest.service_id.in_(service_ids)).subquery()
    return {job.service_id: job for job in db.scalars(select(ConnectionTest)
        .join(ranked, ranked.c.id == ConnectionTest.id).where(ranked.c.rank == 1))}


def list_details(db, projects):
    details = {p.id: {'service': None, 'latestEvaluation': None} for p in projects}
    if not details:
        return details
    services = db.execute(select(Chatbot, Connection.id, Connection.revision,
        Connection.verified_revision, Connection.config['url'].as_string().label('url'))
        .outerjoin(Connection, Connection.service_id == Chatbot.id)
        .where(Chatbot.project_id.in_(details))).all()
    for service, connection_id, revision, verified, url in services:
        details[service.project_id]['service'] = {'id': service.id, 'name': service.name,
            'revision': service.revision, 'hasConnection': connection_id is not None,
            'endpointHost': urlsplit(url).hostname if url else None,
            'connectionRevision': revision, 'connectionVerified': revision is not None and revision == verified}
    ranked = select(Evaluation.id, Chatbot.project_id,
        func.row_number().over(partition_by=Chatbot.project_id,
            order_by=(Evaluation.created_at.desc(), Evaluation.id.desc())).label('rank'))\
        .join(Chatbot, Chatbot.id == Evaluation.service_id).where(Chatbot.project_id.in_(details)).subquery()
    runs = db.execute(select(Evaluation.id, ranked.c.project_id, Evaluation.status,
        Evaluation.created_at, Evaluation.finished_at,
        Evaluation.inputs['mode'].as_string().label('mode'),
        Evaluation.inputs['criteria'].label('criteria'))
        .join(ranked, ranked.c.id == Evaluation.id).where(ranked.c.rank == 1)).all()
    grouped = defaultdict(list)
    if runs:
        # Explicit JSON projections keep arbitrarily large answer/evidence bodies out of list queries.
        rows = db.execute(select(EvaluationItem.evaluation_id,
            EvaluationItem.a['callStatus'].as_string().label('a_status'),
            EvaluationItem.a['criteria'].label('a_criteria'),
            EvaluationItem.b['callStatus'].as_string().label('b_status'),
            EvaluationItem.b['criteria'].label('b_criteria'))
            .where(EvaluationItem.evaluation_id.in_([r.id for r in runs]))).all()
        for row in rows:
            grouped[row.evaluation_id].append(SimpleNamespace(
                a={'callStatus': row.a_status, 'criteria': row.a_criteria} if row.a_status else None,
                b={'callStatus': row.b_status, 'criteria': row.b_criteria} if row.b_status else None))
    for run in runs:
        details[run.project_id]['latestEvaluation'] = {'id': run.id, 'status': run.status,
            'mode': run.mode or 'COMPARE', 'criteriaVersion': run.criteria['version'],
            'createdAt': iso(run.created_at), 'finishedAt': iso(run.finished_at),
            'provisional': run.status not in ('COMPLETED', 'PARTIAL_FAILED', 'FAILED', 'CANCELLED'),
            'criteria': criterion_summary({'mode': run.mode, 'criteria': run.criteria}, grouped[run.id])}
    return details
