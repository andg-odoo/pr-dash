from pr_dash import cli, github
from tests.fakes import insert_pr

# --- shared GraphQL fragment -------------------------------------------------

def test_search_and_sibling_fetch_share_one_fragment():
    # The search query and the by-number fetch must reference the same fragment,
    # so their field selections can never drift.
    assert "fragment PRFields on PullRequest" in github.PR_NODE_FRAGMENT
    assert "...PRFields" in github.SEARCH_QUERY
    assert github.PR_NODE_FRAGMENT in github.SEARCH_QUERY


def test_cron_renders_without_consuming_the_since_last_look_baseline(tmp_path):
    from click.testing import CliRunner

    from pr_dash import db

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[user]\ngithub_login = "me"\n\n[repos]\n"odoo/odoo" = "/tmp/odoo"\n'
        f'\n[paths]\ncache_dir = "{tmp_path}"\n'
    )
    conn = db.connect(tmp_path / "pr_dash.db")
    insert_pr(conn, "odoo/odoo#1", head_sha="sha1")
    db.add_mine(conn, "odoo/odoo#2", "odoo/odoo", 2, "u", "t")
    conn.close()

    runner = CliRunner()
    args = ["refresh", "--offline", "--no-open", "--config", str(config_path)]
    assert runner.invoke(cli.cli, [*args, "--cron"]).exit_code == 0

    conn = db.connect(tmp_path / "pr_dash.db")
    assert (tmp_path / "index.html").exists()
    assert db.list_seen(conn) == {}
    assert db.list_tab_seen(conn, "mine") == {}

    # The same run for a human does look, so it advances the baseline.
    assert runner.invoke(cli.cli, args).exit_code == 0
    assert set(db.list_seen(conn)) == {"odoo/odoo#1"}
    assert set(db.list_tab_seen(conn, "mine")) == {"odoo/odoo#2"}
