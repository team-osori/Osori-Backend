"""Bound incoming archives and reject links/path traversal before root extraction."""
from pathlib import Path, PurePosixPath
import sys
import tarfile

MAX_ARCHIVE = 50 * 1024 * 1024
MAX_EXPANDED = 200 * 1024 * 1024


def validate_members(members):
    size = 0
    for member in members:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts
                or not (member.isfile() or member.isdir())):
            raise ValueError('Unsafe archive member')
        size += member.size
        if size > MAX_EXPANDED:
            raise ValueError('Expanded release is too large')


def receive(stream, destination):
    archive = destination / 'source.tar.gz'
    total = 0
    with archive.open('wb') as target:
        while chunk := stream.read(65536):
            total += len(chunk)
            if total > MAX_ARCHIVE:
                raise ValueError('Release archive is too large')
            target.write(chunk)
    source = destination / 'source'
    source.mkdir()
    with tarfile.open(archive, 'r:gz') as bundle:
        members = bundle.getmembers()
        validate_members(members)
        bundle.extractall(source, members=members, filter='data')
    for required in ('app/main.py', 'requirements.lock', 'alembic.ini',
                     'deploy/manage.py', 'deploy/systemd/osori-api.service',
                     'deploy/systemd/osori-worker.service'):
        if not (source / required).is_file():
            raise ValueError('Incomplete release')


if __name__ == '__main__':
    receive(sys.stdin.buffer, Path(sys.argv[1]))
