import json

from click.testing import CliRunner

from pr_dash import cli, config, db, github, mergebot

# --- _reviewed_open_siblings -------------------------------------------------

def _row(pr_id, *, author="a", head_branch="feat", state="OPEN", archived_at=None):
    repo, _, number = pr_id.rpartition("#")
    return {"id": pr_id, "repo": repo, "number": int(number), "author": author,
            "head_branch": head_branch, "state": state, "archived_at": archived_at}


def test_reviewed_open_siblings_selects_open_reviewed_half():
    # Active odoo half + its open, non-archived enterprise sibling that fell out
    # of the search -> the sibling is selected for priming.
    active = _row("odoo/odoo#1")
    sibling = _row("odoo/enterprise#2")
    out = cli._reviewed_open_siblings([active, sibling], {"odoo/odoo#1"})
    assert [r["id"] for r in out] == ["odoo/enterprise#2"]


def test_reviewed_open_siblings_includes_archived_skips_closed():
    # The sweep archives a reviewed half on the next refresh, so archived rows
    # are the normal case and must still be primed; closed ones must not.
    active = _row("odoo/odoo#1")
    archived_sib = _row("odoo/enterprise#2", archived_at="2026-07-01T00:00:00+00:00")
    out = cli._reviewed_open_siblings([active, archived_sib], {"odoo/odoo#1"})
    assert [r["id"] for r in out] == ["odoo/enterprise#2"]

    closed_sib = _row("odoo/enterprise#3", state="MERGED")
    assert cli._reviewed_open_siblings(
        [_row("odoo/odoo#1"), closed_sib], {"odoo/odoo#1"}
    ) == []


def test_reviewed_open_siblings_requires_active_partner():
    # Both halves fell out of the active set -> nothing to prime.
    a = _row("odoo/odoo#1")
    b = _row("odoo/enterprise#2")
    assert cli._reviewed_open_siblings([a, b], set()) == []


def test_reviewed_open_siblings_ignores_unpaired_and_active_self():
    # A lone PR (no pair) and the active PR itself are never returned.
    active = _row("odoo/odoo#1")
    lone = _row("odoo/odoo#9", head_branch="other")
    out = cli._reviewed_open_siblings([active, lone], {"odoo/odoo#1"})
    assert out == []


# --- _sibling_needs_refresh --------------------------------------------------

def _node(head="sha1", updated="2026-07-01T00:00:00Z"):
    return {"headRefOid": head, "updatedAt": updated}


def _cached(head="sha1", updated="2026-07-01T00:00:00Z"):
    return {"head_sha": head, "updated_at": updated}


def test_sibling_needs_refresh_unchanged_with_comments_skips():
    assert cli._sibling_needs_refresh(
        _cached(), _node(), True, force=False,
    ) is False


def test_sibling_needs_refresh_primes_when_no_comments():
    # Same head/updated but no comment rows yet -> prime the pre-feature row once.
    assert cli._sibling_needs_refresh(
        _cached(), _node(), False, force=False,
    ) is True


def test_sibling_needs_refresh_on_change_and_force_and_missing():
    assert cli._sibling_needs_refresh(_cached(head="old"), _node(head="new"), True,
                                      force=False) is True
    assert cli._sibling_needs_refresh(_cached(updated="old"), _node(updated="new"),
                                      True, force=False) is True
    assert cli._sibling_needs_refresh(_cached(), _node(), True, force=True) is True
    assert cli._sibling_needs_refresh(None, _node(), True, force=False) is True


# --- shared GraphQL fragment -------------------------------------------------

def test_search_and_sibling_fetch_share_one_fragment():
    # The search query and the by-number fetch must reference the same fragment,
    # so their field selections can never drift.
    assert "fragment PRFields on PullRequest" in github.PR_NODE_FRAGMENT
    assert "...PRFields" in github.SEARCH_QUERY
    assert github.PR_NODE_FRAGMENT in github.SEARCH_QUERY


# --- _prime_reviewed_siblings (integration over a real sqlite cache) ---------

def _insert_pr(conn, pr_id, *, author="a", head_branch="feat", state="OPEN",
               archived_at=None, previously_reviewed=1, head_sha="sha1",
               updated="2026-07-01T00:00:00Z"):
    repo, number = pr_id.split("#")
    conn.execute(
        "INSERT INTO pr (id, repo, number, title, url, author, target_branch, "
        "head_branch, head_sha, created_at, updated_at, review_requested_at, "
        "previously_reviewed, additions, deletions, changed_files, state, "
        "archived_at, fetched_at) VALUES (?,?,?,'','',?,?,?,?,?,?,?,?,0,0,0,?,?,?)",
        (pr_id, repo, int(number), author, "18.0", head_branch, head_sha,
         "2026-07-01T00:00:00Z", updated, "2026-07-01T00:00:00Z",
         previously_reviewed, state, archived_at, "2026-07-01T00:00:00Z"),
    )


