from __future__ import annotations

import http.client
import importlib.util
import json
from pathlib import Path

import pytest

from pr_dash import hidden
from pr_dash.config import Config

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp extra not installed",
)


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(github_login="me", repos={}, cache_dir=tmp_path, **kw)


def _item(pr_id, head_sha, **over):
    repo, num = pr_id.split("#")
    item = {"id": pr_id, "head_sha": head_sha,
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
    from pr_dash import github, hidden
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    items = [{"id": "odoo/odoo#1", "head_sha": "stale",
              "members": [{"repo": "odoo/odoo", "number": 1}]}]
    mcp_server = _patch_cfg_and_items(monkeypatch, cfg, items)
    # The cached sha of an archived row can predate pushes; the hide must
    # record the live sha or it expires against it on the next reconcile.
    monkeypatch.setattr(github, "fetch_head_sha", lambda repo, number: "live")
    mcp_server.hide_pr("odoo/odoo#1")
    assert hidden.load(cfg)["odoo/odoo#1"]["head_sha"] == "live"


def test_hide_pr_falls_back_to_cached_sha(tmp_path, monkeypatch):
    from pr_dash import github, hidden
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    items = [{"id": "odoo/odoo#1", "head_sha": "stale",
              "members": [{"repo": "odoo/odoo", "number": 1}]}]
    mcp_server = _patch_cfg_and_items(monkeypatch, cfg, items)

    def _boom(repo, number):
        raise github.GithubError("offline")
    monkeypatch.setattr(github, "fetch_head_sha", _boom)
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


def _seeded_cfg(tmp_path, monkeypatch, seed):
    from pr_dash import db, mcp_server

    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    try:
        with db.transaction(conn):
            seed(conn)
    finally:
        conn.close()
    monkeypatch.setattr(mcp_server, "_cfg", cfg)
    return cfg, mcp_server


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
    assert (out["head_sha"], out["sibling_head_sha"]) == ("sha1", "")
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
    assert (out["head_sha"], out["sibling_head_sha"]) == ("sha2", "sha1")

    # The odoo half is its own row, not a replacement, and is stored pair-blind
    # because the enterprise diff was never cached - exactly what
    # _build_review_queue would have stored, so a refresh reads it as a cache hit.
    out = mcp_server.set_ai_review("odoo#1", summary="Odoo half.", verdict="minor")
    assert (out["head_sha"], out["sibling_head_sha"], out["replaced"]) == (
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
