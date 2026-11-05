import pytest

from test_projects import register


@pytest.mark.parametrize('kind', ['project', 'service'])
def test_metadata_update_ownership_partial_fields_and_conflicts(db_and_client, kind):
    _, client = db_and_client
    headers = register(client, 'metadata_owner')
    other = register(client, 'metadata_other')
    project = client.post('/api/projects', headers=headers, json={'name': '원래 이름', 'description': '원래 설명'}).headers['location']
    path = project
    if kind == 'service':
        path = client.post(project + '/services', headers=headers,
            json={'name': '원래 이름', 'description': '원래 설명'}).headers['location']
    request = {'expectedRevision': 1, 'name': ' 새 이름 '}
    assert client.patch(path, headers=other, json=request).status_code == 404
    assert client.patch(path, json=request).status_code == 401
    response = client.patch(path, headers=headers, json=request)
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['name'] == '새 이름' and data['description'] == '원래 설명' and data['revision'] == 2
    assert client.patch(path, headers=headers, json=request).status_code == 409
    response = client.patch(path, headers=headers, json={'expectedRevision': 2, 'description': None})
    assert response.status_code == 200
    data = response.json()['data']
    assert data['name'] == '새 이름' and data['description'] is None and data['revision'] == 3
    for body in ({'expectedRevision': 3}, {'expectedRevision': 3, 'name': None},
                 {'expectedRevision': 3, 'name': ' '}, {'expectedRevision': 3, 'description': ' '},
                 {'expectedRevision': True, 'name': 'new'}, {'expectedRevision': 3, 'ownerId': 'other'}):
        assert client.patch(path, headers=headers, json=body).status_code == 400
    assert client.get(path, headers=headers).json()['data'] == data
    if kind == 'service':
        other_project = client.post('/api/projects', headers=headers, json={'name': 'Other'}).headers['location']
        assert client.patch(other_project + '/services/' + data['id'], headers=headers,
            json={'expectedRevision': 3, 'name': 'x'}).status_code == 404
        assert client.get(project, headers=headers).json()['data']['revision'] == 1
    else:
        assert data['progress']['stage'] == 1
        assert client.get('/api/projects', headers=headers).json()['data']['items'][0] == data