def _fake_node(repo, number, *, author="a", head_branch="feat", head="sha2",
               updated="2026-07-10T00:00:00Z"):
    return {
        "repository": {"nameWithOwner": repo},
        "number": number,
        "title": "T",
        "url": f"https://github.com/{repo}/pull/{number}",
        "state": "OPEN",
        "isDraft": False,
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": updated,
        "mergeable": "MERGEABLE",
        "additions": 1, "deletions": 0, "changedFiles": 1,
        "body": "b",
        "baseRefName": "18.0",
        "headRefName": head_branch,
        "headRefOid": head,
        "author": {"login": author},
        "reviewRequests": {"nodes": []},
        "latestReviews": {"nodes": []},
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
        "reviewThreads": {"nodes": [
            {"id": "T1", "isResolved": False, "comments": {"nodes": [
                {"author": {"login": "someone"}, "createdAt": "2026-07-05T00:00:00Z",
                 "body": "why?", "path": "sale/x.py", "databaseId": 55,
                 "url": "https://c/55"},
            ]}},
        ]},
        "commits": {"nodes": []},
        "timelineItems": {"nodes": []},   # deliberately no review by "me"
        "files": {"nodes": [{"path": "sale/x.py"}]},
    }


def test_prime_reviewed_siblings_caches_comments_and_preserves_reviewed(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1")                        # active half
    _insert_pr(conn, "odoo/enterprise#2", previously_reviewed=1)  # reviewed sibling, no comments
    kept = {"odoo/odoo#1"}

    monkeypatch.setattr(github, "fetch_pr_nodes",
                        lambda refs, **kw: [_fake_node("odoo/enterprise", 2)])
    cli._prime_reviewed_siblings(conn, cfg, kept, force=False)

    assert "odoo/enterprise#2" in kept                     # sweep will leave it
    assert db.has_comments(conn, "odoo/enterprise#2") is True
    row = db.get_cached_pr(conn, "odoo/enterprise#2")
    # previously_reviewed stays 1 even though the fetched timeline showed no review
    # by "me" (the last:30 window can drop an old review) - so the sweep can't
    # treat it as a deletable un-reviewed row.
    assert row["previously_reviewed"] == 1


def test_prime_reviewed_siblings_shortcircuits_when_unchanged(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1")
    _insert_pr(conn, "odoo/enterprise#2", head_sha="sha2",
               updated="2026-07-10T00:00:00Z")
    db.replace_comments(conn, "odoo/enterprise#2", [
        {"kind": "issue", "thread_id": None, "comment_id": "1", "author": "x",
         "created_at": "t", "body": "b", "path": None, "state": None, "url": None},
    ])
    kept = {"odoo/odoo#1"}

    # Node matches cached head/updated and comments already exist -> no re-persist.
    monkeypatch.setattr(github, "fetch_pr_nodes",
                        lambda refs, **kw: [_fake_node("odoo/enterprise", 2,
                                                       head="sha2",
                                                       updated="2026-07-10T00:00:00Z")])
    cli._prime_reviewed_siblings(conn, cfg, kept, force=False)

    assert "odoo/enterprise#2" in kept
    # The pre-existing comment row is untouched (not replaced by the fetched one).
    assert [c["comment_id"] for c in db.comments_for(conn, "odoo/enterprise#2")] == ["1"]


def test_prime_reviewed_siblings_keeps_archived_at(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1")
    _insert_pr(conn, "odoo/enterprise#2",
               archived_at="2026-07-02T00:00:00+00:00")
    kept = {"odoo/odoo#1"}

    monkeypatch.setattr(github, "fetch_pr_nodes",
                        lambda refs, **kw: [_fake_node("odoo/enterprise", 2)])
    cli._prime_reviewed_siblings(conn, cfg, kept, force=False)

    assert db.has_comments(conn, "odoo/enterprise#2") is True
    row = db.get_cached_pr(conn, "odoo/enterprise#2")
    # Priming refreshes the discussion but must not resurrect the archived half
    # into the pending queue.
    assert row["archived_at"] == "2026-07-02T00:00:00+00:00"


# --- _reconcile_sibling_states: ping detection & clearing ---------------------

def _activity_node(state="OPEN", *, ping=True):
    reviews = [{"author": {"login": "me"}, "submittedAt": "2026-07-01T00:00:00Z",
                "state": "APPROVED"}]
    comments = ([{"author": {"login": "alice"}, "createdAt": "2026-07-02T00:00:00Z",
                  "body": "done, ready for r+"}] if ping else [])
    return {"state": state, "comments": {"nodes": comments},
            "reviewThreads": {"nodes": []}, "reviews": {"nodes": reviews},
            "timelineItems": {"nodes": []}}


def test_reconcile_sets_ping_on_archived_open(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", archived_at="2026-07-01T00:00:00Z")
    monkeypatch.setattr(github, "fetch_archived_activity",
                        lambda refs, **kw: {"odoo/odoo#1": _activity_node()})

    cli._reconcile_sibling_states(conn, cfg, set())
    row = db.get_cached_pr(conn, "odoo/odoo#1")
    assert row["ping_at"] == "2026-07-02T00:00:00Z"
    assert row["ping_author"] == "alice"
    assert "ready" in row["ping_snippet"]


def test_reconcile_hidden_pr_never_pinged(tmp_path, monkeypatch):
    from pr_dash import db, hidden
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", archived_at="2026-07-01T00:00:00Z", head_sha="sha1")
    hidden.save(cfg, {"odoo/odoo#1": {"head_sha": "sha1", "hidden_at": "t"}})
    monkeypatch.setattr(github, "fetch_archived_activity",
                        lambda refs, **kw: {"odoo/odoo#1": _activity_node()})

    cli._reconcile_sibling_states(conn, cfg, set())
    assert db.get_cached_pr(conn, "odoo/odoo#1")["ping_at"] is None


def test_reconcile_closed_clears_state_and_ping(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", archived_at="2026-07-01T00:00:00Z")
    db.set_ping(conn, "odoo/odoo#1", "2026-07-02T00:00:00Z", "alice", "old ping")
    monkeypatch.setattr(github, "fetch_archived_activity",
                        lambda refs, **kw: {"odoo/odoo#1": _activity_node(state="MERGED")})

    cli._reconcile_sibling_states(conn, cfg, set())
    row = db.get_cached_pr(conn, "odoo/odoo#1")
    assert row["state"] == "MERGED"
    assert row["ping_at"] is None


# --- _reconcile_sibling_states: push detection & stale-row refresh ------------

def _push_activity_node(*, head="sha2", reviewed="sha1", updated="2026-07-01T00:00:00Z"):
    """Archived-activity node where my review sits on `reviewed` and the live
    head is `head`."""
    return {
        "state": "OPEN",
        "headRefOid": head,
        "updatedAt": updated,
        "author": {"login": "a"},
        "comments": {"nodes": []},
        "reviewThreads": {"nodes": []},
        "reviews": {"nodes": [{
            "author": {"login": "me"}, "submittedAt": "2026-07-01T00:00:00Z",
            "state": "APPROVED", "commit": {"oid": reviewed},
        }]},
        "commits": {"nodes": [{"commit": {"oid": head,
                                          "committedDate": "2026-07-05T00:00:00Z"}}]},
        "timelineItems": {"nodes": []},
    }


def test_reconcile_sets_push_on_archived_open(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", archived_at="2026-07-01T00:00:00Z", head_sha="sha1")
    monkeypatch.setattr(github, "fetch_archived_activity",
                        lambda refs, **kw: {"odoo/odoo#1": _push_activity_node()})
    monkeypatch.setattr(github, "fetch_pr_nodes",
                        lambda refs, **kw: [_fake_node("odoo/odoo", 1)])
    monkeypatch.setattr(github, "fetch_patch", lambda repo, number: "diff --git a b")

    cli._reconcile_sibling_states(conn, cfg, set())
    row = db.get_cached_pr(conn, "odoo/odoo#1")
    assert row["push_at"] == "2026-07-05T00:00:00Z"
    assert row["push_sha"] == "sha2"


def test_reconcile_refreshes_stale_archived_row_in_place(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", archived_at="2026-07-01T00:00:00Z", head_sha="sha1")
    monkeypatch.setattr(github, "fetch_archived_activity",
                        lambda refs, **kw: {"odoo/odoo#1": _push_activity_node()})
    monkeypatch.setattr(github, "fetch_pr_nodes",
                        lambda refs, **kw: [_fake_node("odoo/odoo", 1)])
    monkeypatch.setattr(github, "fetch_patch", lambda repo, number: "diff --git a b")

    cli._reconcile_sibling_states(conn, cfg, set())
    row = db.get_cached_pr(conn, "odoo/odoo#1")
    # The moved head pulled in the whole row, not just the push marker...
    assert row["head_sha"] == "sha2"
    assert db.has_comments(conn, "odoo/odoo#1") is True
    # ...along with a diff for the new sha, which no other path would fetch.
    assert db.get_diff(conn, "sha2")["patch_text"] == "diff --git a b"
    # ...and it stays archived: a push by someone else is not my cue to re-review.
    assert row["archived_at"] == "2026-07-01T00:00:00Z"
    assert row["previously_reviewed"] == 1
    # Complexity is keyed by sha too, so without this the row renders as a
    # default-M bucket with a score of 0.
    assert db.get_complexity(conn, "sha2") is not None


def test_reconcile_refreshes_on_bumped_updated_at_alone(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", archived_at="2026-07-01T00:00:00Z", head_sha="sha1",
               updated="2026-07-01T00:00:00Z")
    # Same head, later updatedAt: a review or comment landed. This is the case a
    # sha comparison alone misses - my own review is what bumps it first.
    node = _push_activity_node(head="sha1", reviewed="sha1",
                               updated="2026-07-09T00:00:00Z")
    monkeypatch.setattr(github, "fetch_archived_activity", lambda refs, **kw: {"odoo/odoo#1": node})
    monkeypatch.setattr(github, "fetch_pr_nodes",
                        lambda refs, **kw: [_fake_node("odoo/odoo", 1, head="sha1",
                                                       updated="2026-07-09T00:00:00Z")])
    monkeypatch.setattr(github, "fetch_patch", lambda repo, number: "d")

    cli._reconcile_sibling_states(conn, cfg, set())
    row = db.get_cached_pr(conn, "odoo/odoo#1")
    assert row["updated_at"] == "2026-07-09T00:00:00Z"
    assert row["push_at"] is None  # head never moved


def test_reconcile_leaves_fresh_archived_row_alone(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", archived_at="2026-07-01T00:00:00Z", head_sha="sha1")
    node = _push_activity_node(head="sha1", reviewed="sha1")
    monkeypatch.setattr(github, "fetch_archived_activity", lambda refs, **kw: {"odoo/odoo#1": node})

    def _boom(*a, **kw):
        raise AssertionError("unchanged row must not cost a node fetch")

    monkeypatch.setattr(github, "fetch_pr_nodes", _boom)
    cli._reconcile_sibling_states(conn, cfg, set())
    assert db.get_cached_pr(conn, "odoo/odoo#1")["head_sha"] == "sha1"


def test_reconcile_clears_push_once_i_review_the_new_head(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", archived_at="2026-07-01T00:00:00Z", head_sha="sha2")
    db.set_push(conn, "odoo/odoo#1", "2026-07-05T00:00:00Z", "sha2")
    node = _push_activity_node(head="sha2", reviewed="sha2")
    monkeypatch.setattr(github, "fetch_archived_activity", lambda refs, **kw: {"odoo/odoo#1": node})

    cli._reconcile_sibling_states(conn, cfg, set())
    row = db.get_cached_pr(conn, "odoo/odoo#1")
    assert row["push_at"] is None
    assert row["push_sha"] is None


# --- diff caching & the AI review gate ---------------------------------------

def _difffile(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


def test_store_patch_keeps_the_code_around_an_oversized_file(tmp_path, monkeypatch):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    code = _difffile("m/models/x.py", "+code\n")
    # The odoo#277589 shape: a data file blowing diff_max_lines on its own, which
    # used to leave the whole PR with no cached diff and no AI first pass.
    monkeypatch.setattr(github, "fetch_patch",
                        lambda repo, number: _difffile("m/data/res.city.csv",
                                                       "+row\n" * 12_000) + code)
    cli._store_patch(conn, cfg, {"head_sha": "sha1", "repo": "odoo/odoo",
                                 "number": 277589, "changed_files": 2}, force=False)

    row = db.get_diff(conn, "sha1")
    assert code in row["patch_text"]
    assert "+row" not in row["patch_text"]
    assert "pull/277589/files#diff-" in row["patch_text"]
    assert row["truncated"] == 1  # partial, so the dashboard still says so


def test_review_queue_gates_on_the_compacted_diff(tmp_path):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", head_branch="one", head_sha="sha1")
    _insert_pr(conn, "odoo/odoo#2", head_branch="two", head_sha="sha2")
    db.upsert_diff(conn, "sha1", _difffile("m/i18n/fr.po", "+msgid\n" * 400)
                   + _difffile("m/models/x.py", "+code\n"), False, "t")
    db.upsert_diff(conn, "sha2", _difffile("m/models/y.py", "+code\n" * 400), False, "t")

    reqs, _ = cli._build_review_queue(conn, {"odoo/odoo#1", "odoo/odoo#2"}, 1500)

    # #1 blows the gate on translations alone, which the prompt never sees; #2 is
    # genuinely over budget. The request still carries the raw diff, so the
    # prompt can tell the model what was dropped from it.
    assert [r.head_sha for r in reqs] == ["sha1"]
    assert "fr.po" in reqs[0].diff

    # A hand-written review takes the PR out of the queue whatever pair context
    # it was stored against - otherwise the next refresh reads it as a miss and
    # overwrites a human's findings with a model pass.
    db.upsert_ai_review(conn, "sha1", "other-sha", "Read it myself.", "[]",
                        "major", "t", source="manual")
    reqs, _ = cli._build_review_queue(conn, {"odoo/odoo#1", "odoo/odoo#2"}, 1500)
    assert reqs == []


def test_a_failed_review_records_an_attempt_and_stops_being_requeued(tmp_path):
    from pr_dash import ai, db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", head_sha="sha1")
    db.upsert_diff(conn, "sha1", _difffile("m/models/x.py", "+code\n"), False, "t")

    reqs, ctx = cli._build_review_queue(conn, {"odoo/odoo#1"}, 50_000)
    assert [r.head_sha for r in reqs] == ["sha1"]

    # An absent CLI is the batch's problem, not the PR's: no attempt is charged.
    cli._store_reviews(conn, [ai.ReviewOutcome("sha1", None, "cli-missing")], ctx)
    assert db.get_ai_attempt(conn, "sha1", "", "") is None

    cli._store_reviews(conn, [ai.ReviewOutcome("sha1", None, "timeout")], ctx)
    row = db.get_ai_attempt(conn, "sha1", "", "")
    assert (row["attempts"], row["last_error"]) == (1, "timeout")

    # Inside the backoff window the same call is not spent again...
    assert cli._build_review_queue(conn, {"odoo/odoo#1"}, 50_000)[0] == []
    conn.execute("UPDATE ai_attempt SET last_attempt_at = '2020-01-01T00:00:00+00:00'")
    assert [r.head_sha for r in cli._build_review_queue(conn, {"odoo/odoo#1"}, 50_000)[0]]

    # ...and once the budget is spent, not again at all.
    conn.execute("UPDATE ai_attempt SET attempts = 3")
    assert cli._build_review_queue(conn, {"odoo/odoo#1"}, 50_000)[0] == []


def test_a_successful_review_clears_the_attempt(tmp_path):
    from pr_dash import ai, db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", head_sha="sha1")
    db.upsert_diff(conn, "sha1", _difffile("m/models/x.py", "+code\n"), False, "t")
    _, ctx = cli._build_review_queue(conn, {"odoo/odoo#1"}, 50_000)
    cli._store_reviews(conn, [ai.ReviewOutcome("sha1", None, "nonzero")], ctx)

    result = ai.ReviewResult("sha1", "adds a line", [], "looks-good")
    cli._store_reviews(conn, [ai.ReviewOutcome("sha1", result)], ctx)

    assert db.get_ai_attempt(conn, "sha1", "", "") is None
    assert db.get_ai_review(conn, "sha1")["verdict"] == "looks-good"


def test_the_cron_cap_keeps_the_cheapest_candidates(tmp_path):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", head_branch="one", head_sha="big")
    _insert_pr(conn, "odoo/odoo#2", head_branch="two", head_sha="small")
    db.upsert_diff(conn, "big", _difffile("m/models/x.py", "+code\n" * 200), False, "t")
    db.upsert_diff(conn, "small", _difffile("m/models/y.py", "+code\n"), False, "t")

    reqs, ctx = cli._build_review_queue(conn, {"odoo/odoo#1", "odoo/odoo#2"}, 50_000,
                                        limit=1)

    # The big one is not skipped, just deferred to the next tick or a manual run.
    assert [r.head_sha for r in reqs] == ["small"]
    assert set(ctx) == {"small"}


def test_cron_renders_without_consuming_the_since_last_look_baseline(tmp_path):
    from click.testing import CliRunner

    from pr_dash import db

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[user]\ngithub_login = "me"\n\n[repos]\n"odoo/odoo" = "/tmp/odoo"\n'
        f'\n[paths]\ncache_dir = "{tmp_path}"\n'
    )
    conn = db.connect(tmp_path / "pr_dash.db")
    _insert_pr(conn, "odoo/odoo#1", head_sha="sha1")
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
    monkeypatch.setattr(cli, "_run_refresh", lambda *a, **kw: runs.append(kw["cron"]))
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


# --- _refresh_companions -----------------------------------------------------

def _upgrade_pr(branch, *, number=900, sha="usha"):
    return {"number": number, "title": "[IMP] base: merge modules", "state": "open",
            "draft": False, "url": f"https://github.com/odoo/upgrade/pull/{number}",
            "head_branch": branch, "head_sha": sha, "author": "someone-else"}


def _companion_cfg(tmp_path):
    from pr_dash.config import Config

    return Config(github_login="me", repos={}, cache_dir=tmp_path)


def test_refresh_companions_matches_on_branch_only(tmp_path, monkeypatch):
    from pr_dash import db

    cfg = _companion_cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", author="jdoe", head_branch="feat-x")
    _insert_pr(conn, "odoo/enterprise#2", author="jdoe", head_branch="feat-x")
    _insert_pr(conn, "odoo/odoo#3", author="jdoe", head_branch="other")

    monkeypatch.setattr(github, "search_open_prs_by_head_branch",
                        lambda repo, branches: {"feat-x": _upgrade_pr("feat-x")})
    monkeypatch.setattr(cli, "_store_patch", lambda *a, **kw: None)

    assert cli._refresh_companions(conn, cfg, {"odoo/odoo#1"}) == "odoo/upgrade"

    # Both halves of the bundle carry the migration, and its author ("someone-else")
    # is deliberately not part of the match - migrations are often written by
    # someone other than the author of the half they migrate.
    for pr_id in ("odoo/odoo#1", "odoo/enterprise#2"):
        row = db.get_companion(conn, pr_id)
        assert (row["repo"], row["number"], row["state"]) == ("odoo/upgrade", 900, "OPEN")
    # A branch with no upgrade PR is genuinely without one, not unknown.
    assert db.get_companion(conn, "odoo/odoo#3") is None
    # And a companion is never a queue row of its own.
    assert {r["id"] for r in db.list_prs(conn)} == {
        "odoo/odoo#1", "odoo/enterprise#2", "odoo/odoo#3",
    }


def test_refresh_companions_survives_an_unreachable_repo(tmp_path, monkeypatch):
    from pr_dash import db

    cfg = _companion_cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", head_branch="feat-x")

    def boom(repo, branches):
        raise github.GithubError("HTTP 404: Not Found")

    monkeypatch.setattr(github, "search_open_prs_by_head_branch", boom)

    # No access to the private repo (or no network) must leave the refresh intact
    # and, crucially, report that nothing was searched - the prompt may only call
    # a migration absent when one was actually looked for.
    assert cli._refresh_companions(conn, cfg, {"odoo/odoo#1"}) == ""
    assert db.get_companion(conn, "odoo/odoo#1") is None

    cfg.companion.enabled = False
    monkeypatch.setattr(github, "search_open_prs_by_head_branch",
                        lambda repo, branches: {"feat-x": _upgrade_pr("feat-x")})
    assert cli._refresh_companions(conn, cfg, {"odoo/odoo#1"}) == ""


def test_refresh_companions_keeps_an_archived_prs_stored_migration(tmp_path, monkeypatch):
    from pr_dash import db

    cfg = _companion_cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", head_branch="feat-x")
    _insert_pr(conn, "odoo/enterprise#9", head_branch="old-feat",
               archived_at="2026-07-02T00:00:00Z")
    db.upsert_companion(conn, "odoo/enterprise#9", {
        "repo": "odoo/upgrade", "number": 800, "url": "u", "title": "mig", "author": "x",
        "state": "OPEN", "is_draft": 0, "head_branch": "old-feat", "head_sha": "osha",
        "fetched_at": "t",
    })

    asked = []
    monkeypatch.setattr(github, "search_open_prs_by_head_branch",
                        lambda repo, branches: asked.append(branches) or {})
    monkeypatch.setattr(github, "fetch_pr_states", lambda repo, numbers: {800: "MERGED"})
    monkeypatch.setattr(cli, "_store_patch", lambda *a, **kw: None)

    cli._refresh_companions(conn, cfg, {"odoo/odoo#1"})

    # Only the active queue is searched, so an archived PR's branch is never asked about.
    assert asked == [["feat-x"]]
    # Its stored migration must survive that, and still be re-checked from the stored row.
    row = db.get_companion(conn, "odoo/enterprise#9")
    assert (row["number"], row["state"]) == (800, "MERGED")


def test_review_queue_carries_the_companion_and_rekeys_the_cache(tmp_path):
    from pr_dash import db

    cfg = _companion_cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr(conn, "odoo/odoo#1", head_branch="feat-x", head_sha="sha1")
    db.upsert_diff(conn, "sha1", _difffile("m/models/x.py", "+code\n"), False, "t")
    db.upsert_diff(conn, "usha",
                   _difffile("migrations/m/pre-migrate.py", "+util.merge_module\n"),
                   False, "t")
    db.upsert_companion(conn, "odoo/odoo#1", {
        "repo": "odoo/upgrade", "number": 900, "url": "u", "title": "mig",
        "author": "x", "state": "OPEN", "is_draft": 0, "head_branch": "feat-x",
        "head_sha": "usha", "fetched_at": "t",
    })

    reqs, ctx = cli._build_review_queue(conn, {"odoo/odoo#1"}, 50_000,
                                        companion_repo="odoo/upgrade")
    assert ctx == {"sha1": ("", "usha")}
    assert (reqs[0].companion.number, reqs[0].companion_repo) == (900, "odoo/upgrade")
    assert "pre-migrate.py" in reqs[0].companion.diff

    # A review computed before the migration was known was told nothing carried
    # the data across, so it must not satisfy the PR that now has one.
    db.upsert_ai_review(conn, "sha1", "", "s", "[]", "looks-good", "t")
    reqs, _ = cli._build_review_queue(conn, {"odoo/odoo#1"}, 50_000,
                                      companion_repo="odoo/upgrade")
    assert [r.head_sha for r in reqs] == ["sha1"]

    db.upsert_ai_review(conn, "sha1", "", "s", "[]", "looks-good", "t",
                        companion_head_sha="usha")
    reqs, _ = cli._build_review_queue(conn, {"odoo/odoo#1"}, 50_000,
                                      companion_repo="odoo/upgrade")
    assert reqs == []


def test_review_queue_reviews_each_half_against_the_others_and_the_sets_companion(tmp_path):
    conn = db.connect(_companion_cfg(tmp_path).db_path)
    for pr_id, sha in (("odoo/odoo#1", "sha1"), ("odoo/enterprise#2", "sha2"),
                       ("odoo/design-themes#3", "sha3")):
        _insert_pr(conn, pr_id, head_branch="feat-x", head_sha=sha)
        db.upsert_diff(conn, sha, _difffile("m/models/x.py", f"+{sha}\n"), False, "t")
    # Stored on one half only, the Companion still belongs to the whole set.
    db.upsert_companion(conn, "odoo/design-themes#3", {
        "repo": "odoo/upgrade", "number": 900, "url": "u", "title": "mig",
        "author": "x", "state": "OPEN", "is_draft": 0, "head_branch": "feat-x",
        "head_sha": "usha", "fetched_at": "t",
    })

    reqs, ctx = cli._build_review_queue(
        conn, {"odoo/odoo#1", "odoo/enterprise#2", "odoo/design-themes#3"}, 50_000)
    assert ctx == {"sha1": ("sha2+sha3", "usha"), "sha2": ("sha1+sha3", "usha"),
                   "sha3": ("sha1+sha2", "usha")}
    odoo = next(r for r in reqs if r.head_sha == "sha1")
    assert [(h.number, h.diff.splitlines()[-1]) for h in odoo.context] == [(2, "+sha2"), (3, "+sha3")]


# --- _run_mine_refresh -------------------------------------------------------

def _mine_node(repo, number, state):
    return {"url": f"https://github.com/{repo}/pull/{number}", "title": f"PR {number}",
            "state": state, "headRefName": "master-x-6396725-andg",
            "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-05T00:00:00Z"}


def test_mine_refresh_keeps_resolved_members_and_stops_reading_their_final_page(
        tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[user]\ngithub_login = "me"\n\n[repos]\n"odoo/odoo" = "/tmp/odoo"\n'
        f'\n[paths]\ncache_dir = "{tmp_path}"\n',
    )
    cfg = config.load(config_path)
    gh = {"open": [("odoo/odoo", 10), ("odoo/upgrade", 20)], "state": "OPEN"}
    pages = {"odoo/odoo": "blocked", "odoo/upgrade": "unknown"}
    reads = []
    monkeypatch.setattr(github, "search_authored_open", lambda login: gh["open"])
    monkeypatch.setattr(github, "fetch_nodes", lambda refs, fragment, **kw: {
        f"{r}#{n}": _mine_node(r, n, gh["state"]) for r, n in refs})
    monkeypatch.setattr(mergebot, "fetch", lambda repo, n: reads.append(repo)
                        or mergebot.MergebotState(pages[repo]))

    conn = db.connect(cfg.db_path)
    cli._run_mine_refresh(conn, cfg, force=False)
    # Fresh within mine_staleness_minutes, so the second run reads nothing.
    cli._run_mine_refresh(conn, cfg, force=False)
    assert sorted(reads) == ["odoo/odoo", "odoo/upgrade"]

    # Both left the open search and closed, the Mergebot telling Merged from closed.
    gh["open"] = []
    gh["state"], pages["odoo/odoo"], pages["odoo/upgrade"] = "CLOSED", "merged", "closed"
    cli._run_mine_refresh(conn, cfg, force=True)
    cli._run_mine_refresh(conn, cfg, force=True)
    assert sorted(reads) == ["odoo/odoo"] * 2 + ["odoo/upgrade"] * 2

    out = json.loads(CliRunner().invoke(
        cli.cli, ["query", "mine", "--config", str(config_path)]).output)
    assert [(s["key"], s["band"], [(m["num"], m["state"]) for m in s["members"]])
            for s in out["branch_sets"]] == [
        ("master-x-6396725-andg", "done", [(10, "MERGED"), (20, "CLOSED")])]


def _xref(repo, number, author):
    return {"source": {"number": number, "author": {"login": author},
                       "repository": {"nameWithOwner": repo}}}


def test_mine_refresh_hangs_confirmed_forward_ports_under_their_source(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[user]\ngithub_login = "andg-odoo"\n\n[repos]\n"odoo/odoo" = "/tmp/odoo"\n'
        f'\n[paths]\ncache_dir = "{tmp_path}"\n',
    )
    cfg = config.load(config_path)
    body = "The new company now gets the contact's responsibility.\r\n\r\ntask-6470810\n\n"
    nodes = {
        "odoo/odoo#290657": {**_mine_node("odoo/odoo", 290657, "CLOSED"),
                             "headRefName": "saas-19.1-l10n_ar-company-arca-6470810-andg",
                             "crossReferences": {"nodes": [
                                 _xref("odoo/odoo", 291857, "fw-bot"),
                                 _xref("odoo/enterprise", 133776, "andg-odoo"),
                                 _xref("odoo/odoo", 291981, "fw-bot"),
                                 _xref("odoo/odoo", 291000, "fw-bot"),
                             ]}},
        "odoo/odoo#291857": {**_mine_node("odoo/odoo", 291857, "CLOSED"), "baseRefName": "20.0",
                             "body": body + "Forward-Port-Of: odoo/odoo#290657"},
        "odoo/odoo#291981": {**_mine_node("odoo/odoo", 291981, "OPEN"), "baseRefName": "master",
                             "body": body + "Forward-Port-Of: odoo/odoo#291857\n"
                                            "Forward-Port-Of: odoo/odoo#290657"},
        "odoo/odoo#291000": {**_mine_node("odoo/odoo", 291000, "OPEN"),
                             "body": "Forward-Port-Of: odoo/odoo#280000"},
    }
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
    nodes = {
        "odoo/odoo#100": {**_mine_node("odoo/odoo", 100, "OPEN"), "headRefName": "master-live"},
        "odoo/odoo#10": {**_mine_node("odoo/odoo", 10, "CLOSED"), "headRefName": "19.0-old"},
        "odoo/odoo#290657": {**_mine_node("odoo/odoo", 290657, "CLOSED"),
                             "headRefName": "saas-19.1-arca",
                             "crossReferences": {"nodes": [_xref("odoo/odoo", 291981, "fw-bot")]}},
        "odoo/odoo#291981": {**_mine_node("odoo/odoo", 291981, "OPEN"), "baseRefName": "master",
                             "body": "Forward-Port-Of: odoo/odoo#290657"},
    }
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

    source = {**_mine_node("odoo/odoo", 10, "CLOSED"),
              "crossReferences": {"nodes": [_xref("odoo/odoo", 11, "fw-bot")]}}
    fw = {**_mine_node("odoo/odoo", 11, "CLOSED"), "body": "Forward-Port-Of: odoo/odoo#10"}

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
