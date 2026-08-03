import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pr_dash import cli, db, derive, github, render
from pr_dash import query as prquery


def _conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "t.db")


# --- db.add_tracked ----------------------------------------------------------

def _add(conn, pr_id, source="notif", when="2026-08-01T00:00:00+00:00"):
    repo, number = pr_id.split("#")
    return db.add_tracked(conn, pr_id, repo, int(number),
                          f"https://github.com/{repo}/pull/{number}", source, when)


def test_add_tracked_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    assert _add(conn, "odoo/odoo#1") is True
    assert _add(conn, "odoo/odoo#1") is False
    assert len(db.list_tracked(conn)) == 1


def test_add_tracked_keeps_original_provenance(tmp_path):
    # A manual add re-seeded from notifications must not be relabelled: the
    # dashboard shows "tracked manually" vs "via subscription" from this field.
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1", source="manual")
    _add(conn, "odoo/odoo#1", source="notif")
    assert db.get_tracked(conn, "odoo/odoo#1")["source"] == "manual"


def test_notification_seed_does_not_revive_dismissed(tmp_path):
    # The regression this guards: a merged PR keeps its reason=manual thread, so
    # an un-dismissing seed would resurrect every row right after it is cleared.
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    db.dismiss_tracked(conn, "odoo/odoo#1", "2026-08-02T00:00:00+00:00")
    assert db.list_tracked(conn) == []

    _add(conn, "odoo/odoo#1", source="notif")
    assert db.list_tracked(conn) == []
    assert len(db.list_tracked(conn, include_dismissed=True)) == 1


def test_explicit_track_revives_dismissed(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    db.dismiss_tracked(conn, "odoo/odoo#1", "2026-08-02T00:00:00+00:00")

    _add(conn, "odoo/odoo#1", source="manual")
    assert [r["id"] for r in db.list_tracked(conn)] == ["odoo/odoo#1"]


def test_remove_tracked_cascades(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    db.replace_tracked_comments(conn, "odoo/odoo#1", [
        {"comment_id": "c1", "kind": "issue", "author": "a", "created_at": "t",
         "body": "hi", "path": None, "state": None, "url": "u"},
    ])
    db.upsert_tracked_seen(conn, "odoo/odoo#1", "OPEN", "sha", 1, "t")

    assert db.remove_tracked(conn, "odoo/odoo#1") is True
    assert db.list_tracked_comments(conn) == {}
    assert db.list_tracked_seen(conn) == {}
    assert db.remove_tracked(conn, "odoo/odoo#1") is False


def test_update_tracked_state_preserves_tracking_metadata(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1", source="manual", when="2026-08-01T00:00:00+00:00")
    db.update_tracked_state(conn, "odoo/odoo#1", {
        "title": "[FIX] x", "state": "MERGED", "fetched_at": "2026-08-03T00:00:00+00:00",
    })
    row = db.get_tracked(conn, "odoo/odoo#1")
    assert row["title"] == "[FIX] x"
    assert row["state"] == "MERGED"
    assert row["source"] == "manual"
    assert row["added_at"] == "2026-08-01T00:00:00+00:00"


def test_migration_adds_tracked_tables(tmp_path):
    # Simulate a pre-v15 database: schema at the old version, no tracked tables.
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA_SQL.replace(db.TRACKED_SCHEMA_SQL, ""))
    raw.execute("PRAGMA user_version = 14")
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert _add(conn, "odoo/odoo#1") is True


# --- derive.tracked_since_last_look ------------------------------------------

def test_since_last_look_first_run_is_quiet_but_flags_resolved():
    assert derive.tracked_since_last_look(None, "OPEN", "s", 0, first_run=True) == []
    # Landing in the list already merged is the whole point of tracking it.
    assert derive.tracked_since_last_look(
        None, "MERGED", "s", 0, first_run=True) == ["resolved"]


def test_since_last_look_flags_new_row_after_first_run():
    assert derive.tracked_since_last_look(
        None, "OPEN", "s", 0, first_run=False) == ["new"]


def test_since_last_look_flags_merge_and_replies():
    prev = ("OPEN", "sha1", 2)
    assert derive.tracked_since_last_look(prev, "MERGED", "sha1", 2, first_run=False) \
        == ["resolved"]
    assert derive.tracked_since_last_look(prev, "OPEN", "sha2", 5, first_run=False) \
        == ["pushed", "reply"]
    # Comment count going down (a deletion) is not "new discussion".
    assert derive.tracked_since_last_look(prev, "OPEN", "sha1", 1, first_run=False) == []


def test_since_last_look_keeps_resolved_badge_until_dismissed():
    # Already flagged merged last render: the badge must persist so the row
    # stays visibly done rather than quietly going plain again.
    prev = ("MERGED", "sha1", 2)
    assert derive.tracked_since_last_look(
        prev, "MERGED", "sha1", 2, first_run=False) == ["resolved"]


def test_since_last_look_flags_reopen():
    prev = ("CLOSED", "sha1", 2)
    assert derive.tracked_since_last_look(
        prev, "OPEN", "sha1", 2, first_run=False) == ["reopened"]


# --- derive.tracked_row_from_node --------------------------------------------

def _node(**over):
    node = {
        "title": "[FIX] sale: x",
        "author": {"login": "someone"},
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "master",
        "headRefOid": "abc123",
        "body": "desc",
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": "2026-08-01T00:00:00Z",
        "closedAt": None,
        "mergedAt": None,
        "comments": {
            "totalCount": 4,
            "nodes": [
                {"author": {"login": "robodoo"}, "createdAt": "2026-07-02T00:00:00Z",
                 "body": "ci", "url": "u1"},
                {"author": {"login": "human"}, "createdAt": "2026-07-03T00:00:00Z",
                 "body": "lgtm", "url": "u2"},
            ],
        },
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {
            "state": "SUCCESS", "contexts": {"nodes": []}}}}]},
    }
    node.update(over)
    return node


