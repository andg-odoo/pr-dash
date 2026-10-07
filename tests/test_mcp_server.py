from __future__ import annotations

import fcntl
import http.client
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

from pr_dash import hidden
from pr_dash.config import Config
from tests.fakes import FakeGitHub

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp extra not installed",
)


@pytest.fixture(autouse=True)
def _cancel_rerender_timer(monkeypatch):
    """A daemon timer left armed by one test must not fire into the next one,
    where the stub is gone and the real subprocess would run."""
    from pr_dash import mcp_server

    # Requesting monkeypatch keeps this stub in place until the timer below is joined.
    monkeypatch.setattr(mcp_server, "_run_rerender", lambda: True)
    yield
    with mcp_server._rerender_timer_lock:
        timer, mcp_server._rerender_timer = mcp_server._rerender_timer, None
    if timer is not None:
        timer.cancel()
        timer.join(5)


def _wait_for(pred, timeout=2.0):
    """Poll until the debounce timer has fired (or give up and let the assert
    report what actually happened)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(github_login="me", cache_dir=tmp_path, **kw)


def _item(pr_id, head_sha, **over):
    repo, num = pr_id.split("#")
    item = {"id": pr_id, "head_sha": head_sha, "heads_key": head_sha,
            "members": [{"repo_short": repo.split("/")[-1], "number": int(num),
                         "head_sha": head_sha}]}
    item.update(over)
    return item


# --- list_prs hidden filtering ----------------------------------------------


def _patch_cfg_and_items(monkeypatch, cfg, items):
    from pr_dash import mcp_server, query

    monkeypatch.setattr(mcp_server, "_cfg", cfg)
    monkeypatch.setattr(query, "load_items", lambda c: items)
    monkeypatch.setattr(query, "cache_fetched_at", lambda c: None)
    return mcp_server


def test_list_prs_excludes_hidden_by_default(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    items = [_item("odoo/odoo#1", "a"), _item("odoo/odoo#2", "b")]
    mcp_server = _patch_cfg_and_items(monkeypatch, cfg, items)
    hidden.save(cfg, {"odoo/odoo#1": {"head_sha": "a", "hidden_at": "t"}})

    out = mcp_server.list_prs()
    assert [p["id"] for p in out["prs"]] == ["odoo/odoo#2"]
    assert out["count"] == 1


def test_list_prs_include_hidden_flags_rows(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    items = [_item("odoo/odoo#1", "a"), _item("odoo/odoo#2", "b")]
    mcp_server = _patch_cfg_and_items(monkeypatch, cfg, items)
    hidden.save(cfg, {"odoo/odoo#1": {"head_sha": "a", "hidden_at": "t"}})

    out = mcp_server.list_prs(include_hidden=True)
    by_id = {p["id"]: p for p in out["prs"]}
    assert set(by_id) == {"odoo/odoo#1", "odoo/odoo#2"}
    assert by_id["odoo/odoo#1"].get("hidden") is True
    assert "hidden" not in by_id["odoo/odoo#2"]


def test_list_prs_prunes_hidden_on_push(tmp_path, monkeypatch):
    # A hide whose head_sha no longer matches is auto-dropped, so the PR is back.
    cfg = _cfg(tmp_path)
    items = [_item("odoo/odoo#1", "new")]
    mcp_server = _patch_cfg_and_items(monkeypatch, cfg, items)
    hidden.save(cfg, {"odoo/odoo#1": {"head_sha": "old", "hidden_at": "t"}})

    out = mcp_server.list_prs()
    assert [p["id"] for p in out["prs"]] == ["odoo/odoo#1"]
    # Prune was persisted.
    assert hidden.load(cfg) == {}


# --- listener ---------------------------------------------------------------


def test_hidden_listener_post_get_and_cors(tmp_path):
    from pr_dash import mcp_server

    cfg = _cfg(tmp_path, hidden_sync_port=0)
    server = mcp_server.start_hidden_listener(cfg)
    assert server is not None
    try:
        port = server.server_address[1]

        # POST an op -> written to disk, CORS header present.
        body = json.dumps({"ops": [
            {"op": "hide", "pr_id": "odoo/odoo#1", "head_sha": "abc", "hidden_at": "t"},
        ]})
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("POST", "/hidden", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Access-Control-Allow-Origin") == "*"
        assert json.loads(resp.read()) == {"ok": True, "count": 1}
        assert hidden.load(cfg) == {
            "odoo/odoo#1": {"head_sha": "abc", "hidden_at": "t"},
        }

        # GET returns the current map.
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/hidden")
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read()) == {
            "odoo/odoo#1": {"head_sha": "abc", "hidden_at": "t"},
        }

        # OPTIONS preflight advertises the methods/headers.
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("OPTIONS", "/hidden")
        resp = conn.getresponse()
        assert resp.status == 204
        assert resp.getheader("Access-Control-Allow-Methods") == "GET, POST, OPTIONS"
        assert resp.getheader("Access-Control-Allow-Headers") == "content-type"
    finally:
        server.shutdown()


def test_hidden_listener_second_bind_returns_none(tmp_path):
    from pr_dash import mcp_server

    first = mcp_server.start_hidden_listener(_cfg(tmp_path, hidden_sync_port=0))
    assert first is not None
    try:
        port = first.server_address[1]
        # A second instance on the same fixed port loses the race -> None.
        second = mcp_server.start_hidden_listener(_cfg(tmp_path, hidden_sync_port=port))
        assert second is None
    finally:
        first.shutdown()


def test_hide_pr_records_live_sha(tmp_path, monkeypatch):
    from pr_dash import hidden
    from pr_dash.config import Config

    cfg = Config(github_login="me", cache_dir=tmp_path)
    items = [{"id": "odoo/odoo#1", "author": "a", "head_branch": "b",
              "members": [{"repo": "odoo/odoo", "number": 1, "head_sha": "stale"},
                          {"repo": "odoo/enterprise", "number": 2, "head_sha": "stale"}]}]
    mcp_server = _patch_cfg_and_items(monkeypatch, cfg, items)
    # The cached sha of an archived row can predate pushes; the hide must
    # record the live sha or it expires against it on the next reconcile.
    gh = FakeGitHub()
    gh.add("odoo/odoo", 1, head_sha="live1")
    gh.add("odoo/enterprise", 2, head_sha="live2")
    monkeypatch.setattr(mcp_server, "_github", gh)
    mcp_server.hide_pr("odoo/odoo#1")
    assert hidden.load(cfg)["odoo/odoo#1"]["head_sha"] == "live1+live2"


def test_hide_pr_falls_back_to_cached_sha(tmp_path, monkeypatch):
    from pr_dash import hidden
    from pr_dash.config import Config

    cfg = Config(github_login="me", cache_dir=tmp_path)
    items = [{"id": "odoo/odoo#1", "author": "a", "head_branch": "b",
              "members": [{"repo": "odoo/odoo", "number": 1, "head_sha": "stale"}]}]
    mcp_server = _patch_cfg_and_items(monkeypatch, cfg, items)
    gh = FakeGitHub()
    gh.add("odoo/odoo", 1, head_sha="live1")
    gh.fail("head_sha")
    monkeypatch.setattr(mcp_server, "_github", gh)
    mcp_server.hide_pr("odoo/odoo#1")
    assert hidden.load(cfg)["odoo/odoo#1"]["head_sha"] == "stale"


# --- set_ai_review ----------------------------------------------------------

# These go through the real cache instead of a patched load_items: the point of
# a written row is that it comes back out of the renderer like a pipeline one,
# and only a real db exercises the pair-context validation on the way out.

def _seed_pr(conn, pr_id, head_sha, *, head_branch="feat", diff=None):
    from pr_dash import db

    repo, number = pr_id.split("#")
    conn.execute(
        "INSERT INTO pr (id, repo, number, title, url, author, target_branch, "
        "head_branch, head_sha, created_at, updated_at, review_requested_at, "
        "previously_reviewed, additions, deletions, changed_files, fetched_at) "
        "VALUES (?, ?, ?, 'A title', '', 'alice', '18.0', ?, ?, ?, ?, ?, 0, 1, 0, "
        "1, ?)",
        (pr_id, repo, int(number), head_branch, head_sha, *(["2026-07-01T00:00:00+00:00"] * 4)),
    )
    if diff is not None:
        db.upsert_diff(conn, head_sha, diff, False, "2026-07-01T00:00:00+00:00")


def _seeded_cfg(tmp_path, monkeypatch, seed, rerenders=None):
    from pr_dash import db, mcp_server

    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    try:
        with db.transaction(conn):
            seed(conn)
    finally:
        conn.close()
    monkeypatch.setattr(mcp_server, "_cfg", cfg)

    # Never spawn the real `pr-dash rerender` from a test: with no config path
    # set it would render the developer's own dashboard.
    def _fake_rerender():
        if rerenders is not None:
            rerenders.append(time.time())
        return True

    monkeypatch.setattr(mcp_server, "_run_rerender", _fake_rerender)
    monkeypatch.setattr(mcp_server, "_RERENDER_DEBOUNCE_S", 0.01)
    return cfg, mcp_server


def test_a_pair_hide_holds_until_either_half_moves(tmp_path, monkeypatch):
    def seed(conn):
        _seed_pr(conn, "odoo/odoo#1", "bbb")
        _seed_pr(conn, "odoo/enterprise#2", "aaa")
    cfg, mcp_server = _seeded_cfg(tmp_path, monkeypatch, seed)
    hidden.save(cfg, {"odoo/odoo#1": {"head_sha": "aaa+bbb", "hidden_at": "t"}})
    assert mcp_server.list_prs()["prs"] == []
    hidden.save(cfg, {"odoo/odoo#1": {"head_sha": "bbb+ccc", "hidden_at": "t"}})
    assert [p["id"] for p in mcp_server.list_prs()["prs"]] == ["odoo/odoo#1"]


def test_set_ai_review_writes_and_reads_back(tmp_path, monkeypatch):
    # The backfill case: a PR with no cached diff, so the pipeline never reviewed it.
    _, mcp_server = _seeded_cfg(
        tmp_path, monkeypatch, lambda c: _seed_pr(c, "odoo/odoo#1", "sha1"),
    )

    out = mcp_server.set_ai_review(
        "1", summary="Adds a tax hook.", verdict="minor",
        concerns=[
            {"severity": "nope", "message": "sudo() with no comment",
             "where": "sale/models/x.py"},
            {"severity": "high", "message": "   "},  # no message -> dropped
        ],
    )
    assert out["id"] == "odoo/odoo#1"
    assert (out["head_sha"], out["context_heads"]) == ("sha1", "")
    assert (out["verdict"], out["concern_count"], out["replaced"]) == ("minor", 1, False)

    back = mcp_server.get_ai_review("odoo/odoo#1")
    assert back["ai_review_verdict"] == "minor"
    assert back["ai_reviews"][0]["summary"] == "Adds a tax hook."
    # Unknown severity is clamped rather than rejected, same as model output.
    assert back["ai_reviews"][0]["concerns"] == [
        {"severity": "low", "message": "sudo() with no comment",
         "where": "sale/models/x.py"},
    ]


def test_set_ai_review_overwrites_in_place(tmp_path, monkeypatch):
    from pr_dash import db

    def seed(conn):
        _seed_pr(conn, "odoo/odoo#1", "sha1")
        db.upsert_ai_review(conn, "sha1", "", "auto", "[]", "looks-good", "t")

    cfg, mcp_server = _seeded_cfg(tmp_path, monkeypatch, seed)

    out = mcp_server.set_ai_review("1", summary="Actually breaks refunds.",
                                   verdict="major")
    assert out["replaced"] is True

    conn = db.connect(cfg.db_path)
    try:
        rows = conn.execute("SELECT * FROM ai_review").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert (rows[0]["verdict"], rows[0]["summary"]) == ("major", "Actually breaks refunds.")


def test_set_ai_review_records_sibling_pair_context(tmp_path, monkeypatch):
    from pr_dash import db

    def seed(conn):
        _seed_pr(conn, "odoo/odoo#1", "sha1", diff="diff --git a/a b/a\n")
        _seed_pr(conn, "odoo/enterprise#2", "sha2")

    cfg, mcp_server = _seeded_cfg(tmp_path, monkeypatch, seed)

    # The ref picks the half. resolve_item collapses either number onto the same
    # item, so an enterprise ref must still land on the enterprise head - and it
    # carries the odoo half as pair context, whose diff is cached.
    out = mcp_server.set_ai_review("enterprise#2", summary="Enterprise half.",
                                   verdict="looks-good")
    assert (out["head_sha"], out["context_heads"]) == ("sha2", "sha1")

    # The odoo half is its own row, not a replacement, and is stored pair-blind
    # because the enterprise diff was never cached - exactly what
    # _build_review_queue would have stored, so a refresh reads it as a cache hit.
    out = mcp_server.set_ai_review("odoo#1", summary="Odoo half.", verdict="minor")
    assert (out["head_sha"], out["context_heads"], out["replaced"]) == (
        "sha1", "", False,
    )

    # Both rows survive the renderer's pair-context check, so the pair reads as
    # fully reviewed and the item pill takes the worse of the two verdicts.
    back = mcp_server.get_ai_review("odoo/odoo#1")
    assert sorted(r["number"] for r in back["ai_reviews"]) == [1, 2]
    assert back["ai_review_verdict"] == "minor"

    # Caching the missing diff moves the odoo half's expected pair context, which
    # would normally drop the row as stale. A hand-written review is exempt: the
    # automatic pass will not replace it, so hiding it would lose it for good.
    conn = db.connect(cfg.db_path)
    try:
        with db.transaction(conn):
            db.upsert_diff(conn, "sha2", "diff --git a/b b/b\n", False, "t")
    finally:
        conn.close()
    back = mcp_server.get_ai_review("odoo/odoo#1")
    assert sorted(r["number"] for r in back["ai_reviews"]) == [1, 2]


def test_set_ai_review_rejects_unknown_verdict(tmp_path, monkeypatch):
    _, mcp_server = _seeded_cfg(
        tmp_path, monkeypatch, lambda c: _seed_pr(c, "odoo/odoo#1", "sha1"),
    )
    with pytest.raises(ValueError, match="verdict must be one of"):
        mcp_server.set_ai_review("1", summary="s", verdict="lgtm")


def test_set_ai_review_reports_what_it_replaced(tmp_path, monkeypatch):
    from pr_dash import db

    def seed(conn):
        _seed_pr(conn, "odoo/odoo#1", "sha1")
        db.upsert_ai_review(conn, "sha1", "", "earlier", "[]", "minor",
                            "2026-08-10T09:00:00+00:00", source="manual")

    _, mcp_server = _seeded_cfg(tmp_path, monkeypatch, seed)

    out = mcp_server.set_ai_review("1", summary="Mine.", verdict="major")
    # Which row lost, so a session can tell it just overwrote another agent's
    # fresh review rather than a stale automatic one.
    assert out["replaced"] is True
    assert out["replaced_source"] == "manual"
    assert out["replaced_at"] == "2026-08-10T09:00:00+00:00"


# --- dashboard re-render ----------------------------------------------------


def _request_rerender(mcp_server, cfg, count=1):
    """Bump the cross-process request counter without arming a timer, standing in
    for another session's write."""
    request, _, _ = mcp_server._rerender_paths(cfg)
    request.parent.mkdir(parents=True, exist_ok=True)
    with open(request, "ab") as fh:
        fh.write(b"\x01" * count)


