"""Bounded document text extraction in a resource-limited subprocess, with no file persistence."""
import hashlib
import io
import json
import logging
import os
import signal
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
import zipfile
import xml.etree.ElementTree as ET

from app.core.errors import ApiError

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 100000
DOCUMENT_SECONDS = 60
EXTRACTION_SLOTS = BoundedSemaphore(2)
ERRORS = {
    'DOCUMENT_OCR_UNAVAILABLE': '한국어·영어 OCR 실행 환경이 준비되지 않았습니다.',
    'DOCUMENT_OCR_TIMEOUT': 'OCR 제한 시간을 초과했습니다. 문서를 나누어 업로드하세요.',
    'DOCUMENT_OCR_FAILED': '페이지 OCR에 실패했습니다. 문서를 확인해 주세요.',
    'DOCUMENT_TIMEOUT': '문서 추출 제한 시간을 초과했습니다. 문서를 나누어 업로드하세요.',
    'DOCUMENT_INVALID': '문서 형식을 확인할 수 없습니다.',
    'DOCUMENT_ENCRYPTED': '암호화된 PDF는 지원하지 않습니다.',
    'DOCUMENT_PAGE_LIMIT': 'PDF는 최대 50페이지입니다.',
    'DOCUMENT_TEXT_LIMIT': '추출한 원문은 최대 100,000자입니다.',
    'DOCUMENT_TEXT_NOT_FOUND': 'OCR을 포함해 읽을 수 있는 텍스트가 없습니다.',
}


def checked_text(text):
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError('DOCUMENT_TEXT_LIMIT')
    if not text.strip():
        raise ValueError('DOCUMENT_TEXT_NOT_FOUND')
    if '\x00' in text:
        raise ValueError('DOCUMENT_INVALID')
    return text


def parse_text_document(data, suffix):
    if suffix in ('.txt', '.md'):
        return checked_text(data.decode('utf-8-sig'))
    if suffix == '.docx':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 1000:
                raise ValueError('DOCUMENT_INVALID')
            info = archive.getinfo('word/document.xml')
            if info.file_size > 5 * 1024 * 1024:
                raise ValueError('DOCUMENT_TEXT_LIMIT')
            xml = archive.read(info)
        if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
            raise ValueError('DOCUMENT_INVALID')
        root = ET.fromstring(xml)
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        lines = []
        for paragraph in root.iter(ns + 'p'):
            lines.append(''.join(node.text or '' if node.tag == ns + 't' else '\t' if node.tag == ns + 'tab' else '\n'
                for node in paragraph.iter() if node.tag in (ns + 't', ns + 'tab', ns + 'br', ns + 'cr')))
        return checked_text('\n'.join(lines))
    raise ValueError('DOCUMENT_INVALID')


def parse_document(data, suffix, directory, ocr_mode='auto'):
    """Preserve page boundaries, including blank pages, in source offsets."""
    if suffix != '.pdf':
        return [(None, parse_text_document(data, suffix), 'text', False)]
    from pypdf import PdfReader
    from app.services.policy_ocr import check_ocr_runtime, ocr_page
    if not data.startswith(b'%PDF-'):
        raise ValueError('DOCUMENT_INVALID')
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError('DOCUMENT_ENCRYPTED')
    if len(reader.pages) > 50:
        raise ValueError('DOCUMENT_PAGE_LIMIT')
    pdf_path = directory / 'input.pdf'
    parts, size, ready = [], 0, False
    for number, page in enumerate(reader.pages, 1):
        try:
            text = '' if ocr_mode == 'force' else (page.extract_text() or '')
        except Exception:
            # A broken text map can still be readable when the page is rendered.
            text = ''
        method = 'text'
        # Prefer existing text. Flag image+text pages for review instead of silently
        # replacing reliable digital text; clients can request force OCR explicitly.
        has_unread_images = bool(text.strip() and page.images.keys())
        if ocr_mode == 'force' or not text.strip():
            if not ready:
                check_ocr_runtime()
                pdf_path.write_bytes(data)
                ready = True
            text = ocr_page(pdf_path, number, directory)
            method = 'ocr'
            has_unread_images = False
        size += len(text) + (1 if parts else 0)
        if size > MAX_TEXT_CHARS:
            raise ValueError('DOCUMENT_TEXT_LIMIT')
        parts.append((number, text, method, has_unread_images))
    return parts


