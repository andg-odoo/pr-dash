import fcntl
import os

import pytest
from click.testing import CliRunner

from pr_dash import cli, db, github
from tests.fakes import FakeGitHub, insert_pr

# --- shared GraphQL fragment -------------------------------------------------

def test_search_and_sibling_fetch_share_one_fragment():
    # The search query and the by-number fetch must reference the same fragment,
    # so their field selections can never drift.
    assert "fragment PRFields on PullRequest" in github.PR_NODE_FRAGMENT
    assert "...PRFields" in github.SEARCH_QUERY
    assert github.PR_NODE_FRAGMENT in github.SEARCH_QUERY


# --- commands over a fake GitHub ---------------------------------------------

@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(cli, "_github", fake)
    return fake


@pytest.fixture
def run(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[user]\ngithub_login = "me"\n\n[repos]\n"odoo/odoo" = "/tmp/odoo"\n'
        f'\n[paths]\ncache_dir = "{tmp_path}"\n'
    )

    def invoke(*args, stdin=None):
        return CliRunner().invoke(cli.cli, [*args, "--config", str(config_path)], input=stdin)
    return invoke


def test_cron_renders_without_consuming_the_since_last_look_baseline(tmp_path, run):
    conn = db.connect(tmp_path / "pr_dash.db")
    insert_pr(conn, "odoo/odoo#1", head_sha="sha1")
    db.add_mine(conn, "odoo/odoo#2", "odoo/odoo", 2, "u", "t")
    conn.close()

    args = ["refresh", "--offline", "--no-open"]
    assert run(*args, "--cron").exit_code == 0

    conn = db.connect(tmp_path / "pr_dash.db")
    assert (tmp_path / "index.html").exists()
    assert db.list_seen(conn) == {}
    assert db.list_tab_seen(conn, "mine") == {}

    # The same run for a human does look, so it advances the baseline.
    assert run(*args).exit_code == 0
    assert set(db.list_seen(conn)) == {"odoo/odoo#1"}
    assert set(db.list_tab_seen(conn, "mine")) == {"odoo/odoo#2"}


def test_a_github_failure_renders_the_cache_by_hand_and_fails_a_cron_tick(tmp_path, gh, run):
    html = tmp_path / "index.html"
    gh.fail("review_requested")
    result = run("refresh", "--no-open")
    assert result.exit_code == 0
    assert "review_requested: HTTP 502" in result.output
    assert "Falling back to cached data." in result.output
    assert html.exists()

    html.unlink()
    # Re-rendering would put an offline-bannered dashboard over a good one.
    assert run("refresh", "--cron").exit_code == 1
    assert not html.exists()
    assert "GitHub error: review_requested: HTTP 502" in (tmp_path / "cron.log").read_text()


def test_a_cron_tick_skips_while_another_refresh_holds_the_lock(tmp_path, gh, run):
    fd = os.open(tmp_path / "refresh.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert run("refresh", "--cron").exit_code == 0
    finally:
        os.close(fd)
    assert gh.calls == []
    assert not (tmp_path / "index.html").exists()


@pytest.mark.parametrize(("command", "method"), [
    ("backfill", "reviewed_by"), ("import-history", "authored_closed"),
])
def test_a_github_failure_fails_a_history_command_and_says_to_rerun(gh, run, command, method):
    gh.fail(method)
    result = run(command)
    assert result.exit_code == 1
    assert f"GitHub error: {method}: HTTP 502" in result.output
    assert f"Nothing was stored, run {command} again." in result.output


def test_track_names_each_pr_and_shows_why_its_state_was_not_fetched(tmp_path, gh, run):
    gh.add("odoo/odoo", 1, title="[FIX] x")
    result = run("track", "odoo/odoo#1")
    assert result.output.splitlines() == ["Tracking odoo/odoo#1", "1 new, 1 fetched"]

    gh.fail("nodes")
    result = run("track", "-", stdin="odoo/odoo#1\nhttps://github.com/odoo/odoo/pull/2\n")
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "odoo/odoo#1 already tracked", "Tracking odoo/odoo#2",
        "Tracked, but could not fetch state yet: nodes: HTTP 502",
    ]
    tracked = db.connect(tmp_path / "pr_dash.db")
    assert db.get_tracked(tracked, "odoo/odoo#1")["title"] == "[FIX] x"
    assert db.get_tracked(tracked, "odoo/odoo#2") is not None