def test_set_ai_review_rerenders_the_dashboard(tmp_path, monkeypatch):
    rerenders = []
    cfg, mcp_server = _seeded_cfg(
        tmp_path, monkeypatch, lambda c: _seed_pr(c, "odoo/odoo#1", "sha1"),
        rerenders=rerenders,
    )

    mcp_server.set_ai_review("1", summary="s", verdict="minor")
    assert _wait_for(lambda: len(rerenders) == 1)
    # Everything requested before the render started is recorded as covered, so
    # the next caller has nothing to do.
    assert mcp_server._covered(cfg) == mcp_server._requests(cfg) == 1


def test_rerender_coalesces_a_burst_of_writes(tmp_path, monkeypatch):
    rerenders = []

    def seed(conn):
        for n in (1, 2, 3):
            _seed_pr(conn, f"odoo/odoo#{n}", f"sha{n}")

    cfg, mcp_server = _seeded_cfg(tmp_path, monkeypatch, seed, rerenders=rerenders)
    monkeypatch.setattr(mcp_server, "_RERENDER_DEBOUNCE_S", 0.3)

    for n in (1, 2, 3):
        mcp_server.set_ai_review(str(n), summary="s", verdict="minor")

    assert _wait_for(lambda: len(rerenders) >= 1)
    time.sleep(0.4)
    assert len(rerenders) == 1
    assert mcp_server._covered(cfg) == 3


