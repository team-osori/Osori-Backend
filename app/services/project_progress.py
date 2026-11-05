"""Batch project progress without fetching historical snapshots or answer bodies."""
from collections import Counter

from sqlalchemy import case, func, select

from app.models.evaluation import Evaluation, EvaluationItem
from app.models.policy import TestQuestion, TestSet, TestSetVersion
from app.models.project import Chatbot
from app.schemas.project import ProjectProgress
from app.services.evaluation_reviews import latest_reviews
from app.services.policy import iso


def project_progress(db, project_ids):
    result = {project_id: ProjectProgress().model_dump() for project_id in project_ids}
    if not result:
        return result
    # Window functions keep a single latest row while retaining historical unlocks.
    versions = select(TestSet.project_id, TestSetVersion.id, TestSetVersion.version, TestSetVersion.status,
        func.row_number().over(partition_by=TestSet.project_id,
            order_by=(TestSetVersion.created_at.desc(), TestSetVersion.version.desc(), TestSetVersion.id.desc())).label('rank'),
        func.max(case((TestSetVersion.status == 'confirmed', 1), else_=0))
            .over(partition_by=TestSet.project_id).label('confirmed'))\
        .join(TestSet, TestSet.id == TestSetVersion.testset_id).where(TestSet.project_id.in_(project_ids), TestSet.deleted.is_(False)).subquery()
    latest_versions = db.execute(select(versions).where(versions.c.rank == 1)).all()
    counts = dict(db.execute(select(TestQuestion.testset_version_id, func.count(TestQuestion.id))
        .where(TestQuestion.testset_version_id.in_([row.id for row in latest_versions]))
        .group_by(TestQuestion.testset_version_id)).all()) if latest_versions else {}
    for row in latest_versions:
        progress = result[row.project_id]
        progress['testSet'] = {'versionId': row.id, 'label': f'v{row.version}',
            'status': row.status, 'questionCount': counts.get(row.id, 0)}
        if row.confirmed:
            progress.update(stage=2, unlockedStage=2, status='RUNNING')

    evaluations = select(Chatbot.project_id, Evaluation.id, Evaluation.status,
        Evaluation.total_questions, Evaluation.completed_questions,
        Evaluation.inputs['mode'].as_string().label('mode'),
        Evaluation.inputs['prompts']['a']['version'].as_integer().label('prompt_version'),
        func.row_number().over(partition_by=Chatbot.project_id,
            order_by=(Evaluation.created_at.desc(), Evaluation.id.desc())).label('rank'),
        func.max(case((Evaluation.status.in_(('COMPLETED', 'PARTIAL_FAILED')), 1), else_=0))
            .over(partition_by=Chatbot.project_id).label('finished'),
        func.max(Evaluation.finished_at).over(partition_by=Chatbot.project_id).label('last_evaluated_at'))\
        .join(Chatbot, Chatbot.id == Evaluation.service_id).where(Chatbot.project_id.in_(project_ids)).subquery()
    latest_evaluations = db.execute(select(evaluations).where(evaluations.c.rank == 1)).all()
    by_id = {row.id: row for row in latest_evaluations}
    # At most 200 question inputs per latest evaluation; never load answer/secret columns.
    items = db.execute(select(EvaluationItem.id, EvaluationItem.evaluation_id,
        EvaluationItem.input['expectedConditions'].label('conditions'))
        .where(EvaluationItem.evaluation_id.in_(by_id))).all() if by_id else []
    reviews = latest_reviews(db, [item.id for item in items]) if items else {}
    reviewed = Counter()
    for item in items:
        conditions = item.conditions or []
        sides = ('a',) if by_id[item.evaluation_id].mode == 'SINGLE' else ('a', 'b')
        if conditions and all(reviews.get((item.id, side, condition['id']), {}).get('verdict') in ('pass', 'fail')
                              for side in sides for condition in conditions):
            reviewed[item.evaluation_id] += 1
    for row in latest_evaluations:
        progress = result[row.project_id]
        progress['unlockedStage'] = max(progress['unlockedStage'], 3 if row.finished else 2)
        done = row.status in ('COMPLETED', 'PARTIAL_FAILED')
        progress.update(stage=3 if done else 2, status='DONE' if done else 'RUNNING',
            lastEvaluatedAt=iso(row.last_evaluated_at), promptVersion=row.prompt_version,
            evaluation={'id': row.id, 'status': row.status, 'completedQuestions': row.completed_questions,
                'totalQuestions': row.total_questions, 'reviewedQuestions': reviewed[row.id]})
    return result
