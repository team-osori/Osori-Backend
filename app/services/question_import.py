"""Bounded, atomic CSV parsing for reviewable test questions; never infer evidence."""
import csv
import io

from pydantic import ValidationError

from app.core.errors import ApiError
from app.schemas.policy import QuestionBody

MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 200
HEADERS = {'question': 'question', '질문': 'question', 'category': 'category', '유형': 'category',
           'modelAnswer': 'modelAnswer', '모범 답안': 'modelAnswer'}
CATEGORIES = {'정상': 'normal', '모호': 'ambiguous', '문서 없음': 'missing_document',
              '정책 함정': 'policy_trap'}


class CsvValidationError(ApiError):
    def __init__(self, errors):
        super().__init__(400, 'CSV_VALIDATION_ERROR', 'CSV 오류를 수정한 뒤 다시 업로드하세요. 저장된 문항은 없습니다.')
        self.field_errors = errors


def error(row, field, code, message):
    return {'field': f'rows.{row}.{field}', 'code': code, 'message': message}


def parse_questions(content: bytes) -> list[QuestionBody]:
    if len(content) > MAX_CSV_BYTES:
        raise ApiError(413, 'CSV_TOO_LARGE', 'CSV 파일은 최대 2MiB입니다.')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ApiError(400, 'CSV_ENCODING_INVALID', 'UTF-8 CSV 파일을 사용하세요.') from None
    if '\x00' in text:
        raise ApiError(400, 'CSV_FORMAT_INVALID', 'CSV에 NUL 문자를 사용할 수 없습니다.')
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    questions, errors = [], []
    try:
        raw_headers = next(reader, [])
        headers = [HEADERS.get(header.strip()) for header in raw_headers]
        if len(headers) != 3 or set(headers) != {'question', 'category', 'modelAnswer'}:
            raise CsvValidationError([error(1, 'header', 'CSV_HEADER_INVALID',
                'question,category,modelAnswer 또는 질문,유형,모범 답안 헤더가 필요합니다. 중복·추가 열은 허용하지 않습니다.')])
        records = 0
        while True:
            line = reader.line_num + 1
            values = next(reader, None)
            if values is None:
                break
            if not values:  # Truly empty physical lines, not empty cells.
                continue
            records += 1
            if records > MAX_CSV_ROWS:
                raise ApiError(400, 'CSV_ROW_LIMIT_EXCEEDED', 'CSV는 최대 200문항입니다.')
            if len(values) != len(headers):
                errors.append(error(line, 'row', 'CSV_COLUMN_COUNT', '헤더와 열 개수가 다릅니다. 쉼표가 든 값은 큰따옴표로 감싸세요.'))
                continue
            payload = dict(zip(headers, values))
            category = payload['category'].strip()
            payload['category'] = CATEGORIES.get(category, category)
            try:
                questions.append(QuestionBody.model_validate(payload))
            except ValidationError as exc:
                for item in exc.errors():
                    field = str(item['loc'][0]) if item['loc'] else 'row'
                    errors.append(error(line, field, 'CSV_VALUE_INVALID',
                        '지원하는 유형을 입력하세요.' if field == 'category' else '1~2000자의 공백이 아닌 값을 입력하세요.'))
    except csv.Error:
        errors.append(error(reader.line_num, 'row', 'CSV_FORMAT_INVALID', '큰따옴표 또는 CSV 형식을 확인하세요.'))
    if errors:
        raise CsvValidationError(errors)
    if not questions:
        raise ApiError(400, 'CSV_EMPTY', '가져올 문항이 없습니다.')
    return questions