def test_rerender_skips_when_the_cache_has_not_moved(tmp_path, monkeypatch):
    rerenders = []
    cfg, mcp_server = _seeded_cfg(
        tmp_path, monkeypatch, lambda c: _seed_pr(c, "odoo/odoo#1", "sha1"),
        rerenders=rerenders,
    )
    _request_rerender(mcp_server, cfg, count=2)
    mcp_server._set_covered(cfg, 2)

    mcp_server._rerender(cfg)
    assert rerenders == []


def test_rerender_defers_to_a_render_running_elsewhere(tmp_path, monkeypatch):
    rerenders = []
    cfg, mcp_server = _seeded_cfg(
        tmp_path, monkeypatch, lambda c: _seed_pr(c, "odoo/odoo#1", "sha1"),
        rerenders=rerenders,
    )
    _, _, lock_path = mcp_server._rerender_paths(cfg)
    _request_rerender(mcp_server, cfg)

    # Stand in for another agent's MCP server mid-render. flock is per open file
    # description, so a second handle conflicts even inside one process.
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        mcp_server._rerender(cfg)
        assert rerenders == []
        # Not dropped: the timer is re-armed, and if the holder covers the
        # request first, the counter check makes that retry a no-op.
        assert mcp_server._rerender_timer is not None
    finally:
        os.close(fd)

    assert _wait_for(lambda: len(rerenders) == 1)