def test_tracked_row_merges_reviews_and_threads_in_time_order():
    # The bug this pins: an Odoo PR keeps almost nothing in `comments`. Fetching
    # only those made a PR with an approval and 20 inline threads look silent.
    node = _node(
        comments={"totalCount": 1, "nodes": [
            {"author": {"login": "someone"}, "createdAt": "2026-07-01T00:00:00Z",
             "body": "opening note", "url": "u-issue"},
        ]},
        reviews={"totalCount": 3, "nodes": [
            {"id": "REV_appr", "author": {"login": "rev"}, "state": "APPROVED",
             "body": "", "submittedAt": "2026-08-01T00:00:00Z", "url": "u-appr"},
            # Bodiless COMMENTED review: the envelope around inline comments.
            {"id": "REV_env", "author": {"login": "rev"}, "state": "COMMENTED",
             "body": "", "submittedAt": "2026-07-05T00:00:00Z", "url": "u-env"},
        ]},
        reviewThreads={"totalCount": 2, "nodes": [
            {"id": "THR_1", "isResolved": False,
             "path": "addons/sale/models/sale_order.py", "comments": {"nodes": [
                 {"author": {"login": "rev"}, "createdAt": "2026-07-05T00:00:00Z",
                  "body": "why?", "url": "u-t1",
                  "pullRequestReview": {"id": "REV_env"}},
             ]}},
            {"id": "THR_2", "isResolved": True, "path": "addons/sale/x.py",
             "comments": {"nodes": [
                {"author": {"login": "auth"}, "createdAt": "2026-07-06T00:00:00Z",
                 "body": "fixed", "url": "u-t2",
                 "pullRequestReview": {"id": "REV_other"}},
            ]}},
        ]},
    )
    row, comments = derive.tracked_row_from_node(node, "t")

    assert [(c["kind"], c["author"]) for c in comments] == [
        ("issue", "someone"), ("thread", "rev"), ("thread", "auth"), ("review", "rev"),
    ]
    # Threads carry the id of the review that opened them, so the dashboard can
    # nest them under it instead of interleaving everything by timestamp.
    assert comments[1]["parent_id"] == "REV_env"
    assert comments[1]["thread_id"] == "THR_1"
    assert comments[3]["thread_id"] == "REV_appr"
    # The bodiless COMMENTED envelope is dropped; the bodiless APPROVED is kept,
    # because the verdict is the entire message.
    approval = comments[-1]
    assert approval["state"] == "APPROVED" and not approval["body"]
    assert comments[1]["path"] == "addons/sale/models/sale_order.py"
    assert comments[1]["state"] == "UNRESOLVED"
    assert row["unresolved_threads"] == 1
    # comment_count stays the GitHub-visible number; activity_count drives
    # deltas; the parts are kept so the row can read "1 comment · 3 reviews".
    assert row["comment_count"] == 1
    assert row["review_count"] == 3
    assert row["thread_count"] == 2
    assert row["activity_count"] == 1 + 3 + 2


def test_activity_count_moves_when_only_a_review_lands():
    before = derive.tracked_row_from_node(_node(
        comments={"totalCount": 2, "nodes": []},
        reviews={"totalCount": 1, "nodes": []},
        reviewThreads={"totalCount": 0, "nodes": []}), "t")[0]
    after = derive.tracked_row_from_node(_node(
        comments={"totalCount": 2, "nodes": []},
        reviews={"totalCount": 2, "nodes": []},
        reviewThreads={"totalCount": 0, "nodes": []}), "t")[0]

    assert derive.tracked_since_last_look(
        ("OPEN", "abc123", before["activity_count"]),
        "OPEN", "abc123", after["activity_count"], first_run=False,
    ) == ["reply"]


