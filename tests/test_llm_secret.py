import asyncio
from copy import deepcopy

from sqlalchemy import select

from app.connection_worker import process_one
from app.core.secrets import encrypt_secret, pack_secrets, unpack_secrets
from app.models.connection import Connection
from test_connections_prompts import CONFIG, enqueue, setup  # noqa: F401  (setup은 fixture)

LLM_HEADER = 'X-OpenAI-Key'


def save(client, headers, service, revision, **change):
    body = {**deepcopy(CONFIG), 'expectedRevision': revision, **change}
    response = client.put(service + '/connection', headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()['data']


def put_llm(client, headers, service, revision, secret='sk-test-only-llm-key'):
    return client.put(service + '/connection/llm-secret', headers=headers,
                      json={'expectedRevision': revision, 'secret': secret})


def test_pack_keeps_legacy_single_secret_readable(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv('CONNECTION_ENCRYPTION_KEY', Fernet.generate_key().decode())
    assert unpack_secrets(None) == (None, None)
    assert unpack_secrets(encrypt_secret('legacy-token')) == ('legacy-token', None)
    assert pack_secrets(None, None) is None
    # 인증 키만 있으면 예전 형식 그대로 저장
    assert unpack_secrets(pack_secrets('auth', None)) == ('auth', None)
    assert unpack_secrets(pack_secrets(None, 'llm')) == (None, 'llm')
    assert unpack_secrets(pack_secrets('auth', 'llm')) == ('auth', 'llm')


def test_llm_secret_lifecycle_and_independence_from_auth_secret(setup):
    factory, client, headers, service, prompt = setup
    data = save(client, headers, service, 0, llmKeyHeaderName=LLM_HEADER)
    assert data['llmKeyHeaderName'] == LLM_HEADER and data['llmKeyConfigured'] is False

    assert put_llm(client, headers, service, 1).status_code == 200
    secret = client.put(service + '/connection/secret', headers=headers,
                        json={'expectedRevision': 2, 'secret': 'test-only-secret-token'})
    assert secret.status_code == 200, secret.text
    data = client.get(service + '/connection', headers=headers).json()['data']
    assert data['auth']['secretConfigured'] is True and data['llmKeyConfigured'] is True
    assert 'sk-test-only-llm-key' not in str(data)
    with factory() as db:
        stored = db.scalar(select(Connection))
        assert 'sk-test-only-llm-key' not in stored.secret_ciphertext
        assert unpack_secrets(stored.secret_ciphertext) == ('test-only-secret-token', 'sk-test-only-llm-key')

    # 인증 키를 지워도 AI 키는 남음
    assert client.delete(service + '/connection/secret', headers={**headers, 'If-Match': '"3"'}).status_code == 204
    data = client.get(service + '/connection', headers=headers).json()['data']
    assert data['auth']['secretConfigured'] is False and data['llmKeyConfigured'] is True

    assert client.delete(service + '/connection/llm-secret', headers={**headers, 'If-Match': '"4"'}).status_code == 204
    assert client.get(service + '/connection', headers=headers).json()['data']['llmKeyConfigured'] is False
    # 오래된 revision은 412
    assert client.delete(service + '/connection/llm-secret', headers={**headers, 'If-Match': '"4"'}).status_code == 412


def test_llm_secret_requires_header_and_is_dropped_when_target_changes(setup):
    factory, client, headers, service, prompt = setup
    save(client, headers, service, 0)
    response = put_llm(client, headers, service, 1)
    assert response.status_code == 409 and response.json()['error']['code'] == 'LLM_KEY_HEADER_REQUIRED'

    save(client, headers, service, 1, llmKeyHeaderName=LLM_HEADER)
    assert put_llm(client, headers, service, 2).status_code == 200
    # 같은 주소 · 헤더면 일반 설정을 바꿔도 키 유지
    data = save(client, headers, service, 3, llmKeyHeaderName=LLM_HEADER, timeoutMs=2000)
    assert data['llmKeyConfigured'] is True
    # 헤더 이름을 바꾸면 이전 키를 다른 헤더로 보내지 않도록 지움
    data = save(client, headers, service, 4, llmKeyHeaderName='X-Other-Key')
    assert data['llmKeyConfigured'] is False
    assert put_llm(client, headers, service, 5).status_code == 200
    # 주소를 바꿔도 지움
    data = save(client, headers, service, 6, llmKeyHeaderName='X-Other-Key', url='https://other.example.com/chat')
    assert data['llmKeyConfigured'] is False


def test_llm_key_header_validation(setup):
    factory, client, headers, service, prompt = setup
    for name, extra in [('Authorization', {}), ('X-Api-Key', {'auth': {'type': 'HEADER', 'headerName': 'X-Api-Key'}}),
                        ('bad header', {})]:
        body = {**deepcopy(CONFIG), **extra, 'llmKeyHeaderName': name}
        assert client.put(service + '/connection', headers=headers, json=body).status_code == 400, name
    # 같은 이름의 일반 헤더로 키를 우회 저장할 수 없음
    body = {**deepcopy(CONFIG), 'llmKeyHeaderName': LLM_HEADER, 'headers': {LLM_HEADER: 'sk-plain'}}
    assert client.put(service + '/connection', headers=headers, json=body).status_code == 400


def test_worker_sends_llm_key_header_and_redacts_it(setup):
    factory, client, headers, service, prompt = setup
    save(client, headers, service, 0, llmKeyHeaderName=LLM_HEADER)
    assert client.put(service + '/connection/secret', headers=headers,
                      json={'expectedRevision': 1, 'secret': 'test-only-secret-token'}).status_code == 200
    assert put_llm(client, headers, service, 2).status_code == 200
    response = enqueue(client, headers, service, prompt, revision=3)
    assert response.status_code == 202, response.text

    async def sender(config, payload, outgoing_headers):
        assert outgoing_headers['Authorization'] == 'Bearer test-only-secret-token'
        assert outgoing_headers[LLM_HEADER] == 'sk-test-only-llm-key'
        return '답변 sk-test-only-llm-key'

    assert asyncio.run(process_one(factory, sender=sender))
    result = client.get(response.headers['location'], headers=headers).json()['data']
    assert result['status'] == 'SUCCEEDED' and result['answer'] == '답변 [REDACTED]'


def test_auth_check_ignores_llm_only_secret(setup):
    factory, client, headers, service, prompt = setup
    save(client, headers, service, 0, llmKeyHeaderName=LLM_HEADER)
    assert put_llm(client, headers, service, 1).status_code == 200
    # BEARER 인증인데 인증 키 없이 AI 키만 있으면 테스트를 접수하지 않음
    response = enqueue(client, headers, service, prompt, revision=2)
    assert response.status_code == 409 and response.json()['error']['code'] == 'CONNECTION_SECRET_REQUIRED'
