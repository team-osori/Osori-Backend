from email_helpers import verified_post
import base64
import json

import pytest


def encode(payload):
    """Build hostile client cursors independently of the server's encoder."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture
def cursor_endpoints(db_and_client):
    """Create both kinds of paginated resources under an authenticated project."""
    _, client = db_and_client
    credentials = {'email': 'cursor_owner@example.com', 'password': 'secure-password-123'}
    verified_post(client, '/api/auth/signup', json={**credentials, 'name': 'Test'})
    token = client.post('/api/auth/login', json=credentials).json()['data']['accessToken']
    headers = {'Authorization': 'Bearer ' + token}
    project = client.post('/api/projects', json={'name': 'Test'}, headers=headers).json()['data']['id']
    base = f'/api/projects/{project}'
    source = client.post(base + '/policies', json={'name': 'Policy', 'content': '원문',
        'rules': [{'key': 'rule', 'description': '규칙', 'sourceQuote': '원문'}]}, headers=headers).json()['data']
    testset = client.post(base + '/testsets', json={'name': 'Test', 'policyVersionIds': [source['version']['id']]}, headers=headers).json()['data']
    versions = [(base + '/policies/' + source['policy']['id'] + '/versions', 'policyVersions:' + source['policy']['id']),
                (base + '/testsets/' + testset['testSet']['id'] + '/versions', 'testsetVersions:' + testset['testSet']['id'])]
    lists = [(base + '/policies', 'policies:' + project), (base + '/testsets', 'testsets:' + project)]
    return client, headers, versions, lists, source['version']['id']


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan'), 10**100, 2**31, 0, -1, 1.5, True, None, '1', [], {}])
def test_version_cursor_rejects_non_integer_or_out_of_range(cursor_endpoints, value):
    """Invalid version values return the documented error instead of coercion or DB overflow."""
    client, headers, versions, _, id_ = cursor_endpoints
    for path, scope in versions:
        response = client.get(path, params={'cursor': encode({'v': 1, 'scope': scope, 'id': id_, 'value': value})}, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'INVALID_CURSOR'


def test_exponent_overflow_cursor(cursor_endpoints):
    """Reproduce the reported JSON 1e999 overflow for both version-list APIs."""
    client, headers, versions, _, id_ = cursor_endpoints
    for path, scope in versions:
        raw = json.dumps({'v': 1, 'scope': scope, 'id': id_, 'value': 'replace-me'}).replace('"replace-me"', '1e999')
        cursor = base64.urlsafe_b64encode(raw.encode()).decode()
        response = client.get(path, params={'cursor': cursor}, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'INVALID_CURSOR'


@pytest.mark.parametrize('value', ['0001-01-01T00:00:00', '2026-01-01T00:00:00+00:00', '2026-01-01', 'invalid', 1, None])
def test_timestamp_cursor_validates_mysql_domain(cursor_endpoints, value):
    """Timestamp cursors must use the server's canonical UTC-naive database format."""
    client, headers, _, lists, id_ = cursor_endpoints
    for path, scope in lists:
        response = client.get(path, params={'cursor': encode({'v': 1, 'scope': scope, 'id': id_, 'value': value})}, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'INVALID_CURSOR'


@pytest.mark.parametrize('patch', [{'v': True}, {'v': 1.0}, {'id': ''}, {'id': 'x' * 101}, {'id': 'bad\x00id'}, {'scope': 'wrong'}, {'extra': 1}])
def test_cursor_envelope_rejects_invalid_metadata(cursor_endpoints, patch):
    """Validate cursor structure, format version, scope and ID before building SQL."""
    client, headers, versions, _, id_ = cursor_endpoints
    for path, scope in versions:
        payload = {'v': 1, 'scope': scope, 'id': id_, 'value': 1, **patch}
        response = client.get(path, params={'cursor': encode(payload)}, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'INVALID_CURSOR'


@pytest.mark.parametrize('raw', ['null', '[]', '{}', '{', '{"v":1,"v":1}', '"string"'])
def test_malformed_cursor_envelopes(cursor_endpoints, raw):
    """Malformed or duplicate-member JSON cannot escape common cursor validation."""
    client, headers, versions, _, _ = cursor_endpoints
    cursor = base64.urlsafe_b64encode(raw.encode()).decode()
    for path, _ in versions:
        response = client.get(path, params={'cursor': cursor}, headers=headers)
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'INVALID_CURSOR'


def test_valid_version_cursor_boundaries(cursor_endpoints):
    """Retain support for properly typed cursor values at both MySQL INT boundaries."""
    client, headers, versions, _, id_ = cursor_endpoints
    for value in (1, 2**31 - 1):
        for path, scope in versions:
            response = client.get(path, params={'cursor': encode({'v': 1, 'scope': scope, 'id': id_, 'value': value})}, headers=headers)
            assert response.status_code == 200, response.text


def test_oversized_revision_rejected_before_database_binding(cursor_endpoints):
    """Huge mutation revisions are invalid input, not a database integer-overflow error."""
    client, headers, versions, _, _ = cursor_endpoints
    path = versions[0][0]
    response = client.post(path, json={'expectedRevision': 10**100, 'content': '원문',
        'rules': [{'key': 'rule', 'description': '규칙', 'sourceQuote': '원문'}]}, headers=headers)
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'


@pytest.mark.parametrize('value', [True, 1.5, '1', 0, -1, 2**31, 10**100])
def test_all_revision_bodies_share_strict_int_bounds(value):
    """All draft mutations and policy-version writes share one bounded revision contract."""
    from pydantic import ValidationError
    from app.schemas.policy import Generate, NewDraft, PolicyVersionCreate, QuestionWrite, Review, Revision
    for model in (Revision, Generate, NewDraft, Review, QuestionWrite, PolicyVersionCreate):
        with pytest.raises(ValidationError) as error:
            model.model_validate({'expectedRevision': value})
        assert any(item['loc'] == ('expectedRevision',) for item in error.value.errors())


def test_server_issued_timestamp_cursors_round_trip(cursor_endpoints):
    """Stricter validation must preserve normal policy and test-set pagination."""
    client, headers, _, lists, policy_version_id = cursor_endpoints
    policy_path = lists[0][0]
    client.post(policy_path, json={'name': 'Second', 'content': '원문',
        'rules': [{'key': 'rule', 'description': '규칙', 'sourceQuote': '원문'}]}, headers=headers)
    client.post(lists[1][0], json={'name': 'Second', 'policyVersionIds': [policy_version_id]}, headers=headers)
    for path, _ in lists:
        first = client.get(path, params={'limit': 1}, headers=headers).json()['data']
        assert first['nextCursor'] is not None
        second = client.get(path, params={'limit': 1, 'cursor': first['nextCursor']}, headers=headers)
        assert second.status_code == 200, second.text
        page = second.json()['data']
        assert page['nextCursor'] is None
        assert page['items'][0]['id'] != first['items'][0]['id']