def test_rerender_does_another_pass_for_a_write_that_lands_mid_render(
    tmp_path, monkeypatch,
):
    rerenders = []
    cfg, mcp_server = _seeded_cfg(
        tmp_path, monkeypatch, lambda c: _seed_pr(c, "odoo/odoo#1", "sha1"),
        rerenders=rerenders,
    )
    _request_rerender(mcp_server, cfg)

    def _render_then_another_write():
        rerenders.append(time.time())
        if len(rerenders) == 1:
            # Another session commits while this render is reading the cache -
            # its data cannot be in the HTML this pass is about to write.
            _request_rerender(mcp_server, cfg)
        return True

    monkeypatch.setattr(mcp_server, "_run_rerender", _render_then_another_write)

    mcp_server._rerender(cfg)
    assert len(rerenders) == 2
    assert mcp_server._covered(cfg) == 2


# --- Authored PRs -------------------------------------------------------------

def _seed_mine(conn, pr_id, branch, *, state="OPEN", comments=()):
    from pr_dash import db

    repo, number = pr_id.split("#")
    db.add_mine(conn, pr_id, repo, int(number), f"https://github.com/{repo}/pull/{number}", "t")
    db.update_tab_state(conn, "mine", pr_id, {
        "title": f"PR {number}", "state": state, "head_branch": branch, "body": f"body {number}",
        "updated_at": "2026-10-05T00:00:00Z",
    })
    db.replace_discussion(conn, pr_id, [
        {"comment_id": f"c{i}", "kind": "issue", "author": author, "body": body,
         "created_at": f"2026-10-0{i + 1}T00:00:00Z"}
        for i, (author, body) in enumerate(comments)
    ])


