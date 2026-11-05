"""Bounded document text extraction in a resource-limited subprocess, with no file persistence."""
import io
import json
import logging
from pathlib import Path
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

from app.core.errors import ApiError

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 100000
ERRORS = {
    'DOCUMENT_INVALID': '문서 형식을 확인할 수 없습니다.',
    'DOCUMENT_ENCRYPTED': '암호화된 PDF는 지원하지 않습니다.',
    'DOCUMENT_PAGE_LIMIT': 'PDF는 최대 50페이지입니다.',
    'DOCUMENT_TEXT_LIMIT': '추출한 원문은 최대 100,000자입니다.',
    'DOCUMENT_TEXT_NOT_FOUND': '읽을 수 있는 텍스트가 없습니다. 스캔 PDF는 OCR 후 업로드하세요.',
}


def checked_text(text):
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError('DOCUMENT_TEXT_LIMIT')
    if not text.strip():
        raise ValueError('DOCUMENT_TEXT_NOT_FOUND')
    if '\x00' in text:
        raise ValueError('DOCUMENT_INVALID')
    return text


def parse_document(data, suffix):
    if suffix in ('.txt', '.md'):
        return checked_text(data.decode('utf-8-sig'))
    if suffix == '.pdf':
        from pypdf import PdfReader
        if not data.startswith(b'%PDF-'):
            raise ValueError('DOCUMENT_INVALID')
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError('DOCUMENT_ENCRYPTED')
        if len(reader.pages) > 50:
            raise ValueError('DOCUMENT_PAGE_LIMIT')
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or '')
            if sum(map(len, parts)) > MAX_TEXT_CHARS:
                raise ValueError('DOCUMENT_TEXT_LIMIT')
        return checked_text('\n'.join(parts))
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


def extract_document(data, filename):
    suffix = Path(filename or '').suffix.lower()
    if suffix not in ('.pdf', '.docx', '.txt', '.md'):
        raise ApiError(415, 'DOCUMENT_TYPE_UNSUPPORTED', 'PDF, DOCX, UTF-8 TXT/MD 파일을 사용하세요.')
    if not data or len(data) > MAX_FILE_BYTES:
        raise ApiError(413, 'DOCUMENT_SIZE_INVALID', '파일은 1바이트 이상 10MiB 이하여야 합니다.')
    try:
        result = subprocess.run([sys.executable, '-m', __name__, suffix], input=data,
            capture_output=True, timeout=20, check=True)
        output = json.loads(result.stdout)
        if 'error' in output:
            code = output['error'] if output['error'] in ERRORS else 'DOCUMENT_INVALID'
            raise ApiError(400, code, ERRORS[code])
        return checked_text(output['content'])
    except ApiError:
        raise
    except (subprocess.SubprocessError, ValueError, KeyError, OSError):
        raise ApiError(400, 'DOCUMENT_INVALID', '문서를 제한 시간과 메모리 안에서 읽을 수 없습니다.') from None


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        if sys.platform.startswith('linux'):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('DOCUMENT_INVALID')
        print(json.dumps({'content': parse_document(data, sys.argv[1])}))
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc) in ERRORS else 'DOCUMENT_INVALID'
        print(json.dumps({'error': code}))
