import json

from click.testing import CliRunner

from pr_dash import cli, config, db, github, mergebot, sync
from tests.fakes import FakeGitHub, FakePR, insert_pr, pr_node

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


def test_timer_ticks_search_the_review_queue_hourly(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    runs = []
    monkeypatch.setattr(sync.Sync, "refresh_queue", lambda self, **kw: runs.append(kw["cron"]))
    monkeypatch.setattr(cli, "_run_tracked_refresh", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_run_mine_refresh", lambda *a, **kw: None)
    tick = dict(no_open=True, force=False, offline=False, cron=True)

    cli._refresh(cfg, **tick)
    cli._refresh(cfg, **tick)
    cli._refresh(cfg, **{**tick, "cron": False})
    assert runs == [True, False]

    conn = db.connect(cfg.db_path)
    db.set_meta(conn, "last_queue_refresh", "2026-01-01T00:00:00Z")
    cli._refresh(cfg, **tick)
    assert runs == [True, False, True]


# --- _run_mine_refresh -------------------------------------------------------

def test_mine_refresh_keeps_resolved_members_and_stops_reading_their_final_page(
        tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[user]\ngithub_login = "me"\n\n[repos]\n"odoo/odoo" = "/tmp/odoo"\n'
        f'\n[paths]\ncache_dir = "{tmp_path}"\n',
    )
    cfg = config.load(config_path)
    gh = FakeGitHub()
    for repo, number in (("odoo/odoo", 10), ("odoo/upgrade", 20)):
        gh.add(repo, number, author="me", head_branch="master-x-6396725-andg")
    pages = {"odoo/odoo": "blocked", "odoo/upgrade": "unknown"}
    reads = []
    monkeypatch.setattr(github, "search_authored_open", gh.authored_open)
    monkeypatch.setattr(github, "fetch_nodes",
                        lambda refs, fragment, **kw: gh.nodes(refs, "mine"))
    monkeypatch.setattr(mergebot, "fetch", lambda repo, n: reads.append(repo)
                        or mergebot.MergebotState(pages[repo]))

    conn = db.connect(cfg.db_path)
    cli._run_mine_refresh(conn, cfg, force=False)
    # Fresh within mine_staleness_minutes, so the second run reads nothing.
    cli._run_mine_refresh(conn, cfg, force=False)
    assert sorted(reads) == ["odoo/odoo", "odoo/upgrade"]

    # Both left the open search and closed, the Mergebot telling Merged from closed.
    gh.close("odoo/odoo#10")
    gh.close("odoo/upgrade#20")
    pages["odoo/odoo"], pages["odoo/upgrade"] = "merged", "closed"
    cli._run_mine_refresh(conn, cfg, force=True)
    cli._run_mine_refresh(conn, cfg, force=True)
    assert sorted(reads) == ["odoo/odoo"] * 2 + ["odoo/upgrade"] * 2

    out = json.loads(CliRunner().invoke(
        cli.cli, ["query", "mine", "--config", str(config_path)]).output)
    assert [(s["key"], s["band"], [(m["num"], m["state"]) for m in s["members"]])
            for s in out["branch_sets"]] == [
        ("master-x-6396725-andg", "done", [(10, "MERGED"), (20, "CLOSED")])]


def test_mine_refresh_hangs_confirmed_forward_ports_under_their_source(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[user]\ngithub_login = "andg-odoo"\n\n[repos]\n"odoo/odoo" = "/tmp/odoo"\n'
        f'\n[paths]\ncache_dir = "{tmp_path}"\n',
    )
    cfg = config.load(config_path)
    body = "The new company now gets the contact's responsibility.\r\n\r\ntask-6470810\n\n"
    nodes = {pr.id: pr_node(pr, "mine") for pr in (
        FakePR("odoo/odoo", 290657, state="CLOSED",
               head_branch="saas-19.1-l10n_ar-company-arca-6470810-andg",
               cross_refs=[("odoo/odoo", 291857, "fw-bot"),
                           ("odoo/enterprise", 133776, "andg-odoo"),
                           ("odoo/odoo", 291981, "fw-bot"), ("odoo/odoo", 291000, "fw-bot")]),
        FakePR("odoo/odoo", 291857, state="CLOSED", base="20.0",
               body=body + "Forward-Port-Of: odoo/odoo#290657"),
        FakePR("odoo/odoo", 291981, base="master", updated_at="2026-10-05T00:00:00Z",
               body=body + "Forward-Port-Of: odoo/odoo#291857\nForward-Port-Of: odoo/odoo#290657"),
        FakePR("odoo/odoo", 291000, body="Forward-Port-Of: odoo/odoo#280000"),
    )}
    fetched = []
    monkeypatch.setattr(github, "search_authored_open", lambda login: [("odoo/odoo", 290657)])
    monkeypatch.setattr(github, "fetch_nodes", lambda refs, fragment, **kw: fetched.append(
        sorted(n for _, n in refs)) or {f"{r}#{n}": nodes[f"{r}#{n}"] for r, n in refs})
    monkeypatch.setattr(mergebot, "fetch", lambda repo, n: mergebot.MergebotState(
        "blocked" if n == 291981 else "merged"))

    conn = db.connect(cfg.db_path)
    cli._run_mine_refresh(conn, cfg, force=True)
    # Only bot cross-references are candidates, and only a matching Forward-Port-Of line joins.
    assert fetched == [[290657], [291000, 291857, 291981]]
    out = json.loads(CliRunner().invoke(
        cli.cli, ["query", "mine", "--config", str(config_path)]).output)
    [s] = out["branch_sets"]
    [source] = s["members"]
    assert (s["band"], s["fyi"], [(f["base"], f["ref"], f["state"]) for f in source["fw"]]) == (
        "open", ["source merged", "fw 1/2 merged"],
        [("20.0", "odoo#291857", "MERGED"), ("master", "odoo#291981", "OPEN")])

    # Linked Forward-ports ride in the members' batch from then on.
    fetched.clear()
    cli._run_mine_refresh(conn, cfg, force=True)
    assert fetched[0] == [290657, 291857, 291981]


def _history_cfg(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[user]\ngithub_login = "andg-odoo"\n\n[repos]\n"odoo/odoo" = "/tmp/odoo"\n'
        f'\n[paths]\ncache_dir = "{tmp_path}"\n',
    )
    return config_path


def test_import_history_dismisses_resolved_sets_once(tmp_path, monkeypatch):
    config_path = _history_cfg(tmp_path)
    nodes = {pr.id: pr_node(pr, "mine") for pr in (
        FakePR("odoo/odoo", 100, head_branch="master-live"),
        FakePR("odoo/odoo", 10, state="CLOSED", head_branch="19.0-old"),
        FakePR("odoo/odoo", 290657, state="CLOSED", head_branch="saas-19.1-arca",
               cross_refs=[("odoo/odoo", 291981, "fw-bot")]),
        FakePR("odoo/odoo", 291981, base="master", body="Forward-Port-Of: odoo/odoo#290657"),
    )}
    fetched = []
    monkeypatch.setattr(github, "search_authored_open", lambda login: [("odoo/odoo", 100)])
    monkeypatch.setattr(github, "search_authored_closed",
                        lambda login: [("odoo/odoo", 10), ("odoo/odoo", 290657)])
    monkeypatch.setattr(github, "fetch_nodes", lambda refs, fragment, **kw: fetched.extend(refs) or {
        f"{r}#{n}": nodes[f"{r}#{n}"] for r, n in refs})
    monkeypatch.setattr(mergebot, "fetch", lambda repo, n: mergebot.MergebotState(
        "blocked" if nodes[f"{repo}#{n}"]["state"] == "OPEN" else "merged"))
    conn = db.connect(config.load(config_path).db_path)
    cli._run_mine_refresh(conn, config.load(config_path), force=True)

    def run():
        return CliRunner().invoke(cli.cli, ["import-history", "--config", str(config_path)])

    def mine(*flags):
        out = json.loads(CliRunner().invoke(
            cli.cli, ["query", "mine", *flags, "--config", str(config_path)]).output)
        return [(s["key"], s["band"]) for s in out["branch_sets"]]

    def snapshot():
        return [tuple(r) for r in conn.execute(
            "SELECT id, dismissed_at, fetched_at FROM mine ORDER BY id")]

    assert run().exit_code == 0
    # The Chain with an open Forward-port stays visible, the pre-existing open set is untouched.
    assert mine() == [("master-live", "needs"), ("saas-19.1-arca", "open")]
    assert mine("--include-dismissed") == [
        ("master-live", "needs"), ("saas-19.1-arca", "open"), ("19.0-old", "done")]

    before, fetched[:] = snapshot(), []
    result = run()
    assert (result.exit_code, "already imported" in result.output) == (0, True)
    assert (snapshot(), fetched) == (before, [])


def test_import_history_failure_stores_nothing_and_can_rerun(tmp_path, monkeypatch):
    config_path = _history_cfg(tmp_path)
    monkeypatch.setattr(github, "search_authored_closed", lambda login: [("odoo/odoo", 10)])
    monkeypatch.setattr(mergebot, "fetch", lambda repo, n: mergebot.MergebotState("merged"))

    source = pr_node(FakePR("odoo/odoo", 10, state="CLOSED",
                            cross_refs=[("odoo/odoo", 11, "fw-bot")]), "mine")
    fw = pr_node(FakePR("odoo/odoo", 11, state="CLOSED", body="Forward-Port-Of: odoo/odoo#10"),
                 "mine")

    def fail_on_forward_ports(refs, fragment, **kw):
        if refs == [("odoo/odoo", 11)]:
            msg = "HTTP 502"
            raise github.GithubError(msg)
        return {"odoo/odoo#10": source}

    # The source batch succeeds and the Forward-port batch fails, so a partial write would show.
    monkeypatch.setattr(github, "fetch_nodes", fail_on_forward_ports)
    result = CliRunner().invoke(cli.cli, ["import-history", "--config", str(config_path)])
    conn = db.connect(config.load(config_path).db_path)
    assert (result.exit_code, "run import-history again" in result.output) == (1, True)
    assert (db.list_mine(conn, include_dismissed=True),
            db.get_meta(conn, "mine_history_imported")) == ([], None)

    monkeypatch.setattr(github, "fetch_nodes", lambda refs, fragment, **kw: {
        f"{r}#{n}": {10: source, 11: fw}[n] for r, n in refs})
    assert CliRunner().invoke(
        cli.cli, ["import-history", "--config", str(config_path)]).exit_code == 0
    assert [(r["id"], r["source_id"], r["dismissed_at"] is not None)
            for r in db.list_mine(conn, include_dismissed=True)] == [
        ("odoo/odoo#10", None, True), ("odoo/odoo#11", "odoo/odoo#10", False)]
