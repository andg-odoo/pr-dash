import subprocess
import time

import pytest

from pr_dash import github


def _fake_run(errors, *, calls, stdout="ok"):
    """Fail with each stderr in `errors`, then return `stdout`."""
    def run(*a, **k):
        calls.append(1)
        if len(calls) <= len(errors):
            raise subprocess.CalledProcessError(1, ["gh"], "", errors[len(calls) - 1])
        return subprocess.CompletedProcess(["gh"], 0, stdout, "")
    return run


@pytest.fixture
def slept(monkeypatch):
    out = []
    monkeypatch.setattr(time, "sleep", out.append)
    return out


def test_retries_transient_error_with_backoff(monkeypatch, slept):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(["gh: HTTP 502", "stream error: CANCEL"], calls=calls))

    assert github._gh(["api", "graphql"]) == "ok"
    assert len(calls) == 3
    assert slept == [1, 2]


def test_does_not_retry_a_client_error(monkeypatch, slept):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(["gh: Not Found (HTTP 404)"], calls=calls))

    with pytest.raises(github.GithubError):
        github._gh(["api", "repos/o/r"])
    assert len(calls) == 1
    assert slept == []


def test_gives_up_after_max_attempts(monkeypatch, slept):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(["gh: HTTP 502"] * 5, calls=calls))

    with pytest.raises(github.GithubError, match="502"):
        github._gh(["api", "graphql"])
    assert len(calls) == github.MAX_ATTEMPTS


def _search_hit(number, ref):
    return {"number": number, "title": "t", "state": "OPEN", "isDraft": False,
            "url": f"u{number}", "headRefName": ref, "headRefOid": f"sha{number}",
            "author": {"login": "a"}}


def test_branch_search_keeps_only_exact_head_matches(monkeypatch):
    sent = {}

    def fake_graphql(query, variables):
        sent["query"] = query
        return {"b0": {"nodes": [_search_hit(2, "feat-x-followup"), _search_hit(1, "feat-x")]},
                "b1": {"nodes": [_search_hit(3, "feat-y-2")]}}

    monkeypatch.setattr(github, "_graphql", fake_graphql)

    out = github.search_open_prs_by_head_branch("odoo/upgrade", ["feat-x", "feat-y"])

    # `head:` is a filter, so a PR whose branch merely starts the same is a different bundle.
    assert out == {"feat-x": {"number": 1, "title": "t", "state": "OPEN", "draft": False,
                              "url": "u1", "head_branch": "feat-x", "head_sha": "sha1",
                              "author": "a"}}
    # Newest-first is the server's job, which is what keeps the tie-break the listing had.
    assert "sort:created-desc" in sent["query"]
