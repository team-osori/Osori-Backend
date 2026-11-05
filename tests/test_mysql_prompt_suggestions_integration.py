"""Opt-in real MySQL serialization; every provider/enterprise transport is replaced."""
import pytest
from sqlalchemy import func, select

from app.models.prompt import PromptVersion
from test_evaluations import prepare
from test_mysql_auth_integration import mysql_client  # noqa: F401
from test_mysql_connections_integration import concurrently
from test_prompt_suggestions import setup_suggestion, create, select_all, apply, application


@pytest.fixture
def mysql_suggested(mysql_client, monkeypatch):
    client, factory, _, _ = mysql_client
    headers, service, original, _ = prepare(factory, client, monkeypatch)
    body = {key: original[key] for key in ('connectionRevision', 'testSetVersionId')}
    return setup_suggestion((factory, client, headers, service, body, original))


def test_mysql_concurrent_suggestion_creation_calls_provider_once(mysql_suggested):
    responses = concurrently(lambda _: create(mysql_suggested))
    assert all(r.status_code in (200, 201, 202) for r in responses), [r.text for r in responses]
    assert len({r.json()['data']['id'] for r in responses}) == 1
    assert mysql_suggested[6].calls == 1


def test_mysql_duplicate_application_returns_one_immutable_version(mysql_suggested):
    factory, client, headers, *_ = mysql_suggested
    path, selected = select_all(mysql_suggested)
    responses = concurrently(lambda _: apply(mysql_suggested, selected))
    assert [r.status_code for r in responses] == [201, 201], [r.text for r in responses]
    assert len({r.json()['data']['id'] for r in responses}) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PromptVersion).where(
            PromptVersion.prompt_id == selected['snapshot']['promptId'])) == 3
    assert client.get(path, headers=headers).json()['data']['status'] == 'APPLIED'


def test_mysql_two_suggestions_cannot_both_replace_same_base(mysql_suggested):
    factory, client, headers, *_ = mysql_suggested
    paths_and_rows = [select_all(mysql_suggested, create(mysql_suggested, key=f'proposal-{i}')) for i in range(2)]
    responses = concurrently(lambda i: apply(mysql_suggested, paths_and_rows[i][1]))
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    states = [client.get(path, headers=headers).json()['data']['status'] for path, _ in paths_and_rows]
    assert sorted(states) == ['APPLIED', 'READY']


def test_mysql_selection_and_application_race_keeps_content_consistent(mysql_suggested):
    _, client, headers, *_ = mysql_suggested
    path, selected = select_all(mysql_suggested)
    def action(i):
        if i == 0:
            return apply(mysql_suggested, selected)
        return client.patch(path, headers=headers, json={'expectedRevision': selected['revision'], 'selectedSuggestionIds': []})
    responses = concurrently(action)
    assert sorted(r.status_code for r in responses) in ([200, 409], [201, 409]), [r.text for r in responses]
    current = client.get(path, headers=headers).json()['data']
    assert (current['status'] == 'APPLIED' and current['selectedSuggestionIds']) or (
        current['status'] == 'READY' and current['selectedSuggestionIds'] == [])


def test_mysql_json_results_and_full_flow(mysql_suggested):
    from test_prompt_suggestions import test_full_workflow_reuses_version_rerun_and_comparison
    test_full_workflow_reuses_version_rerun_and_comparison(mysql_suggested)
