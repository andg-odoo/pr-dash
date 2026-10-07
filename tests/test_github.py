import json
import subprocess
import time

import pytest

from pr_dash import github
from tests.fakes import SHAPES, FakePR, pr_node


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


def _gh_answers(monkeypatch, answer):
    """Make every `gh` call print `answer(args, stdin)`, JSON-encoded unless it is a string."""
    def run(args, *, input=None, **kwargs):
        out = answer(args, input)
        return subprocess.CompletedProcess(
            args, 0, out if isinstance(out, str) else json.dumps(out), "")
    monkeypatch.setattr(subprocess, "run", run)


def _search_hit(number, ref):
    return {"number": number, "title": "t", "state": "OPEN", "isDraft": False,
            "url": f"u{number}", "headRefName": ref, "headRefOid": f"sha{number}",
            "author": {"login": "a"}}


def test_branch_search_keeps_only_exact_head_matches(monkeypatch):
    sent = []
    _gh_answers(monkeypatch, lambda args, stdin: sent.append(stdin) or {"data": {
        "b0": {"nodes": [_search_hit(2, "feat-x-followup"), _search_hit(1, "feat-x")]},
        "b1": {"nodes": [_search_hit(3, "feat-y-2")]}}})

    out = github.GhGitHub().open_prs_by_head_branch("odoo/upgrade", ["feat-x", "feat-y"])

    # `head:` is a filter, so a PR whose branch merely starts the same is a different bundle.
    assert out == {"feat-x": {"number": 1, "title": "t", "state": "OPEN", "draft": False,
                              "url": "u1", "head_branch": "feat-x", "head_sha": "sha1",
                              "author": "a"}}
    # Newest-first is the server's job, which is what keeps the tie-break the listing had.
    assert "sort:created-desc" in sent[0]


def test_a_truncated_partial_body_is_a_github_error(monkeypatch):
    _gh_answers(monkeypatch, lambda args, stdin: '{"data":{"p0"')

    with pytest.raises(github.GithubError, match="non-JSON"):
        github.GhGitHub().nodes([("odoo/odoo", 1)], "tracked")


def test_nodes_of_an_unknown_view_raise_instead_of_borrowing_one(monkeypatch):
    _gh_answers(monkeypatch, lambda args, stdin: {"data": {}})

    with pytest.raises(KeyError):
        github.GhGitHub().nodes([("odoo/odoo", 1)], "queu")


def _page(nodes, cursor=None):
    return {"totalCount": 0, "nodes": nodes,
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def test_complete_pages_follows_threads_and_their_replies(monkeypatch):
    def answer(args, stdin):
        if 'node(id: \\"PR\\")' in stdin:
            return {"data": {"c0": {"reviewThreads": _page(
                [{"id": "T2", "comments": _page([{"n": 3}], "r")}])}}}
        return {"data": {"c0": {"comments": _page([{"n": 4}])}}}

    _gh_answers(monkeypatch, answer)
    node = {"id": "PR", "reviews": _page([]), "comments": _page([]),
            "reviewThreads": _page([{"id": "T1", "comments": _page([{"n": 1}])}], "t")}

    github._complete_pages([node], github._PAGES)

    # A thread from a later page must still get its own replies paged in.
    threads = node["reviewThreads"]["nodes"]
    assert [t["id"] for t in threads] == ["T1", "T2"]
    assert threads[1]["comments"]["nodes"] == [{"n": 3}, {"n": 4}]


def test_manual_subscriptions_keep_each_pr_once(monkeypatch):
    # gh --jq streams one object per line, and a PR with several retained notifications repeats.
    def entry(path, updated):
        return json.dumps({"url": f"https://api.github.com/repos/{path}", "title": "[ADD] x",
                           "updated_at": updated, "repo": path.split("/pulls")[0]})

    _gh_answers(monkeypatch, lambda args, stdin: "\n".join([
        entry("odoo/odoo/pulls/264068", "2026-08-01T00:00:00Z"),
        entry("odoo/odoo/pulls/264068", "2026-08-02T00:00:00Z"),
        entry("odoo/enterprise/issues/99", "2026-08-01T00:00:00Z"),
        "",
        "not json",
    ]))

    assert github.GhGitHub().manual_subscriptions() == [{
        "id": "odoo/odoo#264068", "repo": "odoo/odoo", "number": 264068,
        "url": "https://github.com/odoo/odoo/pull/264068", "title": "[ADD] x",
        "updated_at": "2026-08-01T00:00:00Z"}]


def test_each_fake_node_carries_exactly_what_its_real_query_selects():
    pr = FakePR("odoo/odoo", 1, comments=[{"author": "a", "at": f"{i:02}", "body": str(i)}
                                          for i in range(12)])
    for view, shape in SHAPES.items():
        assert set(pr_node(pr, view)) == set(shape), view
    # The archived-row check reads the last ten comments, and backfill no discussion at all.
    assert [c["body"] for c in pr_node(pr, "activity")["comments"]["nodes"]] == [
        str(i) for i in range(2, 12)]
    assert {"body", "comments", "reviewThreads", "files"}.isdisjoint(SHAPES["reviewed_by"])
    assert "files" not in SHAPES["tracked"] and "closedAt" not in SHAPES["queue"]
