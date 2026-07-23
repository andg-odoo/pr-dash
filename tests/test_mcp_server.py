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
