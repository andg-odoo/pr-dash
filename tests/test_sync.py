import pytest

from pr_dash import db, github, hidden, render
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
        self.cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
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
    assert db.has_comments(w.conn, ODOO)

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


def test_a_hidden_archived_pr_is_never_pinged(w):
    w.gh.add("odoo/odoo", 1, requested=["me"])
    w.refresh()
    w.clock.advance(hours=1)
    w.gh.review(ODOO, "me")
    w.refresh()
    hidden.save(w.cfg, {ODOO: {"head_sha": "sha1", "hidden_at": "t"}})
    w.clock.advance(hours=1)
    w.gh.comment(ODOO, "alice", "done, ready for r+")
    assert w.refresh().pinged == 0
    assert w.row(ODOO)["ping_at"] is None


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
    assert db.has_comments(w.conn, ENT)

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


def test_a_half_cached_before_comment_rows_existed_is_primed_once(w):
    insert_pr(w.conn, ENT)
    w.gh.add("odoo/enterprise", 2, comments=[{"author": "x", "at": T0, "body": "b"}])
    w.gh.add("odoo/odoo", 1, requested=["me"])
    assert w.refresh().primed == 1
    assert w.refresh().primed == 0


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
