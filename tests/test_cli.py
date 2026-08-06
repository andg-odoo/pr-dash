from pr_dash import cli, github


# --- _reviewed_open_siblings -------------------------------------------------

def _row(pr_id, *, author="a", head_branch="feat", state="OPEN", archived_at=None):
    return {"id": pr_id, "author": author, "head_branch": head_branch,
            "state": state, "archived_at": archived_at}


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

    monkeypatch.setattr(github, "list_open_prs_by_head_branch",
                        lambda repo: {"feat-x": _upgrade_pr("feat-x")})
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

    def boom(repo):
        raise github.GithubError("HTTP 404: Not Found")

    monkeypatch.setattr(github, "list_open_prs_by_head_branch", boom)

    # No access to the private repo (or no network) must leave the refresh intact
    # and, crucially, report that nothing was searched - the prompt may only call
    # a migration absent when one was actually looked for.
    assert cli._refresh_companions(conn, cfg, {"odoo/odoo#1"}) == ""
    assert db.get_companion(conn, "odoo/odoo#1") is None

    cfg.companion.enabled = False
    monkeypatch.setattr(github, "list_open_prs_by_head_branch",
                        lambda repo: {"feat-x": _upgrade_pr("feat-x")})
    assert cli._refresh_companions(conn, cfg, {"odoo/odoo#1"}) == ""


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
