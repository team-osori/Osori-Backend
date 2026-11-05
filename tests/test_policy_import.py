import io
import json
import zipfile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.main import app
from app.core.errors import ApiError
from app.services.policy_extraction import get_policy_extractor, OpenAIPolicyExtractor
from app.services.question_generation import OpenAIQuestionGenerator
from app.services.policy_documents import extract_document, MAX_FILE_BYTES
from test_projects import register


class Extractor:
    calls = 0

    def extract(self, content):
        self.calls += 1
        return {'rules': [{'key': 'refund', 'description': '환불 기한 안내', 'sourceQuote': content}]}


def document(kind):
    content = 'Refund within 24 hours.'
    if kind in ('txt', 'md'):
        return content.encode()
    if kind == 'docx':
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + content + '</w:t></w:r></w:p></w:body></w:document>')
        return stream.getvalue()
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 20 100 Td (Refund within 24 hours.) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def prepare(client):
    headers = register(client, 'import_owner')
    path = client.post('/api/projects', headers=headers, json={'name': 'Policy'}).headers['location'] + '/policies'
    extractor = Extractor()
    app.dependency_overrides[get_policy_extractor] = lambda: extractor
    return headers, path, extractor


@pytest.mark.parametrize('kind', ['pdf', 'docx', 'txt', 'md'])
def test_upload_draft_review_and_explicit_save(db_and_client, kind):
    _, client = db_and_client
    headers, path, extractor = prepare(client)
    result = client.post(path + '/import', headers=headers, data={'name': '환불 정책'}, files={'file': ('policy.' + kind, document(kind))})
    assert result.status_code == 200, result.text
    draft = result.json()['data']
    assert draft['status'] == 'draft' and draft['persisted'] is False
    assert 'Refund within 24 hours.' in draft['content']
    assert draft['rules'][0]['sourceQuote'] in draft['content']
    assert client.get(path, headers=headers).json()['data']['items'] == []
    saved = client.post(path, headers=headers, json={k: draft[k] for k in ('name', 'content', 'rules')})
    assert saved.status_code == 201, saved.text
    assert extractor.calls == 1
    assert saved.json()['data']['version']['content'] == draft['content']


def test_content_only_draft_and_invalid_evidence_do_not_persist(db_and_client):
    _, client = db_and_client
    headers, path, extractor = prepare(client)
    result = client.post(path, headers=headers, json={'name': '환불', 'content': '24시간 전 취소 가능'})
    assert result.status_code == 200 and result.json()['data']['status'] == 'draft'
    extractor.extract = lambda content: {'rules': [{'key': 'x', 'description': 'unsupported', 'sourceQuote': 'invented-private-source'}]}
    result = client.post(path, headers=headers, json={'name': '환불', 'content': '24시간 전 취소 가능'})
    assert result.status_code == 502 and result.json()['error']['code'] == 'POLICY_EXTRACTION_INVALID'
    assert 'invented-private-source' not in result.text
    assert client.get(path, headers=headers).json()['data']['items'] == []


def test_upload_auth_type_and_resource_limits(db_and_client):
    _, client = db_and_client
    headers, path, extractor = prepare(client)
    other = register(client, 'import_other')
    for credentials, status in [({}, 401), (other, 404)]:
        result = client.post(path + '/import', headers=credentials, data={'name': 'x'}, files={'file': ('p.txt', b'Policy')})
        assert result.status_code == status
    for filename, data, status in [('p.exe', b'x', 415), ('p.pdf', b'broken-private-pdf', 400), ('p.txt', b'x' * (MAX_FILE_BYTES + 1), 413), ('p.txt', b' ' * 10, 400)]:
        result = client.post(path + '/import', headers=headers, data={'name': 'x'}, files={'file': (filename, data)})
        assert result.status_code == status, result.text
        assert 'broken-private-pdf' not in result.text
    assert extractor.calls == 0


def test_empty_and_encrypted_pdf_are_explicit_errors(monkeypatch, tmp_path):
    from app.services import policy_documents, policy_ocr
    monkeypatch.setattr(policy_ocr, 'check_ocr_runtime', lambda: None)
    monkeypatch.setattr(policy_ocr, 'ocr_page', lambda *args: '')
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    out = io.BytesIO()
    writer.write(out)
    with pytest.raises(ValueError, match='DOCUMENT_TEXT_NOT_FOUND'):
        policy_documents.document_output(policy_documents.parse_document(out.getvalue(), '.pdf', tmp_path))
    writer.encrypt('not-a-real-secret')
    out = io.BytesIO()
    writer.write(out)
    with pytest.raises(ApiError) as caught:
        extract_document(out.getvalue(), 'locked.pdf')
    assert caught.value.code == 'DOCUMENT_ENCRYPTED'


def test_model_request_uses_existing_transport_without_paid_call(monkeypatch):
    transport = OpenAIQuestionGenerator('test-only-key')
    async def request(body):
        assert body['store'] is False and 'test-only-key' not in json.dumps(body)
        assert body['text']['format']['strict'] is True
        assert json.loads(body['input'][0]['content'])['content'] == 'Policy'
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'rules': [{'key': 'r', 'description': 'Rule', 'sourceQuote': 'Policy'}]})}]}]}
    monkeypatch.setattr(transport, '_request', request)
    assert OpenAIPolicyExtractor(transport).extract('Policy').rules[0].key == 'r'


def test_bedrock_extraction_reuses_current_provider_without_live_call(monkeypatch):
    from app.services import bedrock_runtime
    from app.services.policy_extraction import extract_rules, BedrockPolicyExtractor
    monkeypatch.setenv('QUESTION_GENERATION_PROVIDER', 'bedrock')
    monkeypatch.setenv('QUESTION_GENERATION_MODEL', 'test-model')
    def converse(**kwargs):
        assert kwargs['model'] == 'test-model'
        assert kwargs['name'] == 'submit_policy_rules'
        return {'rules': [{'key': 'r', 'description': 'Rule', 'sourceQuote': 'Policy'}]}
    monkeypatch.setattr(bedrock_runtime, 'converse_json', converse)
    extractor = get_policy_extractor()
    assert isinstance(extractor, BedrockPolicyExtractor)
    assert extract_rules(extractor, 'Policy')[0].sourceQuote == 'Policy'