def document_output(parts):
    content, chunks, ocr_pages, empty_pages, image_text_pages = '', [], [], [], []
    for index, (page, text, method, has_unread_images) in enumerate(parts):
        if index:
            content += '\n'
        if has_unread_images:
            image_text_pages.append(page)
        start = len(content)
        content += text
        if page is not None and method == 'ocr':
            ocr_pages.append(page)
        if not text.strip():
            if page is not None:
                empty_pages.append(page)
            continue
        for offset in range(start, len(content), 2000):
            chunks.append({'index': len(chunks) + 1, 'pageNumber': page,
                'startOffset': offset, 'endOffset': min(offset + 2000, len(content)), 'method': method})
    return {'content': checked_text(content), 'pageCount': len(parts) if parts and parts[0][0] is not None else None,
            'chunks': chunks, 'ocrPages': ocr_pages, 'emptyPages': empty_pages, 'imageTextPages': image_text_pages}


def safe_filename(filename):
    return Path((filename or 'document').replace('\\', '/')).name[:255] or 'document'


def extract_document_details(data, filename, ocr_mode='auto'):
    from app.schemas.policy_document import DocumentSource
    filename = safe_filename(filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in ('.pdf', '.docx', '.txt', '.md'):
        raise ApiError(415, 'DOCUMENT_TYPE_UNSUPPORTED', 'PDF, DOCX, UTF-8 TXT/MD 파일을 사용하세요.')
    if not data or len(data) > MAX_FILE_BYTES:
        raise ApiError(413, 'DOCUMENT_SIZE_INVALID', '파일은 1바이트 이상 10MiB 이하여야 합니다.')
    if ocr_mode not in ('auto', 'force'):
        raise ApiError(400, 'DOCUMENT_OCR_MODE_INVALID', 'OCR 모드는 auto 또는 force입니다.')
    if not EXTRACTION_SLOTS.acquire(blocking=False):
        raise ApiError(503, 'DOCUMENT_EXTRACTION_BUSY', '문서 추출이 진행 중입니다. 잠시 후 다시 시도하세요.',
                       headers={'Retry-After': '5'})
    try:
        # Parent owns temporary files so even a timed-out worker leaves no document behind.
        with TemporaryDirectory(prefix='osori-document-') as temporary:
            with subprocess.Popen([sys.executable, '-m', __name__, suffix, temporary, ocr_mode],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True) as process:
                try:
                    stdout, _ = process.communicate(data, timeout=DOCUMENT_SECONDS)
                except subprocess.TimeoutExpired:
                    raise ApiError(504, 'DOCUMENT_TIMEOUT', ERRORS['DOCUMENT_TIMEOUT']) from None
                finally:
                    # Includes the renderer/OCR grandchildren, even if the worker was killed.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            if process.returncode != 0:
                raise ValueError('DOCUMENT_INVALID')
            output = json.loads(stdout)
        if 'error' in output:
            code = output['error'] if output['error'] in ERRORS else 'DOCUMENT_INVALID'
            status = 503 if code == 'DOCUMENT_OCR_UNAVAILABLE' else 504 if code.endswith('_TIMEOUT') else 400
            raise ApiError(status, code, ERRORS[code])
        content = checked_text(output.pop('content'))
        source = DocumentSource(documentId='doc_' + hashlib.sha256(data).hexdigest(),
            filename=filename, contentHash=hashlib.sha256(content.encode()).hexdigest(),
            chunkCount=len(output['chunks']), **output)
        return content, source
    except ApiError:
        raise
    except (subprocess.SubprocessError, ValueError, KeyError, OSError):
        raise ApiError(400, 'DOCUMENT_INVALID', '문서를 제한 시간과 메모리 안에서 읽을 수 없습니다.') from None
    finally:
        EXTRACTION_SLOTS.release()


def extract_document(data, filename):
    """Compatibility helper for callers that only need text."""
    return extract_document_details(data, filename)[0]


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
        if sys.platform.startswith('linux'):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('DOCUMENT_INVALID')
        print(json.dumps(document_output(parse_document(data, sys.argv[1], Path(sys.argv[2]), sys.argv[3]))))
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc) in ERRORS else 'DOCUMENT_INVALID'
        print(json.dumps({'error': code}))