def test_tracked_row_from_node_flattens_state_and_comments():
    row, comments = derive.tracked_row_from_node(_node(), "2026-08-03T00:00:00+00:00")
    assert row["title"] == "[FIX] sale: x"
    assert row["author"] == "someone"
    assert row["state"] == "OPEN"
    assert row["target_branch"] == "master"
    assert row["head_sha"] == "abc123"
    assert row["ci_state"] == "SUCCESS"
    # totalCount, not len(nodes): only the last page of comments is fetched.
    assert row["comment_count"] == 4
    assert [c["author"] for c in comments] == ["robodoo", "human"]


def test_tracked_row_from_node_tolerates_missing_ci_and_author():
    row, comments = derive.tracked_row_from_node(
        _node(author=None, commits={"nodes": []}, comments={}), "t",
    )
    assert row["author"] == ""
    assert row["ci_state"] is None
    assert row["comment_count"] == 0
    assert comments == []


# --- render.build_tracked_payload --------------------------------------------

def test_build_tracked_payload_sorts_active_first_and_drops_bots(tmp_path):
    conn = _conn(tmp_path)
    for pr_id, state, updated in [
        ("odoo/odoo#1", "OPEN", "2026-08-01T00:00:00Z"),
        ("odoo/odoo#2", "MERGED", "2026-07-01T00:00:00Z"),
        ("odoo/odoo#3", "OPEN", "2026-08-02T00:00:00Z"),
    ]:
        _add(conn, pr_id)
        db.update_tracked_state(conn, pr_id, {
            "state": state, "updated_at": updated, "created_at": updated,
            "head_sha": "s", "fetched_at": "t",
        })
    db.replace_tracked_comments(conn, "odoo/odoo#1", [
        {"comment_id": "c1", "kind": "issue", "author": "robodoo",
         "created_at": "t", "body": "ci", "path": None, "state": None, "url": None},
        {"comment_id": "c2", "kind": "issue", "author": "human",
         "created_at": "t", "body": "hi", "path": None, "state": None, "url": None},
    ])

    items, seen_updates = render.build_tracked_payload(conn)

    # Open rows newest-active first; resolved ones sink to the bottom so a long
    # tail of months-old closures can't bury the PRs still in flight.
    assert [i["id"] for i in items] == ["odoo/odoo#3", "odoo/odoo#1", "odoo/odoo#2"]
    assert len(seen_updates) == 3
    by_id = {i["id"]: i for i in items}
    assert [c["author"] for c in by_id["odoo/odoo#1"]["comments"]] == ["human"]
    assert by_id["odoo/odoo#1"]["repo_short"] == "odoo"