def test_authored_prs_list_and_resolve_without_moving_the_baseline(tmp_path, monkeypatch):
    from pr_dash import db

    ec = "master-l10n_ec-6396725-andg"

    def seed(conn):
        _seed_pr(conn, "odoo/odoo#1", "sha1")
        _seed_mine(conn, "odoo/odoo#290109", ec, comments=[("jov-odoo", "Why here?")])
        _seed_mine(conn, "odoo/enterprise#132695", ec)
        _seed_mine(conn, "odoo/upgrade#11389", ec)
        _seed_mine(conn, "odoo/enterprise#1", "master-other-andg", state="CLOSED")
        _seed_mine(conn, "odoo/odoo#291981", "master-fw", comments=[("me", "Rebased.")])
        db.link_mine_forward_port(conn, "odoo/odoo#291981", "odoo/odoo#290109")

    cfg, mcp_server = _seeded_cfg(tmp_path, monkeypatch, seed)

    listed = mcp_server.list_mine()
    assert [(s["key"], s["band"], [(m["repo"], m["num"]) for m in s["members"]])
            for s in listed["branch_sets"]] == [
        (ec, "open",
         [("odoo/odoo", 290109), ("odoo/enterprise", 132695), ("odoo/upgrade", 11389)]),
        ("master-other-andg", "done", [("odoo/enterprise", 1)]),
    ]
    assert [s["key"] for s in mcp_server.list_mine(band="done")["branch_sets"]] == [
        "master-other-andg"]

    detail = mcp_server.get_mine("odoo#290109")
    assert detail["task"] == "6396725"
    odoo = next(m for m in detail["members"] if m["num"] == 290109)
    assert odoo["body"] == "body 290109"
    assert [(g["kind"], g["entry"]["author"], g["entry"]["body"]) for g in odoo["discussion"]] == [
        ("issue", "jov-odoo", "Why here?")]
    assert mcp_server.get_mine("https://github.com/odoo/upgrade/pull/11389")["key"] == ec

    # A Forward-port ref resolves to its Source PR's set, carrying its own discussion.
    by_fw = mcp_server.get_mine("odoo#291981")
    [fw] = next(m for m in by_fw["members"] if m["num"] == 290109)["fw"]
    assert (by_fw["key"], fw["ref"], [g["entry"]["body"] for g in fw["discussion"]]) == (
        ec, "odoo#291981", ["Rebased."])

    pr = mcp_server.get_pr("odoo/odoo#291981")
    assert (pr["key"], ["discussion" in m for m in pr["members"]]) == (ec, [False] * 3)
    assert "discussion" not in pr["members"][0]["fw"][0]
    assert mcp_server.get_comments("132695")["members"][0]["discussion"] == odoo["discussion"]
    # A bare number in the review queue still resolves there, the Authored PR needs its repo.
    assert mcp_server.get_pr("1")["id"] == "odoo/odoo#1"
    assert mcp_server.get_comments("1")["id"] == "odoo/odoo#1"
    assert mcp_server.get_pr("enterprise#1")["key"] == "master-other-andg"
    with pytest.raises(ValueError, match="No PR matching"):
        mcp_server.get_pr("99")

    conn = db.connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM mine_seen").fetchone()[0] == 0
    finally:
        conn.close()
