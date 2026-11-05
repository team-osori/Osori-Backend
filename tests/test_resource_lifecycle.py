from datetime import timedelta

from sqlalchemy import func, select

from app.maintenance import cleanup_expired
from app.models.project import Project
from app.models.prompt import PromptVersion
from app.models.policy import TestSetVersion as SetVersion
from test_evaluations import prepare, submit
from test_projects import register


def exercise_project_trash(factory, client, monkeypatch):
    headers, service, body, _ = prepare(factory, client, monkeypatch)
    project = service.split('/services/')[0]
    path = submit(client, headers, service, body)
    assert client.delete(project, headers=headers).status_code == 428
    assert client.delete(project, headers={**headers, 'If-Match': '"2"'}).status_code == 412
    assert client.delete(project, headers={**headers, 'If-Match': '"1"'}).status_code == 204
    assert client.get(project, headers=headers).status_code == 404
    assert client.get(path, headers=headers).status_code == 404
    assert client.get('/api/projects', headers=headers).json()['data']['items'] == []
    trash = client.get('/api/projects/trash', headers=headers).json()['data']['items'][0]
    assert trash['revision'] == 2 and trash['deletedAt'] and trash['purgeAfter']
    other = register(client, 'trash_intruder')
    assert client.post(project + '/restore', headers=other, json={'expectedRevision': 2}).status_code == 404
    restored = client.post(project + '/restore', headers=headers, json={'expectedRevision': 2})
    assert restored.status_code == 200, restored.text
    assert restored.json()['data']['revision'] == 3
    assert client.get(path, headers=headers).json()['data']['status'] == 'CANCELLED'
    assert client.delete(project, headers={**headers, 'If-Match': '"3"'}).status_code == 204
    with factory() as db:
        assert cleanup_expired(db)['projects'] == 0
        row = db.get(Project, project.rsplit('/', 1)[1])
        row.deleted_at -= timedelta(days=31)
        db.commit()
    assert client.post(project + '/restore', headers=headers, json={'expectedRevision': 4}).status_code == 409
    with factory() as db:
        prompt_count = db.scalar(select(func.count()).select_from(PromptVersion))
        testset_count = db.scalar(select(func.count()).select_from(SetVersion))
        assert cleanup_expired(db)['projects'] == 1
        assert db.get(PromptVersion, body['aPromptVersionId']) is None
        assert db.get(PromptVersion, body['bPromptVersionId']) is None
        assert db.get(SetVersion, body['testSetVersionId']) is None
        assert db.scalar(select(func.count()).select_from(PromptVersion)) == prompt_count - 2
        assert db.scalar(select(func.count()).select_from(SetVersion)) == testset_count - 1
    assert client.get('/api/projects/trash', headers=headers).json()['data']['items'] == []


def test_project_trash_restore_and_permanent_cleanup(db_and_client, monkeypatch):
    factory, client = db_and_client
    exercise_project_trash(factory, client, monkeypatch)


def test_prompt_testset_rename_archive_preserve_snapshots(db_and_client, monkeypatch):
    factory, client = db_and_client
    headers, service, body, _ = prepare(factory, client, monkeypatch)
    project = service.split('/services/')[0]
    run = submit(client, headers, service, body)
    frozen = client.get(run + '/inputs', headers=headers).json()['data']
    prompt = client.get(service + '/prompts', headers=headers).json()['data']['items'][0]
    ppath = service + '/prompts/' + prompt['id']
    renamed = client.patch(ppath, headers=headers, json={'expectedRevision': prompt['revision'], 'name': 'renamed'})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()['data']['currentVersionId'] == prompt['currentVersionId']
    assert client.get(service + '/prompts', headers=headers).json()['data']['items'][0]['version'] == prompt['version']
    assert client.delete(ppath, headers={**headers, 'If-Match': '"' + str(prompt['revision'] + 1) + '"'}).status_code == 204
    assert client.get(ppath, headers=headers).status_code == 404
    assert client.get(service + '/prompts', headers=headers).json()['data']['items'] == []
    testset = client.get(project + '/testsets', headers=headers).json()['data']['items'][0]
    tpath = project + '/testsets/' + testset['id']
    assert client.patch(tpath, headers=headers, json={'expectedRevision': 1, 'name': 'renamed'}).status_code == 200
    draft = client.post(tpath + '/versions', headers=headers, json={'expectedRevision': 2, 'sourceVersionId': body['testSetVersionId']})
    assert draft.status_code == 201 and draft.json()['data']['version'] == 2
    assert client.delete(tpath, headers={**headers, 'If-Match': '"3"'}).status_code == 204
    assert client.get(tpath, headers=headers).status_code == 404
    assert client.get(project + '/testsets', headers=headers).json()['data']['items'] == []
    assert client.get(run + '/inputs', headers=headers).json()['data'] == frozen
