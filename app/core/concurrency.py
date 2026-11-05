from sqlalchemy.exc import OperationalError


def is_write_conflict(exc: OperationalError) -> bool:
    """MySQL/MariaDB stale snapshot, deadlock, or lock timeout on a guarded write."""
    return bool(exc.orig.args and exc.orig.args[0] in (1020, 1205, 1213))
