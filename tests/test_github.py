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
