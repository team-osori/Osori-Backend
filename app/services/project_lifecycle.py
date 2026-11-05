from datetime import timedelta

from sqlalchemy import delete, select, update

from app.models.connection import Connection, ConnectionTest
from app.models.evaluation import Evaluation, EvaluationItem, EvaluationReview
from app.models.policy import GenerationRequest, Policy, PolicyVersion, QuestionHistory, TestQuestion, TestSet, TestSetVersion
from app.models.project import Chatbot, Project, ProjectMember
from app.models.prompt import Prompt, PromptVersion
from app.services.auth import now
from app.services.evaluation_control import cancel_run

RETENTION_DAYS = 30


def trash_project(db, project):
    project.deleted_at, project.updated_at, project.revision = now(), now(), project.revision + 1
    service_ids = select(Chatbot.id).where(Chatbot.project_id == project.id)
    runs = db.scalars(select(Evaluation).where(Evaluation.service_id.in_(service_ids),
        Evaluation.status.in_(('QUEUED', 'RUNNING'))).with_for_update()).all()
    for run in runs:
        cancel_run(db, run)
    db.execute(update(ConnectionTest).where(ConnectionTest.service_id.in_(service_ids),
        ConnectionTest.status.in_(('QUEUED', 'RUNNING'))).values(status='FAILED', finished_at=now(),
        error={'code': 'PROJECT_DELETED', 'message': '프로젝트가 휴지통으로 이동되었습니다.'}))


def purge_expired_projects(db):
    cutoff = now() - timedelta(days=RETENTION_DAYS)
    projects = db.scalars(select(Project).where(Project.deleted_at <= cutoff).with_for_update()).all()
    for project in projects:
        services = select(Chatbot.id).where(Chatbot.project_id == project.id)
        evaluations = select(Evaluation.id).where(Evaluation.service_id.in_(services))
        items = select(EvaluationItem.id).where(EvaluationItem.evaluation_id.in_(evaluations))
        prompts = select(Prompt.id).where(Prompt.service_id.in_(services))
        testsets = select(TestSet.id).where(TestSet.project_id == project.id)
        versions = select(TestSetVersion.id).where(TestSetVersion.testset_id.in_(testsets))
        policies = select(Policy.id).where(Policy.project_id == project.id)
        for model, condition in (
            (EvaluationReview, EvaluationReview.item_id.in_(items)),
            (EvaluationItem, EvaluationItem.evaluation_id.in_(evaluations)),
            (Evaluation, Evaluation.service_id.in_(services)),
            (ConnectionTest, ConnectionTest.service_id.in_(services)),
            (PromptVersion, PromptVersion.prompt_id.in_(prompts)),
            (Prompt, Prompt.service_id.in_(services)),
            (Connection, Connection.service_id.in_(services)),
            (Chatbot, Chatbot.project_id == project.id),
            (GenerationRequest, GenerationRequest.response['data']['id'].as_string().in_(versions)),
            (QuestionHistory, QuestionHistory.testset_version_id.in_(versions)),
            (TestQuestion, TestQuestion.testset_version_id.in_(versions)),
            (TestSetVersion, TestSetVersion.testset_id.in_(testsets)),
            (TestSet, TestSet.project_id == project.id),
            (PolicyVersion, PolicyVersion.policy_id.in_(policies)),
            (Policy, Policy.project_id == project.id),
            (ProjectMember, ProjectMember.project_id == project.id),
        ):
            db.execute(delete(model).where(condition).execution_options(synchronize_session=False))
        db.delete(project)
    return len(projects)
