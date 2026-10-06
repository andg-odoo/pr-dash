import pytest

from pr_dash import db, hidden
from pr_dash.config import Config
from pr_dash.sync import Sync
from tests.fakes import T0, FakeClock, FakeGitHub, FakeReviewer, insert_pr

ODOO, ENT = "odoo/odoo#1", "odoo/enterprise#2"


def _difffile(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


CODE = _difffile("m/models/x.py", "+code\n")


class World:
    """A cache, the fake GitHub and reviewer behind it, and one clock for both."""

    def __init__(self, tmp_path):
        self.cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
        self.conn = db.connect(self.cfg.db_path)
        self.clock = FakeClock()
        self.gh = FakeGitHub(self.clock)
        self.reviewer = FakeReviewer()
        self.sync = Sync(self.conn, self.cfg, self.gh, reviewer=self.reviewer, clock=self.clock)

    def refresh(self, *, force=False, cron=False):
        return self.sync.refresh_queue(force=force, cron=cron)

    def row(self, pr_id):
        return db.get_cached_pr(self.conn, pr_id)

    def reviewed(self):
        return [r.head_sha for r in self.reviewer.requests]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


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
    assert "nodes" not in [c[0] for c in w.gh.calls]

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