def test_build_tracked_payload_excludes_dismissed(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    _add(conn, "odoo/odoo#2")
    db.dismiss_tracked(conn, "odoo/odoo#2", "2026-08-02T00:00:00+00:00")

    items, _ = render.build_tracked_payload(conn)
    assert [i["id"] for i in items] == ["odoo/odoo#1"]


def test_build_tracked_payload_handles_never_fetched_row(tmp_path):
    # `pr-dash track` inserts before any fetch; a render in between must not crash.
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    items, _ = render.build_tracked_payload(conn)
    assert items[0]["age_days"] == 0
    assert items[0]["state"] == "OPEN"


def test_tracked_seen_baseline_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    db.update_tracked_state(conn, "odoo/odoo#1", {"state": "OPEN", "head_sha": "s1"})

    items, updates = render.build_tracked_payload(conn)
    assert items[0]["since_last_look"] == []          # first run, no baseline
    render.commit_tracked_seen_baseline(conn, updates, "t")

    db.update_tracked_state(conn, "odoo/odoo#1", {"state": "MERGED", "head_sha": "s1"})
    items, _ = render.build_tracked_payload(conn)
    assert items[0]["since_last_look"] == ["resolved"]


# --- cli._parse_pr_ref -------------------------------------------------------

@pytest.mark.parametrize("ref", [
    "odoo/odoo#264068",
    "https://github.com/odoo/odoo/pull/264068",
    "https://github.com/odoo/odoo/pull/264068/",
    "  odoo/odoo#264068  ",
])
def test_parse_pr_ref_accepts_both_forms(ref):
    assert cli._parse_pr_ref(ref) == ("odoo/odoo", 264068)


@pytest.mark.parametrize("ref", ["264068", "odoo#264068", "odoo/odoo", "", "odoo/odoo#x"])
def test_parse_pr_ref_rejects_junk(ref):
    with pytest.raises(ValueError):
        cli._parse_pr_ref(ref)


# --- github.list_manual_subscriptions ----------------------------------------

def test_list_manual_subscriptions_parses_and_dedupes(monkeypatch):
    # gh --jq streams one JSON object per line; the same PR can appear twice
    # when several notifications for it are still retained.
    lines = "\n".join([
        '{"url":"https://api.github.com/repos/odoo/odoo/pulls/264068",'
        '"title":"[ADD] x","updated_at":"2026-08-01T00:00:00Z","repo":"odoo/odoo"}',
        '{"url":"https://api.github.com/repos/odoo/odoo/pulls/264068",'
        '"title":"[ADD] x","updated_at":"2026-08-02T00:00:00Z","repo":"odoo/odoo"}',
        '{"url":"https://api.github.com/repos/odoo/enterprise/issues/99",'
        '"title":"an issue","updated_at":"2026-08-01T00:00:00Z","repo":"odoo/enterprise"}',
        "",
        "not json",
    ])
    monkeypatch.setattr(github, "_gh", lambda *a, **k: lines)

    out = github.list_manual_subscriptions()
    assert [s["id"] for s in out] == ["odoo/odoo#264068"]
    assert out[0]["url"] == "https://github.com/odoo/odoo/pull/264068"
    assert out[0]["number"] == 264068


# --- query surface -----------------------------------------------------------

def _tracked_item(**over):
    item = {
        "id": "odoo/odoo#1", "repo": "odoo/odoo", "repo_short": "odoo",
        "number": 1, "url": "u", "title": "t", "author": "a", "state": "OPEN",
        "is_draft": False, "target_branch": "master", "ci_state": "SUCCESS",
        "body": "desc", "comment_count": 1, "review_count": 2, "thread_count": 3,
        "activity_count": 6, "unresolved_threads": 1, "comments": [],
        "source": "notif", "added_at": "t", "updated_at": "t",
        "merged_at": None, "closed_at": None, "age_days": 5, "idle_days": 1,
        "since_last_look": [],
    }
    item.update(over)
    return item


def test_resolve_tracked_accepts_short_and_full_refs():
    items = [_tracked_item(), _tracked_item(
        id="odoo/enterprise#2", repo="odoo/enterprise", repo_short="enterprise",
        number=2)]
    assert prquery.resolve_tracked(items, "1")["id"] == "odoo/odoo#1"
    assert prquery.resolve_tracked(items, "enterprise#2")["id"] == "odoo/enterprise#2"
    assert prquery.resolve_tracked(
        items, "https://github.com/odoo/odoo/pull/1")["id"] == "odoo/odoo#1"
    with pytest.raises(ValueError):
        prquery.resolve_tracked(items, "999")


def test_resolve_tracked_rejects_ambiguous_number():
    # Same number in both repos, no repo given -> must not silently pick one.
    items = [_tracked_item(), _tracked_item(
        id="odoo/enterprise#1", repo="odoo/enterprise", repo_short="enterprise")]
    with pytest.raises(ValueError, match="ambiguous"):
        prquery.resolve_tracked(items, "1")


def test_summarize_tracked_omits_body_and_discussion():
    row = prquery.summarize_tracked(_tracked_item(comments=[{"body": "x"}]))
    assert "body" not in row and "discussion" not in row
    assert row["review_count"] == 2 and row["unresolved_threads"] == 1


def test_tracked_detail_exposes_nesting_keys():
    item = _tracked_item(comments=[
        {"kind": "review", "thread_id": "REV_1", "parent_id": None,
         "author": "r", "created_at": "t", "body": "LGTM", "url": "u",
         "path": None, "state": "APPROVED"},
        {"kind": "thread", "thread_id": "THR_1", "parent_id": "REV_1",
         "author": "r", "created_at": "t", "body": "why", "url": "u2",
         "path": "a/b.py", "state": "UNRESOLVED"},
    ])
    out = prquery.tracked_detail(item)
    assert out["body"] == "desc"
    assert [d["kind"] for d in out["discussion"]] == ["review", "thread"]
    # parent_id is what lets a consumer rebuild the dashboard's nesting.
    assert out["discussion"][1]["parent_id"] == "REV_1"
    assert out["discussion"][1]["path"] == "a/b.py"
    assert out["discussion"][0]["state"] == "APPROVED"


def test_load_tracked_include_dismissed_flags_them(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    _add(conn, "odoo/odoo#2")
    db.dismiss_tracked(conn, "odoo/odoo#2", "2026-08-02T00:00:00+00:00")
    conn.close()

    cfg = SimpleNamespace(db_path=tmp_path / "t.db")
    assert [t["id"] for t in prquery.load_tracked(cfg)] == ["odoo/odoo#1"]

    both = prquery.load_tracked(cfg, include_dismissed=True)
    assert {t["id"] for t in both} == {"odoo/odoo#1", "odoo/odoo#2"}
    dismissed = next(t for t in both if t["id"] == "odoo/odoo#2")
    assert dismissed["dismissed_at"] == "2026-08-02T00:00:00+00:00"
