import base64
import json
from uuid import uuid4

import pytest

from test_projects import register


def encode(value):
    """Construct client-controlled envelopes independently of server pagination code."""
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def prepare_cursor_resources(client):
    """Provide two entries for each original pagination wire format under one distinct owner."""
    headers = register(client, 'cursor' + uuid4().hex[:15])
    owner = client.get('/api/auth/me', headers=headers).json()['data']['id']
    project = client.post('/api/projects', headers=headers, json={'name': 'First'}).headers['location']
    client.post('/api/projects', headers=headers, json={'name': 'Second'})
    service = client.post(project + '/services', headers=headers, json={'name': 'Chatbot'}).headers['location']
    prompt = client.post(service + '/prompts', headers=headers, json={'name': 'First', 'content': 'A'}).json()['data']
    client.post(service + '/prompts', headers=headers, json={'name': 'Second', 'content': 'Other'})
    client.post(service + '/prompts/' + prompt['id'] + '/versions', headers=headers,
        json={'baseVersionId': prompt['currentVersionId'], 'content': 'B'})
    endpoints = [('/api/projects', {'v': 1, 'owner': owner, 'createdAt': '2026-11-05T00:00:00', 'id': 'valid-id'}),
        (service + '/prompts', {'v': 1, 'scope': 'prompts:' + service.rsplit('/', 1)[1],
            'createdAt': '2026-11-05T00:00:00', 'id': 'valid-id'})]
    versions = (service + '/prompts/' + prompt['id'] + '/versions', {'promptId': prompt['id'], 'before': 2})
    return headers, endpoints, versions


@pytest.fixture
def resources(db_and_client):
    """Use a separate memory database for every hostile cursor case."""
    _, client = db_and_client
    return client, *prepare_cursor_resources(client)


def assert_invalid(client, headers, path, value):
    """An invalid cursor must fail before the DB driver tries to encode or bind it."""
    response = client.get(path, headers=headers, params={'cursor': encode(value)})
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == 'INVALID_CURSOR'


@pytest.mark.parametrize('id_', ['\ud800', '\udfff', 'bad\x00id', '', 'x' * 101, True])
def test_project_and_prompt_cursor_ids_are_safe(resources, id_):
    """Reject nested lone surrogates and unsupported identifier values on both listing APIs."""
    client, headers, endpoints, _ = resources
    for path, value in endpoints:
        assert_invalid(client, headers, path, {**value, 'id': id_})


@pytest.mark.parametrize('value', ['0001-01-01T00:00:00', '2026-11-05', '2026-11-05T00:00:00+00:00'])
def test_all_timestamp_cursors_use_the_mysql_domain(resources, value):
    """Do not let old list APIs accept dates rejected by policy and evaluation pagination."""
    client, headers, endpoints, _ = resources
    for path, data in endpoints:
        assert_invalid(client, headers, path, {**data, 'createdAt': value})


@pytest.mark.parametrize('value', [2**31, 10**1000, True, 1.5, '2', 0])
def test_prompt_version_cursor_rejects_invalid_integers(resources, value):
    """Reject integer overflow consistently on SQLite and MySQL instead of coercing it."""
    client, headers, _, (path, data) = resources
    assert_invalid(client, headers, path, {**data, 'before': value})


def test_cursor_metadata_and_duplicate_members(resources):
    """Reject boolean format versions and duplicate keys in otherwise valid envelopes."""
    client, headers, endpoints, versions = resources
    for path, value in endpoints:
        assert_invalid(client, headers, path, {**value, 'v': True})
    for path, value in [*endpoints, versions]:
        field = 'id' if 'id' in value else 'before'
        raw = json.dumps(value)[:-1] + ',' + json.dumps(field) + ':' + json.dumps(value[field]) + '}'
        response = client.get(path, headers=headers, params={'cursor': base64.urlsafe_b64encode(raw.encode()).decode()})
        assert response.status_code == 400 and response.json()['error']['code'] == 'INVALID_CURSOR'


def assert_roundtrip(client, headers, endpoints, versions):
    """Server-generated cursors retain their existing formats and visit each result once."""
    for path, _ in [*endpoints, versions]:
        first = client.get(path, headers=headers, params={'limit': 1}).json()['data']
        assert len(first['items']) == 1 and first['nextCursor']
        second = client.get(path, headers=headers, params={'limit': 1, 'cursor': first['nextCursor']})
        assert second.status_code == 200, second.text
        second = second.json()['data']
        assert len(second['items']) == 1 and second['nextCursor'] is None
        assert first['items'][0]['id'] != second['items'][0]['id']


def test_valid_cursor_formats_still_roundtrip(resources):
    """Protect clients from a cursor format change while strengthening input validation."""
    client, headers, endpoints, versions = resources
    assert_roundtrip(client, headers, endpoints, versions)
