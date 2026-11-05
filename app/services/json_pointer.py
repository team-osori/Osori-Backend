import re


def tokens(pointer: str) -> list[str]:
    if not pointer.startswith('/') or re.search(r'~(?![01])', pointer):
        raise ValueError('루트가 아닌 JSON Pointer가 필요합니다.')
    return [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]


def key(container, token):
    if isinstance(container, list):
        if not re.fullmatch(r'0|[1-9][0-9]*', token):
            raise ValueError('잘못된 배열 인덱스입니다.')
        index = int(token)
        if index >= len(container):
            raise ValueError('배열 범위를 벗어났습니다.')
        return index
    if isinstance(container, dict) and token in container:
        return token
    raise ValueError('JSON 경로를 찾을 수 없습니다.')


def read_pointer(document, pointer):
    current = document
    for token in tokens(pointer):
        current = current[key(current, token)]
    return current


def write_pointer(document, pointer, value):
    parts = tokens(pointer)
    current = document
    for token in parts[:-1]:
        current = current[key(current, token)]
    current[key(current, parts[-1])] = value
