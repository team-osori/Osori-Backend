"""OCR integration uses a synthetic policy fixture, never a customer document/model API."""
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
from time import monotonic, sleep

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from app.core.errors import ApiError
from app.services import policy_documents as documents, policy_ocr as ocr
from app.schemas.policy import PolicyCreate
from test_policy_import import document, prepare

SCAN = Path(__file__).parent / 'fixtures' / 'policy-scan-ko-en.pdf'


@pytest.fixture
def ocr_runtime():
    try:
        ocr.check_ocr_runtime()
    except ValueError:
        if os.environ.get('CI'):
            pytest.fail('CI must install Poppler and Tesseract kor+eng')
        pytest.skip('Poppler and Tesseract kor+eng are required for real OCR integration')


def test_real_korean_english_scan_and_mixed_page_locations(ocr_runtime):
    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(document('pdf'))).pages[0])
    writer.add_blank_page(300, 200)
    writer.add_page(PdfReader(SCAN).pages[0])
    output = io.BytesIO()
    writer.write(output)
    content, source = documents.extract_document_details(output.getvalue(), 'mixed.pdf')
    assert 'Refund within 24 hours.' in content
    assert 'Refund within 7 days' in content
    assert '환불' in content and '구매' in content
    assert source.pageCount == 3 and source.ocrPages == [2, 3] and source.emptyPages == [2]
    assert [c.pageNumber for c in source.chunks] == [1, 3]
    assert [c.method for c in source.chunks] == ['text', 'ocr']
    assert '환불' in content[source.chunks[-1].startOffset:source.chunks[-1].endOffset]
    # Explicit full OCR works even on a digital page.
    forced, location = documents.extract_document_details(document('pdf'), 'digital.pdf', 'force')
    assert 'Refund' in forced and location.ocrPages == [1]


