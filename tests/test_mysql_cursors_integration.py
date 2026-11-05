from test_mysql_auth_integration import mysql_client  # noqa: F401 - shared fixture
from test_shared_cursors import assert_invalid, assert_roundtrip, prepare_cursor_resources


def test_mysql_rejects_hostile_cursor_values_before_binding(mysql_client):
    """Reproduce both reported 500 errors against the actual production database dialect."""
    client, _, _, _ = mysql_client
    headers, endpoints, (path, data) = prepare_cursor_resources(client)
    for list_path, value in endpoints:
        for id_ in ('\ud800', '\udfff', 'bad\x00id'):
            assert_invalid(client, headers, list_path, {**value, 'id': id_})
    for number in (2**31, 10**1000):
        assert_invalid(client, headers, path, {**data, 'before': number})


def test_mysql_preserves_original_cursor_roundtrips(mysql_client):
    """Keep second-precision MySQL ordering and original wire formats working after validation."""
    client, _, _, _ = mysql_client
    headers, endpoints, versions = prepare_cursor_resources(client)
    assert_roundtrip(client, headers, endpoints, versions)
