from copy import deepcopy

import pytest

from app.main import app
from test_policy_testsets import setup


@pytest.mark.parametrize('kind,code,field', [
    ('duplicate', 'POLICY_RULE_KEY_DUPLICATE', 'rules.1.key'),
    ('source', 'POLICY_RULE_SOURCE_MISMATCH', 'rules.0.sourceQuote'),
    ('whitespace', 'POLICY_RULE_SOURCE_MISMATCH', 'rules.0.sourceQuote'),
])
def test_rule_errors_identify_safe_field_and_preserve_versions(db_and_client, kind, code, field):
    _, client = db_and_client
    headers, project, policy, source, *_ = setup(client)
    invalid = deepcopy(policy)
    if kind == 'duplicate':
        invalid['rules'].append(deepcopy(invalid['rules'][0]))
    elif kind == 'whitespace':
        invalid['rules'][0]['sourceQuote'] = invalid['rules'][0]['sourceQuote'].replace(' ', '\n')
    else:
        invalid['rules'][0]['sourceQuote'] = 'private-policy-text-do-not-echo'
    response = client.post(project + '/policies', json=invalid, headers=headers)
    assert response.status_code == 400
    error = response.json()['error']
    assert error['code'] == 'VALIDATION_ERROR'
    assert error['fieldErrors'][0]['field'] == field
    assert error['fieldErrors'][0]['code'] == code
    assert 'private-policy-text-do-not-echo' not in response.text
    path = project + '/policies/' + source['policy']['id']
    response = client.post(path + '/versions', headers=headers,
        json={k: v for k, v in {**invalid, 'expectedRevision': 1}.items() if k != 'name'})
    assert response.status_code == 400 and response.json()['error']['fieldErrors'][0]['code'] == code
    assert client.get(path, headers=headers).json()['data']['policy']['revision'] == 1
    assert len(client.get(project + '/policies', headers=headers).json()['data']['items']) == 1


def test_policy_schema_documents_client_rules_and_verbatim_quotes():
    schemas = app.openapi()['components']['schemas']
    assert 'rules' not in schemas['PolicyCreate']['required']
    assert '미저장 draft' in schemas['PolicyCreate']['properties']['rules']['description']
    assert '공백·줄바꿈' in schemas['Rule']['properties']['sourceQuote']['description']
