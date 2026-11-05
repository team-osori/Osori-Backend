from fastapi.testclient import TestClient


def register(client, login):
    """Create a distinct test user and obtain credentials for protected API checks."""
    credentials = {'email': f'{login}@example.com', 'password': 'secure-password-123'}
    assert client.post('/api/auth/signup', json={**credentials, 'name': 'Test'}).status_code == 201
    data = client.post('/api/auth/login', json=credentials).json()['data']
    return {'Authorization': 'Bearer ' + data['accessToken']}


def test_projects_and_ownership(db_and_client):
    """Verify projects and ownership against the observable API or database contract."""
    _, client = db_and_client
    owner = register(client, 'owner')
    other = register(client, 'other')
    assert client.post('/api/projects', json={'name': 'test'}).status_code == 401
    empty = client.get('/api/projects', headers=owner).json()['data']
    assert empty == {'items': [], 'nextCursor': None, 'total': 0,
        'statusCounts': {'SETUP': 0, 'RUNNING': 0, 'DONE': 0}}
    created = client.post('/api/projects', json={'name': ' 한빛항공 ', 'description': ' 검증 '}, headers=owner)
    assert created.status_code == 201
    project = created.json()['data']
    assert project['name'] == '한빛항공' and project['description'] == '검증'
    assert project['revision'] == 1 and project['serviceCount'] == 0
    path = created.headers['location']
    assert client.get(path, headers=owner).json()['data'] == project
    assert client.get(path, headers=other).status_code == 404
    assert client.get('/api/projects', headers=other).json()['data']['items'] == []
    assert client.post(path + '/services', json={'name': 'chatbot'}, headers=other).status_code == 404
    assert client.get(path + '/services', headers=other).status_code == 404
    assert client.get(path + '/services', headers=owner).json()['data']['items'] == []
    created_bot = client.post(path + '/services', json={'name': ' 상담 챗봇 '}, headers=owner)
    assert created_bot.status_code == 201
    chatbot = created_bot.json()['data']
    assert chatbot['projectId'] == project['id'] and chatbot['hasConnection'] is False
    assert client.get(created_bot.headers['location'], headers=owner).json()['data'] == chatbot
    assert client.get(created_bot.headers['location'], headers=other).status_code == 404
    assert client.post(path + '/services', json={'name': 'second'}, headers=owner).status_code == 409
    assert client.get(path, headers=owner).json()['data']['serviceCount'] == 1
    another = client.post('/api/projects', json={'name': 'Other'}, headers=owner).headers['location']
    assert client.get(another + '/services/' + chatbot['id'], headers=owner).status_code == 404


def test_pagination_and_invalid_inputs(db_and_client):
    """Verify pagination and invalid inputs against the observable API or database contract."""
    _, client = db_and_client
    headers = register(client, 'owner')
    other = register(client, 'other')
    ids = set()
    for i in range(5):
        ids.add(client.post('/api/projects', json={'name': f'project {i}'}, headers=headers).json()['data']['id'])
    found = []
    cursor = None
    while True:
        params = {'limit': 2}
        if cursor:
            params['cursor'] = cursor
        page = client.get('/api/projects', params=params, headers=headers).json()['data']
        found.extend(p['id'] for p in page['items'])
        cursor = page['nextCursor']
        if cursor is None:
            break
        assert client.get('/api/projects', params={'cursor': cursor}, headers=other).status_code == 400
    assert len(found) == len(set(found)) == 5 and set(found) == ids
    for cursor in ('broken', 'W10=', 'bnVsbA=='):
        assert client.get('/api/projects', params={'cursor': cursor}, headers=headers).status_code == 400
    assert client.get('/api/projects', params={'limit': 101}, headers=headers).status_code == 400
    for body in ({'name': ' '}, {'name': 'x', 'description': ' '}, {'name': 'x', 'extra': 1}):
        assert client.post('/api/projects', json=body, headers=headers).status_code == 400
    assert client.post('/api/projects', content='{}', headers={**headers, 'Content-Type': 'text/plain'}).status_code == 415


def test_common_errors(db_and_client):
    """Verify common errors against the observable API or database contract."""
    _, client = db_and_client
    missing = client.get('/api/does-not-exist')
    assert missing.status_code == 404
    assert missing.json()['error']['requestId'] == missing.headers['x-request-id']
    from app.api.dependencies import get_current_user

    def broken():
        """Simulate an internal failure to verify sensitive details stay out of responses."""
        raise RuntimeError('secret-content-must-not-leak')

    from app.main import app
    app.dependency_overrides[get_current_user] = broken
    response = client.get('/api/projects')
    assert response.status_code == 500
    assert 'secret-content' not in response.text
    assert response.json()['error']['code'] == 'INTERNAL_ERROR'
    app.dependency_overrides.pop(get_current_user)


def test_openapi_and_cors(db_and_client):
    """Verify openapi and cors against the observable API or database contract."""
    _, client = db_and_client
    schema = client.get('/openapi.json').json()
    assert 'HTTPBearer' in schema['components']['securitySchemes']
    assert 'Data_ProjectResponse_' in schema['components']['schemas']
    assert '422' not in schema['paths']['/api/projects']['post']['responses']
    response = client.options('/api/projects', headers={'Origin': 'http://localhost:5173',
        'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'})
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'http://localhost:5173'
