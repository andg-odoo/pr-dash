import pytest

from pr_dash import db, github, render, sync, tab
from pr_dash.config import Config
from pr_dash.sync import Sync
from tests.fakes import T0, FakeClock, FakeGitHub, FakeMergebot, FakeReviewer, insert_pr

ODOO, ENT = "odoo/odoo#1", "odoo/enterprise#2"
# The call that shows each refresh phase ran, the Tracked one being its nodes view.
QUEUE, TRACKED, MINE = "review_requested", "tracked", "authored_open"


def _difffile(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


CODE = _difffile("m/models/x.py", "+code\n")


class World:
    """A cache, the fake GitHub, Mergebot and reviewer behind it, and one clock for all."""

    def __init__(self, tmp_path):
        self.cfg = Config(github_login="me", cache_dir=tmp_path)
        self.conn = db.connect(self.cfg.db_path)
        self.clock = FakeClock()
        self.gh = FakeGitHub(self.clock)
        self.mergebot = FakeMergebot()
        self.reviewer = FakeReviewer()
        self.sync = Sync(self.conn, self.cfg, self.gh, read_mergebot=self.mergebot,
                         reviewer=self.reviewer, clock=self.clock)

    def refresh(self, *, force=False, cron=False):
        return self.sync.refresh(force=force, cron=cron)

    def mine(self, *, include_dismissed=False):
        sets, _ = render.build_mine_payload(self.conn, "me", include_dismissed=include_dismissed,
                                            now=self.clock().isoformat())
        return sets

    def row(self, pr_id):
        return db.get_cached_pr(self.conn, pr_id)

    def reviewed(self):
        return [r.head_sha for r in self.reviewer.requests]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


# --- gates and failures ------------------------------------------------------

def test_timer_ticks_search_the_queue_hourly_and_each_tab_when_stale(w):
    w.gh.add("odoo/odoo", 1, author="me")
    w.gh.add("odoo/odoo", 2, subscribed=True)

    def tick(*, cron=True, force=False):
        w.gh.calls.clear()
        w.refresh(cron=cron, force=force)
        return {c[2] if c[0] == "nodes" else c[0] for c in w.gh.calls} & {QUEUE, TRACKED, MINE}

    assert tick() == {QUEUE, TRACKED, MINE}
    assert tick() == set()
    assert tick(cron=False) == {QUEUE}
    # Each gate keeps a minute of slack but the tracked one, a tick lands seconds short.
    w.clock.advance(minutes=14)
    assert tick() == {MINE}
    w.clock.advance(minutes=45)
    assert tick() == {QUEUE, MINE}
    w.clock.advance(hours=6)
    assert tick() == {QUEUE, TRACKED, MINE}
    assert tick(force=True) == {QUEUE, TRACKED, MINE}


def test_a_tab_failure_is_a_warning_and_a_queue_failure_skips_the_tabs(w):
    w.gh.fail("manual_subscriptions")
    w.gh.fail("authored_open")
    report = w.refresh()
    assert report.warnings == ["Could not read subscriptions: manual_subscriptions: HTTP 502",
                               "Authored PR refresh failed: authored_open: HTTP 502"]
    assert report.mine_refreshed is None

    stamped = db.get_meta(w.conn, "last_refresh")
    w.gh.fail("review_requested")
    w.gh.calls.clear()
    w.clock.advance(hours=1)
    with pytest.raises(github.GithubError):
        w.refresh(force=True)
    assert [c[0] for c in w.gh.calls] == ["review_requested"]
    assert db.get_meta(w.conn, "last_refresh") == stamped


# --- archived rows -----------------------------------------------------------

def test_an_archived_row_follows_its_pr_on_github(w):
    w.gh.add("odoo/odoo", 1, requested=["me"])
    w.refresh()
    w.clock.advance(hours=1)
    w.gh.review(ODOO, "me")
    w.refresh()
    archived_at = w.row(ODOO)["archived_at"]
    assert archived_at and w.row(ODOO)["previously_reviewed"] == 1

    # Nothing moved on GitHub, so nothing is fetched beyond the activity check.
    w.gh.calls.clear()
    assert w.refresh().refreshed == 0
    assert not [c for c in w.gh.calls if c[0] == "nodes" and c[2] == "queue"]

    # A comment alone bumps updatedAt, which refetches the row in place without a push marker.
    w.clock.advance(hours=1)
    w.gh.comment(ODOO, "alice", "done, ready for r+")
    report = w.refresh()
    row = w.row(ODOO)
    assert (report.pinged, report.refreshed, report.pushed) == (1, 1, 0)
    assert (row["ping_author"], row["push_at"]) == ("alice", None)
    assert row["ping_at"] == row["updated_at"] == "2026-07-01T02:00:00Z"
    assert "ready" in row["ping_snippet"]
    assert ODOO in db.list_discussions(w.conn, "pr")

    w.clock.advance(hours=1)
    w.gh.push(ODOO, "sha2")
    w.refresh()
    row = w.row(ODOO)
    assert (row["push_at"], row["push_sha"]) == ("2026-07-01T03:00:00Z", "sha2")
    # The moved head pulls in the whole row, its diff and complexity, and it stays archived.
    assert row["head_sha"] == "sha2"
    assert db.get_diff(w.conn, "sha2") is not None
    assert db.get_complexity(w.conn, "sha2") is not None
    assert (row["archived_at"], row["previously_reviewed"]) == (archived_at, 1)

    w.clock.advance(hours=1)
    w.gh.review(ODOO, "me")
    w.refresh()
    assert (w.row(ODOO)["push_at"], w.row(ODOO)["ping_at"]) == (None, None)

    w.clock.advance(hours=1)
    w.gh.comment(ODOO, "alice", "again?")
    w.refresh()
    assert w.row(ODOO)["ping_at"]
    w.gh.close(ODOO, "MERGED")
    assert w.refresh().closed == 1
    assert (w.row(ODOO)["state"], w.row(ODOO)["ping_at"]) == ("MERGED", None)


def test_a_hidden_archived_pr_is_never_pinged_until_a_push_ends_the_hide(w):
    w.gh.add("odoo/odoo", 1, requested=["me"])
    w.refresh()
    w.clock.advance(hours=1)
    w.gh.review(ODOO, "me")
    w.refresh()
    db.set_mark(w.conn, "hide", ODOO, "sha1", "t")
    w.clock.advance(hours=1)
    w.gh.comment(ODOO, "alice", "done, ready for r+")
    assert w.refresh().pinged == 0
    assert w.row(ODOO)["ping_at"] is None
    assert list(db.marks(w.conn, "hide")) == [ODOO]

    w.clock.advance(hours=1)
    w.gh.push(ODOO, "sha2")
    w.refresh()
    assert db.marks(w.conn, "hide") == {}


# --- reviewed Branch set halves ----------------------------------------------

def test_a_reviewed_half_of_an_active_set_is_kept_and_primed(w):
    w.gh.add("odoo/odoo", 1, requested=["me"])
    w.gh.add("odoo/enterprise", 2, requested=["me"])
    w.refresh()
    w.clock.advance(hours=1)
    w.gh.review(ENT, "me")
    w.gh.comment(ENT, "someone", "why?")
    assert w.refresh().primed == 1
    assert w.row(ENT)["archived_at"] is None
    assert ENT in db.list_discussions(w.conn, "pr")

    # Unchanged with its comments cached, the half is kept without being re-persisted.
    assert w.refresh().primed == 0
    assert w.row(ENT)["archived_at"] is None
    assert w.refresh(force=True).primed == 1

    # The node's timeline window can lose my review, which must not make the row deletable.
    w.gh.prs[ENT].reviews.clear()
    w.gh.push(ENT, "sha2")
    assert w.refresh().primed == 1
    assert (w.row(ENT)["head_sha"], w.row(ENT)["previously_reviewed"]) == ("sha2", 1)


def test_priming_an_archived_half_keeps_it_archived(w):
    w.gh.add("odoo/enterprise", 2, requested=["me"])
    w.refresh()
    w.clock.advance(hours=1)
    w.gh.review(ENT, "me")
    w.refresh()
    archived_at = w.row(ENT)["archived_at"]
    assert archived_at

    # Its partner reaches the queue, so the half joins an active set and is primed.
    w.gh.add("odoo/odoo", 1, requested=["me"])
    w.clock.advance(hours=1)
    w.gh.comment(ENT, "someone", "why?")
    assert w.refresh().primed == 1
    assert w.row(ENT)["archived_at"] == archived_at


def test_a_half_the_migration_left_unfetched_is_primed_once(w):
    insert_pr(w.conn, ENT, fetched_at=db.UNFETCHED)
    w.gh.add("odoo/enterprise", 2, comments=[{"author": "x", "at": T0, "body": "b"}])
    w.gh.add("odoo/odoo", 1, requested=["me"])
    assert w.refresh().primed == 1
    assert w.refresh().primed == 0


def test_a_queue_rows_thread_signals_follow_its_discussion(w):
    w.gh.add("odoo/odoo", 1, requested=["me"],
             comments=[{"author": "robodoo", "at": T0, "body": "staged"}],
             reviews=[{"id": "RJ", "author": "jov", "state": "COMMENTED", "at": T0, "commit": "s"},
                      {"author": "clbr", "state": "APPROVED", "at": T0, "commit": "s"},
                      {"author": "me", "state": "PENDING", "at": T0, "commit": "s"}],
             threads=[{"id": "T1", "path": "a.py", "comments": [
                 {"author": "jov", "at": T0, "body": "why?", "review": "RJ"},
                 {"author": "me", "at": T0, "body": "because"}]}])

    def look():
        w.clock.advance(minutes=1)
        w.refresh()
        items[:], seen = render.build_payload(w.conn, "me", 30)
        render.commit_seen_baseline(w.conn, seen, w.clock().isoformat())
        row = w.row(ODOO)
        return (row["unresolved_threads"], row["awaiting_my_reply"], row["my_pending_review"],
                items[0]["since_last_look"])

    items = []

    assert look() == (1, 0, 1, [])
    w.gh.reply(ODOO, "T1", "jov", "still why?")
    assert look() == (1, 1, 1, ["reply"])
    w.gh.reply(ODOO, "T1", "me", "fixed")
    assert look()[:2] == (1, 0)
    w.gh.resolve(ODOO, "T1")
    assert look()[:2] == (0, 0)

    # The envelope review is dropped, the bodiless approval kept, the bot flagged.
    discussion = tab.get_comments(w.cfg, "odoo#1")["discussion"]
    assert discussion == items[0]["discussion"]
    assert discussion[0]["entry"]["member"] == "odoo"
    assert [(g["kind"], g["entry"] and (g["entry"]["author"], g["entry"]["is_bot"],
                                        g["entry"]["is_pending"]))
            for g in discussion] == [
        ("issue", ("robodoo", True, False)), ("review", ("clbr", False, False)),
        ("review", ("me", False, True)), ("orphan", None)]


# --- Companions --------------------------------------------------------------

def test_a_companion_attaches_to_every_half_on_its_branch(w):
    for repo, number, branch in (("odoo/odoo", 1, "feat-x"), ("odoo/enterprise", 2, "feat-x"),
                                 ("odoo/odoo", 3, "other")):
        w.gh.add(repo, number, author="jdoe", head_branch=branch, requested=["me"])
    # Its author is not part of the match, migrations are often written by someone else.
    w.gh.add("odoo/upgrade", 900, head_branch="feat-x", head_sha="usha", author="someone-else")

    assert w.refresh().companions == 2
    for pr_id in (ODOO, ENT):
        row = db.get_companion(w.conn, pr_id)
        assert (row["repo"], row["number"], row["state"]) == ("odoo/upgrade", 900, "OPEN")
    assert db.get_companion(w.conn, "odoo/odoo#3") is None
    assert {r["id"] for r in db.list_prs(w.conn)} == {ODOO, ENT, "odoo/odoo#3"}


def test_an_unreachable_or_disabled_companion_repo_is_reported_as_unsearched(w):
    w.gh.add("odoo/odoo", 1, requested=["me"], head_branch="feat-x", patch=CODE)
    w.gh.add("odoo/upgrade", 900, head_branch="feat-x")
    w.gh.fail("open_prs_by_head_branch")
    w.refresh()
    # The prompt may only call a migration absent when one was actually looked for.
    assert w.reviewer.requests[0].companion_repo == ""
    assert db.get_companion(w.conn, ODOO) is None

    w.cfg.companion.enabled = False
    w.gh.calls.clear()
    w.refresh()
    assert "open_prs_by_head_branch" not in [c[0] for c in w.gh.calls]


def test_an_archived_prs_stored_migration_is_rechecked_not_searched(w):
    w.gh.add("odoo/odoo", 1, requested=["me"], head_branch="feat-x")
    w.gh.add("odoo/enterprise", 9, requested=["me"], head_branch="old-feat")
    w.gh.add("odoo/upgrade", 800, head_branch="old-feat", head_sha="osha")
    w.refresh()
    w.clock.advance(hours=1)
    w.gh.review("odoo/enterprise#9", "me")
    w.refresh()
    w.gh.close("odoo/upgrade#800", "MERGED")
    w.gh.calls.clear()
    w.refresh()

    assert [c[2] for c in w.gh.calls if c[0] == "open_prs_by_head_branch"] == [["feat-x"]]
    row = db.get_companion(w.conn, "odoo/enterprise#9")
    assert (row["number"], row["state"]) == (800, "MERGED")


# --- Mine and the history import ---------------------------------------------

def test_mine_keeps_resolved_members_and_stops_reading_their_final_page(w):
    for repo, number in (("odoo/odoo", 10), ("odoo/upgrade", 20)):
        w.gh.add(repo, number, author="me", head_branch="master-x-6396725-andg")
    w.mergebot.pages = {"odoo/odoo#10": "blocked"}
    w.refresh()
    w.refresh()
    assert sorted(w.mergebot.reads) == ["odoo/odoo#10", "odoo/upgrade#20"]

    # Both left the open search and closed, the Mergebot telling Merged from closed.
    w.gh.close("odoo/odoo#10")
    w.gh.close("odoo/upgrade#20")
    w.mergebot.pages = {"odoo/odoo#10": "merged", "odoo/upgrade#20": "closed"}
    w.refresh(force=True)
    w.refresh(force=True)
    assert sorted(w.mergebot.reads) == ["odoo/odoo#10"] * 2 + ["odoo/upgrade#20"] * 2
    assert [(s["key"], s["band"], [(m["num"], m["state"]) for m in s["members"]])
            for s in w.mine()] == [
        ("master-x-6396725-andg", "done", [(10, "MERGED"), (20, "CLOSED")])]


def test_mine_hangs_confirmed_forward_ports_under_their_source(w):
    src_id = "odoo/odoo#290657"
    source = w.gh.add("odoo/odoo", 290657, author="me",
                      head_branch="saas-19.1-l10n_ar-company-arca-6470810-andg")
    w.refresh()
    w.gh.close(src_id)
    source.cross_refs = [("odoo/odoo", 291857, "fw-bot"), ("odoo/enterprise", 133776, "me"),
                         ("odoo/odoo", 291981, "fw-bot"), ("odoo/odoo", 291000, "fw-bot")]
    body = "The new company now gets the contact's responsibility.\r\n\r\ntask-6470810\n\n"
    w.gh.add("odoo/odoo", 291857, state="CLOSED", base="20.0",
             body=body + "Forward-Port-Of: odoo/odoo#290657")
    w.gh.add("odoo/odoo", 291981, base="master",
             body=body + "Forward-Port-Of: odoo/odoo#291857\nForward-Port-Of: odoo/odoo#290657")
    w.gh.add("odoo/odoo", 291000, body="Forward-Port-Of: odoo/odoo#280000")
    w.mergebot.pages = {src_id: "merged", "odoo/odoo#291857": "merged",
                        "odoo/odoo#291981": "blocked"}

    def fetched():
        return [sorted(n for _, n in c[1]) for c in w.gh.calls
                if c[0] == "nodes" and c[2] == "mine"]

    w.gh.calls.clear()
    w.refresh(force=True)
    # Only bot cross-references are candidates, and only a matching Forward-Port-Of line joins.
    assert fetched() == [[290657], [291000, 291857, 291981]]
    [s] = w.mine()
    [member] = s["members"]
    assert (s["band"], s["fyi"], [(f["base"], f["ref"], f["state"]) for f in member["fw"]]) == (
        "open", ["source merged", "fw 1/2 merged"],
        [("20.0", "odoo#291857", "MERGED"), ("master", "odoo#291981", "OPEN")])

    # Linked Forward-ports ride in the members' batch from then on.
    w.gh.calls.clear()
    w.refresh(force=True)
    assert fetched()[0] == [290657, 291857, 291981]


def test_the_history_import_dismisses_resolved_sets_once(w):
    w.gh.add("odoo/odoo", 100, author="me", head_branch="master-live")
    w.gh.add("odoo/odoo", 10, author="me", state="CLOSED", head_branch="19.0-old")
    w.gh.add("odoo/odoo", 290657, author="me", state="CLOSED", head_branch="saas-19.1-arca",
             cross_refs=[("odoo/odoo", 291981, "fw-bot")])
    w.gh.add("odoo/odoo", 291981, base="master", body="Forward-Port-Of: odoo/odoo#290657")
    w.mergebot.pages = {"odoo/odoo#100": "blocked", "odoo/odoo#10": "merged",
                        "odoo/odoo#290657": "merged", "odoo/odoo#291981": "blocked"}
    w.refresh()

    w.gh.calls.clear()
    report = w.sync.import_history()
    assert (report.closed, report.forward_ports, report.dismissed, report.left_open,
            report.unread) == (2, 1, 1, 1, 0)
    # Closed PRs are fetched through the narrow history batches, never the Mine ones.
    assert {c[2] for c in w.gh.calls if c[0] == "nodes"} == {"history"}
    # The Chain with an open Forward-port stays visible, the pre-existing open set is untouched.
    assert [(s["key"], s["band"]) for s in w.mine()] == [
        ("master-live", "needs"), ("saas-19.1-arca", "open")]
    assert [(s["key"], s["band"]) for s in w.mine(include_dismissed=True)] == [
        ("master-live", "needs"), ("saas-19.1-arca", "open"), ("19.0-old", "done")]

    before = [tuple(r) for r in w.conn.execute("SELECT * FROM mine ORDER BY id")]
    w.gh.calls.clear()
    assert w.sync.import_history() is None
    assert [tuple(r) for r in w.conn.execute("SELECT * FROM mine ORDER BY id")] == before
    assert w.gh.calls == []


def test_a_failed_history_import_stores_nothing_and_can_rerun(w):
    w.gh.add("odoo/odoo", 10, author="me", state="CLOSED", cross_refs=[("odoo/odoo", 11, "fw-bot")])
    w.gh.add("odoo/odoo", 11, state="CLOSED", body="Forward-Port-Of: odoo/odoo#10")
    w.mergebot.pages = {"odoo/odoo#10": "merged", "odoo/odoo#11": "merged"}
    # The source batch succeeds and the Forward-port batch fails, so a partial write would show.
    w.gh.fail("nodes", lambda refs, view: refs == [("odoo/odoo", 11)])
    with pytest.raises(github.GithubError):
        w.sync.import_history()
    assert (db.list_mine(w.conn, include_dismissed=True),
            db.get_meta(w.conn, "mine_history_imported")) == ([], None)

    w.gh.fail("nodes", lambda *args: False)
    w.sync.import_history()
    assert [(r["id"], r["source_id"], r["dismissed_at"] is not None)
            for r in db.list_mine(w.conn, include_dismissed=True)] == [
        ("odoo/odoo#10", None, True), ("odoo/odoo#11", "odoo/odoo#10", False)]


# --- backfill and tracking by hand ------------------------------------------

def test_backfill_archives_past_reviews_and_leaves_live_rows_to_the_refresh(w):
    def reviewed(state, at="2026-06-01T00:00:00Z"):
        return [{"author": "me", "state": state, "at": at, "commit": "sha1"}]

    w.gh.add("odoo/odoo", 1, reviews=reviewed("COMMENTED") + reviewed("APPROVED", T0))
    w.gh.add("odoo/odoo", 2, author="me", reviews=reviewed("COMMENTED"))
    w.gh.add("odoo/odoo", 3, requested=["me"])
    w.gh.add("odoo/odoo", 4, state="MERGED", reviews=reviewed("CHANGES_REQUESTED"))
    w.refresh()
    w.gh.review("odoo/odoo#3", "me")
    insert_pr(w.conn, "odoo/odoo#4", archived_at=T0)

    report = w.sync.backfill(since=None, limit=1000)
    assert (report.added, report.updated, report.skipped, report.self_authored,
            report.no_review) == (1, 1, 1, 1, 0)
    # Keyed by my latest review, which stands for when the PR left my queue.
    row = w.row("odoo/odoo#1")
    assert (row["archived_at"], row["previously_reviewed"], row["state"]) == (T0, 1, "OPEN")
    assert (w.row("odoo/odoo#3")["archived_at"], w.row("odoo/odoo#4")["state"]) == (None, "MERGED")
    assert {pr_id: [r["state"] for r in rows if r["name"] == "me"]
            for pr_id, rows in db.list_reviewers(w.conn).items()} == {
        "odoo/odoo#1": ["APPROVED"], "odoo/odoo#3": ["PENDING"],
        "odoo/odoo#4": ["CHANGES_REQUESTED"]}
    assert w.row("odoo/odoo#2") is None


def test_backfilled_rows_fetch_their_discussion_a_batch_a_refresh(w, monkeypatch):
    monkeypatch.setattr(sync, "_PRIME_BATCH", 2)
    for n in (1, 2, 3):
        w.gh.add("odoo/odoo", n, state="MERGED",
                 reviews=[{"author": "me", "state": "APPROVED", "at": T0, "commit": "sha1"}])
        w.gh.comment(f"odoo/odoo#{n}", "alice", "thanks")
    assert w.sync.backfill(since=None, limit=1000).added == 3
    # Gone from GitHub, so it is tried once and never holds a batch slot again.
    insert_pr(w.conn, "odoo/odoo#9", archived_at=T0, fetched_at=db.UNFETCHED)

    assert sum(w.refresh().refreshed for _ in range(3)) == 3
    assert w.refresh().refreshed == 0
    assert sorted(db.list_discussions(w.conn, "pr")) == ["odoo/odoo#1", "odoo/odoo#2", "odoo/odoo#3"]
    assert w.row("odoo/odoo#9")["fetched_at"] != db.UNFETCHED
    assert all(w.row(f"odoo/odoo#{n}")["archived_at"] == T0 for n in (1, 2, 3))


def test_tracking_by_hand_fills_rows_at_once_and_outlives_a_github_failure(w):
    w.gh.add("odoo/odoo", 1, title="[FIX] x")
    refs = [("odoo/odoo", 1), ("odoo/odoo", 9)]
    assert w.sync.track(refs) == ["odoo/odoo#1", "odoo/odoo#9"]
    assert w.sync.fetch_tracked(refs) == 1
    assert db.get_tracked(w.conn, "odoo/odoo#1")["title"] == "[FIX] x"

    db.set_mark(w.conn, "dismiss_tracked", "odoo/odoo#1", None, T0)
    w.gh.fail("nodes")
    assert w.sync.track([("odoo/odoo", 1)]) == []
    with pytest.raises(github.GithubError):
        w.sync.fetch_tracked([("odoo/odoo", 1)])
    assert db.marks(w.conn, "dismiss_tracked") == {}


def test_a_pr_in_two_tabs_shares_one_discussion_swept_once_it_leaves_them_all(w):
    mine = w.gh.add("odoo/odoo", 1, author="me", subscribed=True, requested=["me"])
    other = w.gh.add("odoo/odoo", 2, subscribed=True)
    for pr in (mine, other):
        w.gh.comment(pr.id, "jov-odoo", "Why here?")
    w.refresh()

    def stored():
        return sorted(r["pr_id"] for r in w.conn.execute("SELECT pr_id FROM comment"))

    tracked = {t["id"]: t for t in render.build_tracked_payload(w.conn)[0]}
    [s] = w.mine()
    assert stored() == [ODOO, "odoo/odoo#2"]
    assert [g["entry"]["body"] for g in tracked[ODOO]["discussion"]] == [
        g["entry"]["body"] for g in s["discussion"]] == ["Why here?"]

    # Untracked, #2 is in no tab and #1 stays for Mine.
    for pr in (mine, other):
        pr.subscribed = False
        db.remove_tracked(w.conn, pr.id)
    w.refresh()
    assert stored() == [ODOO]


# --- diffs and the AI pass ---------------------------------------------------

def test_an_oversized_file_is_stubbed_and_the_code_around_it_kept(w):
    # The odoo#277589 shape, one data file over diff_max_lines on its own.
    w.gh.add("odoo/odoo", 277589, requested=["me"], files=["a", "b"],
             patch=_difffile("m/data/res.city.csv", "+row\n" * 12_000) + CODE)
    w.refresh()
    row = db.get_diff(w.conn, "sha1")
    assert CODE in row["patch_text"]
    assert "+row" not in row["patch_text"]
    assert "pull/277589/files#diff-" in row["patch_text"]
    assert row["truncated"] == 1


def test_the_ai_pass_gates_on_the_compacted_diff_and_skips_manual_reviews(w):
    w.cfg.ai.review_max_diff_chars = 1500
    w.gh.add("odoo/odoo", 1, requested=["me"], head_branch="one", head_sha="sha1",
             patch=_difffile("m/i18n/fr.po", "+msgid\n" * 400) + CODE)
    w.gh.add("odoo/odoo", 2, requested=["me"], head_branch="two", head_sha="sha2",
             patch=_difffile("m/models/y.py", "+code\n" * 400))
    w.gh.add("odoo/odoo", 3, requested=["me"], head_branch="three", head_sha="sha3", patch=CODE)
    # A hand-written review counts whatever context it was stored against.
    db.upsert_ai_review(w.conn, "sha3", "other-sha", "Read it myself.", "[]", "major", "t",
                        source="manual")
    w.refresh()
    # #1 is over budget on translations alone, which the prompt never sees, #2 genuinely is.
    assert w.reviewed() == ["sha1"]
    assert "fr.po" in w.reviewer.requests[0].diff


def test_a_failing_review_backs_off_then_gives_up(w):
    w.gh.add("odoo/odoo", 1, requested=["me"], patch=CODE)
    w.reviewer.failures["sha1"] = "cli-missing"
    w.refresh()
    # An absent CLI is the batch's problem, not the PR's, so no attempt is charged.
    assert db.get_ai_attempt(w.conn, "sha1", "", "") is None

    w.reviewer.failures["sha1"] = "timeout"
    asked = []
    for minutes in (0, 0, 61, 180, 62, 60_000):
        w.clock.advance(minutes=minutes)
        w.refresh()
        asked.append(len(w.reviewer.requests))
    # One hour after the first failure, four after the second, never after the third.
    assert asked == [2, 2, 3, 3, 4, 4]
    row = db.get_ai_attempt(w.conn, "sha1", "", "")
    assert (row["attempts"], row["last_error"]) == (3, "timeout")


def test_a_review_that_succeeds_after_a_failure_clears_it(w):
    w.gh.add("odoo/odoo", 1, requested=["me"], patch=CODE)
    w.reviewer.failures["sha1"] = "nonzero"
    w.refresh()
    del w.reviewer.failures["sha1"]
    w.clock.advance(minutes=61)
    w.refresh()
    assert db.get_ai_attempt(w.conn, "sha1", "", "") is None
    assert db.get_ai_review(w.conn, "sha1")["verdict"] == "looks-good"


def test_the_cron_cap_takes_the_cheapest_first_and_the_rest_next_tick(w):
    w.cfg.ai.cron_max_reviews = 1
    w.gh.add("odoo/odoo", 1, requested=["me"], head_branch="one", head_sha="big",
             patch=_difffile("m/models/x.py", "+code\n" * 200))
    w.gh.add("odoo/odoo", 2, requested=["me"], head_branch="two", head_sha="small", patch=CODE)
    w.refresh(cron=True)
    assert w.reviewed() == ["small"]
    w.clock.advance(hours=1)
    w.refresh(cron=True)
    assert w.reviewed() == ["small", "big"]


def test_a_new_companion_rekeys_the_review_and_rides_in_the_prompt(w):
    w.gh.add("odoo/odoo", 1, requested=["me"], head_branch="feat-x", patch=CODE)
    w.refresh()
    assert w.reviewer.requests[0].companion is None

    # The first review was told nothing carried the data across, so it no longer satisfies.
    w.gh.add("odoo/upgrade", 900, head_branch="feat-x", head_sha="usha",
             patch=_difffile("migrations/m/pre-migrate.py", "+util.merge_module\n"))
    w.refresh()
    req = w.reviewer.requests[1]
    assert (req.companion.number, req.companion_repo) == (900, "odoo/upgrade")
    assert "pre-migrate.py" in req.companion.diff
    assert db.get_ai_review(w.conn, "sha1", "", "usha") is not None
    w.refresh()
    assert len(w.reviewer.requests) == 2


def test_each_half_is_reviewed_against_the_others_and_the_sets_companion(w):
    for pr_id, sha in ((ODOO, "sha1"), (ENT, "sha2"), ("odoo/design-themes#3", "sha3")):
        insert_pr(w.conn, pr_id, head_branch="feat-x", head_sha=sha)
        db.upsert_diff(w.conn, sha, _difffile("m/models/x.py", f"+{sha}\n"), False, "t")
    # Stored on one half only, the Companion still belongs to the whole set.
    db.upsert_companion(w.conn, "odoo/design-themes#3", {
        "repo": "odoo/upgrade", "number": 900, "url": "u", "title": "mig",
        "author": "x", "state": "OPEN", "is_draft": 0, "head_branch": "feat-x",
        "head_sha": "usha", "fetched_at": "t",
    })

    reqs, ctx = w.sync._build_review_queue({ODOO, ENT, "odoo/design-themes#3"})
    assert ctx == {"sha1": ("sha2+sha3", "usha"), "sha2": ("sha1+sha3", "usha"),
                   "sha3": ("sha1+sha2", "usha")}
    odoo = next(r for r in reqs if r.head_sha == "sha1")
    assert [(h.number, h.diff.splitlines()[-1]) for h in odoo.context] == [
        (2, "+sha2"), (3, "+sha3")]