def test_image_with_digital_header_prefers_text_and_flags_review(monkeypatch, tmp_path):
    writer = PdfWriter()
    page = writer.add_page(PdfReader(SCAN).pages[0])
    digital = PdfReader(io.BytesIO(document('pdf'))).pages[0]
    page['/Resources'][NameObject('/Font')] = digital['/Resources']['/Font']
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data() + b'\n' + digital.get_contents().get_data())
    page[NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    calls = []
    monkeypatch.setattr(ocr, 'check_ocr_runtime', lambda: None)
    monkeypatch.setattr(ocr, 'ocr_page', lambda pdf, page, directory: calls.append(page) or 'scanned body')
    parts = documents.parse_document(out.getvalue(), '.pdf', tmp_path)
    result = documents.document_output(parts)
    assert 'Refund within 24 hours.' in result['content'] and calls == []
    assert result['imageTextPages'] == [1] and result['ocrPages'] == []
    forced = documents.document_output(documents.parse_document(out.getvalue(), '.pdf', tmp_path, 'force'))
    assert forced['content'] == 'scanned body' and calls == [1]
    assert forced['imageTextPages'] == [] and forced['ocrPages'] == [1]


def test_ocr_dependency_failure_is_explicit_and_text_pdf_still_works(monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(ApiError) as caught:
        documents.extract_document(SCAN.read_bytes(), 'scan.pdf')
    assert (caught.value.status, caught.value.code) == (503, 'DOCUMENT_OCR_UNAVAILABLE')
    assert 'Refund within 24 hours.' in documents.extract_document(document('pdf'), 'digital.pdf')


def test_ocr_page_timeout_and_error_cleanup(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        (tmp_path / 'page.png').write_bytes(b'partial')
        raise subprocess.TimeoutExpired('renderer', 15)
    monkeypatch.setattr(ocr.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='DOCUMENT_OCR_TIMEOUT'):
        ocr.ocr_page(tmp_path / 'input.pdf', 1, tmp_path)
    assert not (tmp_path / 'page.png').exists()


def test_document_timeout_kills_descendants_and_removes_files(monkeypatch, tmp_path):
    """A hung OCR grandchild must not survive the request or keep temporary source files."""
    actual_popen = subprocess.Popen
    paths = []
    child_pid = tmp_path / 'pid'
    def hung_worker(command, **kwargs):
        paths.append(Path(command[4]))
        script = ('import pathlib,subprocess,sys,time; '
                  'pathlib.Path(sys.argv[1],"input.pdf").write_bytes(b"private"); '
                  'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
                  'pathlib.Path(sys.argv[2]).write_text(str(child.pid)); time.sleep(30)')
        return actual_popen([sys.executable, '-c', script, command[4], str(child_pid)], **kwargs)
    monkeypatch.setattr(documents.subprocess, 'Popen', hung_worker)
    monkeypatch.setattr(documents, 'DOCUMENT_SECONDS', 1)
    with pytest.raises(ApiError) as caught:
        documents.extract_document(b'policy', 'policy.txt')
    assert caught.value.code == 'DOCUMENT_TIMEOUT' and caught.value.status == 504
    assert paths and not paths[0].exists()
    monkeypatch.setattr(documents.subprocess, 'Popen', actual_popen)
    pid = int(child_pid.read_text())
    # Reaped on macOS; Linux may briefly retain a zombie until PID 1 reaps it.
    deadline = monotonic() + 2
    while monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        state = Path(f'/proc/{pid}/stat')
        try:
            if state.exists() and state.read_text().split(') ', 1)[1].startswith('Z'):
                break
        except FileNotFoundError:
            break
        sleep(0.02)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail('OCR descendant survived timeout')


def test_pdf_page_limit_rejected_before_ocr(monkeypatch, tmp_path):
    writer = PdfWriter()
    for _ in range(51):
        writer.add_blank_page(100, 100)
    out = io.BytesIO()
    writer.write(out)
    monkeypatch.setattr(ocr, 'check_ocr_runtime', lambda: pytest.fail('must reject before OCR'))
    with pytest.raises(ValueError, match='DOCUMENT_PAGE_LIMIT'):
        documents.parse_document(out.getvalue(), '.pdf', tmp_path)


def test_chunk_limits_unicode_offsets_and_stale_source_rejection():
    text = '정책😀' * 900
    content, source = documents.extract_document_details(text.encode(), '../policy.txt')
    assert source.filename == 'policy.txt' and source.chunkCount == 2
    assert ''.join(content[c.startOffset:c.endOffset] for c in source.chunks) == text
    payload = {'name': 'policy', 'content': content, 'rules': [
        {'key': 'r', 'description': 'rule', 'sourceQuote': '정책😀'}], 'sourceDocument': source.model_dump()}
    assert PolicyCreate.model_validate(payload).sourceDocument == source
    with pytest.raises(ValueError):
        PolicyCreate.model_validate({**payload, 'content': content + 'x'})
    assert PolicyCreate.model_validate({**payload, 'content': content + 'x', 'sourceDocument': None})
    with pytest.raises(ApiError) as caught:
        documents.extract_document(b'a' * 100001, 'large.txt')
    assert caught.value.code == 'DOCUMENT_TEXT_LIMIT'


def test_batch_partial_failure_and_reviewed_source_persistence(db_and_client):
    _, client = db_and_client
    headers, path, extractor = prepare(client)
    result = client.post(path + '/imports', headers=headers, files=[
        ('files', ('one.txt', b'Refund within 24 hours.')),
        ('files', ('broken.pdf', b'private-invalid-data')),
        ('files', ('three.md', b'Return the item within 7 days.'))])
    assert result.status_code == 200, result.text
    items = result.json()['data']['items']
    assert [item['status'] for item in items] == ['draft', 'failed', 'draft']
    assert items[1]['error']['code'] == 'DOCUMENT_INVALID'
    assert 'private-invalid-data' not in result.text and extractor.calls == 2
    assert client.get(path, headers=headers).json()['data']['items'] == []
    draft = items[0]['draft']
    payload = {k: draft[k] for k in ('name', 'content', 'rules', 'sourceDocument')}
    saved = client.post(path, headers=headers, json=payload)
    assert saved.status_code == 201, saved.text
    detail = saved.json()['data']
    assert detail['version']['sourceDocument'] == draft['sourceDocument']
    location = saved.headers['location']
    assert client.get(location, headers=headers).json()['data']['version']['sourceDocument'] == draft['sourceDocument']
    new_version = client.post(location + '/versions', headers=headers,
        json={**{k: v for k, v in payload.items() if k != 'name'}, 'expectedRevision': 1})
    assert new_version.status_code == 201 and new_version.json()['data']['sourceDocument'] == draft['sourceDocument']
    stale = client.post(path, headers=headers, json={**payload, 'content': payload['content'] + ' edited'})
    assert stale.status_code == 400


def test_batch_authorization_file_count_and_rate_limit_precede_extraction(db_and_client, monkeypatch):
    from app.api import policies
    _, client = db_and_client
    headers, path, extractor = prepare(client)
    files = [('files', ('x.txt', b'policy'))]
    assert client.post(path + '/imports', files=files).status_code == 401
    assert client.post(path + '/imports', headers=headers, files=files * 6).status_code == 400
    monkeypatch.setattr(policies, 'check_rate_limit', lambda *args: 42)
    monkeypatch.setattr(policies, 'extract_document_details', lambda *args: pytest.fail('rate limited before OCR'))
    result = client.post(path + '/imports', headers=headers, files=files)
    assert result.json()['data']['items'][0]['error'] == {
        'code': 'RATE_LIMITED', 'message': '규칙 추출 요청이 너무 많습니다.', 'httpStatus': 429, 'retryAfter': '42'}


def test_busy_extraction_is_retryable():
    documents.EXTRACTION_SLOTS.acquire()
    documents.EXTRACTION_SLOTS.acquire()
    try:
        with pytest.raises(ApiError) as caught:
            documents.extract_document(b'policy', 'policy.txt')
        assert caught.value.status == 503 and caught.value.headers == {'Retry-After': '5'}
    finally:
        documents.EXTRACTION_SLOTS.release()
        documents.EXTRACTION_SLOTS.release()
