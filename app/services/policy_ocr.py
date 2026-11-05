"""Local Korean/English OCR. Called only inside the bounded document process group."""
import os
from pathlib import Path
import shutil
import subprocess
from time import monotonic

OCR_PAGE_SECONDS = 15
OCR_MAX_PIXELS = 2400


def check_ocr_runtime():
    if not shutil.which('pdftoppm') or not shutil.which('tesseract'):
        raise ValueError('DOCUMENT_OCR_UNAVAILABLE')
    try:
        result = subprocess.run(['tesseract', '--list-langs'], capture_output=True, text=True,
                                check=True, timeout=5)
        if not {'kor', 'eng'} <= set(result.stdout.split()):
            raise ValueError('DOCUMENT_OCR_UNAVAILABLE')
    except (OSError, subprocess.SubprocessError):
        raise ValueError('DOCUMENT_OCR_UNAVAILABLE') from None


def ocr_page(pdf_path: Path, page_number: int, directory: Path):
    """Render one bounded grayscale image and read it without external network calls."""
    deadline = monotonic() + OCR_PAGE_SECONDS
    prefix = directory / 'page'
    output = directory / 'recognized'
    try:
        subprocess.run(['pdftoppm', '-f', str(page_number), '-l', str(page_number),
            '-singlefile', '-scale-to', str(OCR_MAX_PIXELS), '-gray', '-png',
            str(pdf_path), str(prefix)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=max(0.01, deadline - monotonic()))
        subprocess.run(['tesseract', str(prefix) + '.png', str(output), '-l', 'kor+eng',
            '--psm', '3'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            env={**os.environ, 'OMP_THREAD_LIMIT': '1'}, timeout=max(0.01, deadline - monotonic()))
        with output.with_suffix('.txt').open(encoding='utf-8') as stream:
            text = stream.read(100001)
        if len(text) > 100000:
            raise ValueError('DOCUMENT_TEXT_LIMIT')
        return text
    except subprocess.TimeoutExpired:
        raise ValueError('DOCUMENT_OCR_TIMEOUT') from None
    except FileNotFoundError:
        raise ValueError('DOCUMENT_OCR_UNAVAILABLE') from None
    except (OSError, subprocess.SubprocessError):
        raise ValueError('DOCUMENT_OCR_FAILED') from None
    finally:
        prefix.with_suffix('.png').unlink(missing_ok=True)
        output.with_suffix('.txt').unlink(missing_ok=True)
