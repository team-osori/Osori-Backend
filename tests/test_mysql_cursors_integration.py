from test_mysql_auth_integration import mysql_client  # noqa: F401 - shared fixture
from test_shared_cursors import assert_invalid, assert_roundtrip, prepare_cursor_resources


def test_mysql_rejects_hostile_cursor_values_before_binding(mysql_client):
    """Reproduce both reported 500 errors against the actual production database dialect."""
    client, _, _, _ = mysql_client
    headers, endpoints, (path, data) = prepare_cursor_resources(client)
    for list_path, value in endpoints:
        for id_ in ('\ud800', '\udfff', 'bad\x00id'):
            assert_invalid(client, headers, list_path, {**value, 'id': id_})
    for number in (2**31, 10**1000):
        assert_invalid(client, headers, path, {**data, 'before': number})


def test_mysql_preserves_original_cursor_roundtrips(mysql_client):
    """Keep second-precision MySQL ordering and original wire formats working after validation."""
    client, _, _, _ = mysql_client
    headers, endpoints, versions = prepare_cursor_resources(client)
    assert_roundtrip(client, headers, endpoints, versions)


def test_mysql_metadata_revision_allows_only_one_concurrent_writer(mysql_client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from test_projects import register
    client, _, _, _ = mysql_client
    headers = register(client, 'metadata_race')
    project = client.post('/api/projects', headers=headers, json={'name': 'original'}).headers['location']
    service = client.post(project + '/services', headers=headers, json={'name': 'original'}).headers['location']
    for path in (project, service):
        barrier = Barrier(2)

        def change(name):
            barrier.wait(timeout=10)
            return client.patch(path, headers=headers, json={'expectedRevision': 1, 'name': name})

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(change, ('first', 'second')))
        assert sorted(response.status_code for response in responses) == [200, 409]
        winner = next(response.json()['data'] for response in responses if response.status_code == 200)
        assert client.get(path, headers=headers).json()['data'] == winner
        assert winner['revision'] == 2


def test_mysql_project_trash_cleanup_removes_full_graph(mysql_client, monkeypatch):
    from test_resource_lifecycle import exercise_project_trash
    client, factory, _, _ = mysql_client
    exercise_project_trash(factory, client, monkeypatch)


def test_mysql_duplicate_invitations_and_acceptance(mysql_client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from test_projects import register
    client, _, _, _ = mysql_client
    headers = register(client, 'invite_owner')
    member = register(client, 'invite_member')
    project = client.post('/api/projects', headers=headers, json={'name': 'Shared'}).headers['location']
    barrier = Barrier(2)
    def invite(_):
        barrier.wait(timeout=10)
        return client.post(project + '/invitations', headers=headers, json={'email': 'invite_member@example.com'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invite, (1, 2)))
    assert sorted(result.status_code for result in results) == [201, 409]
    invitation = next(result.json()['data'] for result in results if result.status_code == 201)
    barrier = Barrier(2)
    def accept(_):
        barrier.wait(timeout=10)
        return client.post('/api/invitations/' + invitation['id'] + '/accept', headers=member, json={'expectedRevision': 1})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, (1, 2)))
    assert sorted(result.status_code for result in results) == [200, 409]
