from datetime import timedelta

from app.models.auth import User
from app.services.auth import now
from test_projects import register


def test_project_invitation_accept_roles_and_revocation(db_and_client):
    factory, client = db_and_client
    owner = register(client, 'member_owner')
    member = register(client, 'member_user')
    outsider = register(client, 'member_other')
    project = client.post('/api/projects', headers=owner, json={'name': 'Shared'}).headers['location']
    member_id = client.get('/api/auth/me', headers=member).json()['data']['id']
    owner_id = client.get('/api/auth/me', headers=owner).json()['data']['id']
    invite = client.post(project + '/invitations', headers=owner, json={'email': 'member_user@example.com'})
    assert invite.status_code == 201, invite.text
    invitation = invite.json()['data']
    assert client.get(project, headers=member).status_code == 404
    assert client.get('/api/projects', headers=member).json()['data']['items'] == []
    assert client.get('/api/invitations', headers=member).json()['data']['items'] == [invitation]
    endpoint = '/api/invitations/' + invitation['id'] + '/accept'
    assert client.post(endpoint, headers=outsider, json={'expectedRevision': 1}).status_code == 404
    accepted = client.post(endpoint, headers=member, json={'expectedRevision': 1})
    assert accepted.status_code == 200 and accepted.json()['data']['role'] == 'member'
    assert client.get(project, headers=member).status_code == 200
    assert client.get('/api/projects', headers=member).json()['data']['items'][0]['id'] == invitation['projectId']
    assert client.get('/api/auth/me', headers=member).json()['data']['projectRoles'] == [{'projectId': invitation['projectId'], 'role': 'member'}]
    assert client.patch(project, headers=member, json={'expectedRevision': 1, 'name': 'no'}).status_code == 403
    assert client.post(project + '/invitations', headers=member, json={'email': 'member_other@example.com'}).status_code == 403
    assert client.post(project + '/invitations', headers=owner, json={'email': 'member_user@example.com'}).status_code == 409
    member_path = project + '/members/' + member_id
    assert client.patch(member_path, headers=owner, json={'expectedRevision': 1, 'role': 'admin'}).status_code == 409
    assert client.patch(member_path, headers=owner, json={'expectedRevision': 2, 'role': 'admin'}).status_code == 200
    assert client.patch(project, headers=member, json={'expectedRevision': 1, 'name': 'shared renamed'}).status_code == 200
    assert client.delete(project + '/members/' + owner_id, headers={**member, 'If-Match': '"1"'}).status_code == 409
    assert client.delete(member_path, headers={**owner, 'If-Match': '"2"'}).status_code == 412
    assert client.delete(member_path, headers={**owner, 'If-Match': '"3"'}).status_code == 204
    assert client.get(project, headers=member).status_code == 404
    assert client.get('/api/auth/me', headers=member).json()['data']['projectRoles'] == []
    with factory() as db:
        user = db.get(User, owner_id)
        user.last_seen_at = now() - timedelta(days=2)
        db.commit()
    me = client.get('/api/auth/me', headers=owner).json()['data']
    assert me['email'] == 'member_owner@example.com' and me['lastSeenAt']
    roster = client.get(project + '/members', headers=owner).json()['data']['items']
    assert len(roster) == 1 and roster[0]['status'] == 'owner'
    assert roster[0]['lastSeenAt'] == me['lastSeenAt']


def test_invitations_are_hidden_after_project_deleted(db_and_client):
    _, client = db_and_client
    owner = register(client, 'trash_owner')
    member = register(client, 'trash_member')
    project = client.post('/api/projects', headers=owner, json={'name': 'Shared'}).headers['location']
    invitation = client.post(project + '/invitations', headers=owner, json={'email': 'trash_member@example.com'}).json()['data']
    assert client.delete(project, headers={**owner, 'If-Match': '"1"'}).status_code == 204
    assert client.get('/api/invitations', headers=member).json()['data']['items'] == []
    assert client.post('/api/invitations/' + invitation['id'] + '/accept', headers=member,
        json={'expectedRevision': 1}).status_code == 404
