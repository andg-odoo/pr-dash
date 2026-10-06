import dataclasses
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pr_dash import cli, db, derive, mcp_server, mergebot, render
from pr_dash import query as prquery
from tests.fakes import FakePR, pr_node


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
    db.set_dismissed(conn, "tracked", "odoo/odoo#1", "2026-08-02T00:00:00+00:00")
    assert db.list_tracked(conn) == []

    _add(conn, "odoo/odoo#1", source="notif")
    assert db.list_tracked(conn) == []
    assert len(db.list_tracked(conn, include_dismissed=True)) == 1


def test_explicit_track_revives_dismissed(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    db.set_dismissed(conn, "tracked", "odoo/odoo#1", "2026-08-02T00:00:00+00:00")

    _add(conn, "odoo/odoo#1", source="manual")
    assert [r["id"] for r in db.list_tracked(conn)] == ["odoo/odoo#1"]


def test_remove_tracked_cascades(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    db.replace_tab_comments(conn, "tracked", "odoo/odoo#1", [
        {"comment_id": "c1", "kind": "issue", "author": "a", "created_at": "t",
         "body": "hi", "path": None, "state": None, "url": "u"},
    ])
    db.upsert_tab_seen(conn, "tracked", {"pr_id": "odoo/odoo#1", "state": "OPEN", "head_sha": "sha",
                                         "activity_count": 1, "seen_at": "t"})

    assert db.remove_tracked(conn, "odoo/odoo#1") is True
    assert db.list_tab_comments(conn, "tracked") == {}
    assert db.list_tab_seen(conn, "tracked") == {}
    assert db.remove_tracked(conn, "odoo/odoo#1") is False


def test_update_tracked_state_preserves_tracking_metadata(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1", source="manual", when="2026-08-01T00:00:00+00:00")
    db.update_tab_state(conn, "tracked", "odoo/odoo#1", {
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

_TRACKED = FakePR(
    "odoo/odoo", 1, title="[FIX] sale: x", author="someone", base="master", head_sha="abc123",
    body="desc", updated_at="2026-08-01T00:00:00Z", checks={"ci/runbot": "SUCCESS"},
    comments=[{"author": "robodoo", "at": "2026-07-02T00:00:00Z", "body": "ci"},
              {"author": "human", "at": "2026-07-03T00:00:00Z", "body": "lgtm"}],
)


def test_tracked_row_merges_reviews_and_threads_in_time_order():
    # The bug this pins: an Odoo PR keeps almost nothing in `comments`. Fetching
    # only those made a PR with an approval and 20 inline threads look silent.
    node = pr_node(
        _TRACKED, "tracked",
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
    before = derive.tracked_row_from_node(pr_node(
        _TRACKED, "tracked",
        comments={"totalCount": 2, "nodes": []},
        reviews={"totalCount": 1, "nodes": []},
        reviewThreads={"totalCount": 0, "nodes": []}), "t")[0]
    after = derive.tracked_row_from_node(pr_node(
        _TRACKED, "tracked",
        comments={"totalCount": 2, "nodes": []},
        reviews={"totalCount": 2, "nodes": []},
        reviewThreads={"totalCount": 0, "nodes": []}), "t")[0]

    assert derive.tracked_since_last_look(
        ("OPEN", "abc123", before["activity_count"]),
        "OPEN", "abc123", after["activity_count"], first_run=False,
    ) == ["reply"]


def test_tracked_row_from_node_flattens_state_and_comments():
    node = pr_node(_TRACKED, "tracked")
    node["comments"]["totalCount"] = 4
    row, comments = derive.tracked_row_from_node(node, "2026-08-03T00:00:00+00:00")
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
        pr_node(_TRACKED, "tracked", author=None, commits={"nodes": []}, comments={}), "t",
    )
    assert row["author"] == ""
    assert row["ci_state"] is None
    assert row["comment_count"] == 0
    assert comments == []


# --- render.build_tracked_payload --------------------------------------------

def test_build_tracked_payload_sorts_active_first(tmp_path):
    conn = _conn(tmp_path)
    for pr_id, state, updated in [
        ("odoo/odoo#1", "OPEN", "2026-08-01T00:00:00Z"),
        ("odoo/odoo#2", "MERGED", "2026-07-01T00:00:00Z"),
        ("odoo/odoo#3", "OPEN", "2026-08-02T00:00:00Z"),
    ]:
        _add(conn, pr_id)
        db.update_tab_state(conn, "tracked", pr_id, {
            "state": state, "updated_at": updated, "created_at": updated,
            "head_sha": "s", "fetched_at": "t",
        })

    items, seen_updates = render.build_tracked_payload(conn)

    # Open rows newest-active first; resolved ones sink to the bottom so a long
    # tail of months-old closures can't bury the PRs still in flight.
    assert [i["id"] for i in items] == ["odoo/odoo#3", "odoo/odoo#1", "odoo/odoo#2"]
    assert len(seen_updates) == 3
    assert items[1]["repo_short"] == "odoo"


def test_build_tracked_payload_dismissed_rows(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    _add(conn, "odoo/odoo#2")
    db.set_dismissed(conn, "tracked", "odoo/odoo#2", "2026-08-02T00:00:00+00:00")

    items, updates = render.build_tracked_payload(conn)
    assert [i["id"] for i in items] == ["odoo/odoo#1"]
    # Shipped for the dashboard, a dismissed row still leaves the seen baseline alone.
    items, all_updates = render.build_tracked_payload(conn, include_dismissed=True)
    assert {i["id"]: i["dismissed_at"] for i in items} == {
        "odoo/odoo#1": None, "odoo/odoo#2": "2026-08-02T00:00:00+00:00"}
    assert all_updates == updates


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
    db.update_tab_state(conn, "tracked", "odoo/odoo#1", {"state": "OPEN", "head_sha": "s1"})

    items, updates = render.build_tracked_payload(conn)
    assert items[0]["since_last_look"] == []          # first run, no baseline
    render.commit_tab_seen_baseline(conn, "tracked", updates, "t")

    db.update_tab_state(conn, "tracked", "odoo/odoo#1", {"state": "MERGED", "head_sha": "s1"})
    items, _ = render.build_tracked_payload(conn)
    assert items[0]["since_last_look"] == ["resolved"]


# --- render.build_mine_payload -----------------------------------------------

def _mine(conn, pr_id, branch, updated, page=None, **node):
    repo, number = pr_id.split("#")
    db.add_mine(conn, pr_id, repo, int(number), f"https://github.com/{repo}/pull/{number}", "t")
    row, comments = derive.mine_row_from_node(
        {"title": f"PR {number}", "headRefName": branch, "baseRefName": "master",
         "headRefOid": "s", "createdAt": "2026-09-01T00:00:00Z", "updatedAt": updated,
         **node}, updated)
    db.update_tab_state(conn, "mine", pr_id, row)
    db.replace_tab_comments(conn, "mine", pr_id, comments)
    if page:
        db.upsert_mine_mergebot(conn, pr_id, dataclasses.asdict(page), "t")


def _mine_sets(conn):
    ec, done = "master-l10n_ec-6396725-andg", "saas-19.1-l10n_ar-6470810-andg"
    runbot = "https://runbot.odoo.com/runbot/batch/1"
    _mine(conn, "odoo/odoo#290657", done, "2026-10-06T00:00:00Z", state="CLOSED",
          page=mergebot.MergebotState("merged"))
    _mine(conn, "odoo/odoo#290109", ec, "2026-10-02T00:00:00Z", reviewDecision="APPROVED",
          commits={"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
              {"__typename": "StatusContext", "context": "ci/style", "state": "ERROR",
               "targetUrl": "https://runbot.odoo.com/runbot/build/2"},
              {"__typename": "StatusContext", "context": "ci/runbot", "state": "SUCCESS",
               "targetUrl": runbot}]}}}}]},
          page=mergebot.MergebotState("blocked", r_plus=False, checks=[
              mergebot.Check("ci/style", "fail", "", True, "kmagusiak")]))
    _mine(conn, "odoo/enterprise#132695", ec, "2026-10-03T00:00:00Z",
          page=mergebot.MergebotState("blocked", r_plus=False),
          comments={"nodes": [
              {"author": {"login": "robodoo"}, "body": "staging failed",
               "createdAt": "2026-10-03T00:00:00Z"},
              {"author": {"login": "clbr-odoo"}, "body": "why?",
               "createdAt": "2026-10-03T01:00:00Z"}]})
    _mine(conn, "odoo/odoo#269608", "19.0-mp-test-mode-andg", "2026-10-04T00:00:00Z",
          isDraft=True, page=mergebot.MergebotState("unknown", reason="timeout"))
    return ec, done, runbot


def test_mine_payload_bands_members_and_discussion(tmp_path):
    conn = _conn(tmp_path)
    ec, done, runbot = _mine_sets(conn)

    sets, seen_updates = render.build_mine_payload(conn, "andg")

    # Needs you above Open, the Done set below them, and the draft never lifts.
    assert [(s["key"], s["band"]) for s in sets] == [
        (ec, "needs"), ("19.0-mp-test-mode-andg", "open"), (done, "done")]
    assert len(seen_updates) == 4
    odoo = next(m for m in sets[0]["members"] if m["ref"] == "odoo#290109")
    assert (odoo["ci"], odoo["override"], odoo["review"], odoo["runbot_url"]) == (
        "green", [{"check": "ci/style", "by": "kmagusiak"}], "approved · r+ missing", runbot)
    # Bot comments are flagged for the browser to hide, each comment names its member.
    assert [(g["entry"]["author"], g["entry"]["is_bot"], g["entry"]["member"])
            for g in sets[0]["discussion"]] == [
        ("clbr-odoo", False, "enterprise#132695"), ("robodoo", True, "enterprise#132695")]
    [mp] = sets[1]["members"]
    assert (mp["draft"], mp["mergebot_unknown"], mp["review"]) == (True, True, "")


def test_tracked_and_mine_payloads_ship_one_discussion_tree(tmp_path):
    conn = _conn(tmp_path)

    def said(login, at, url, review=None, **extra):
        return {"author": {"login": login}, "createdAt": at, "submittedAt": at, "url": url,
                "body": url, "pullRequestReview": review and {"id": review}, **extra}

    node = {
        "comments": {"nodes": [said("fw-bot", "2026-10-04T00:00:00Z", "c1")]},
        "reviews": {"nodes": [
            said("jov-odoo", "2026-10-01T00:00:00Z", "r1", id="R1", state="CHANGES_REQUESTED"),
            said("robodoo", "2026-10-02T00:00:00Z", "r2", id="R2", state="COMMENTED"),
            said("andg", "2026-10-03T00:00:00Z", "", id="R3", state="COMMENTED")]},
        "reviewThreads": {"nodes": [
            {"id": "T1", "path": "a.py", "isResolved": False, "comments": {"nodes": [
                said("jov-odoo", "2026-10-01T00:00:01Z", "t1a", "R1"),
                said("andg", "2026-10-03T00:00:00Z", "t1b", "R3")]}},
            {"id": "T2", "path": "b.py", "isResolved": True, "comments": {"nodes": [
                said("clbr-odoo", "2026-10-02T01:00:00Z", "t2a", "R2")]}},
            {"id": "T3", "path": "c.py", "isResolved": True, "comments": {"nodes": [
                said("clbr-odoo", "2026-10-01T00:00:00Z", "t3a", "R1")]}}]},
    }
    _add(conn, "odoo/odoo#1")
    db.replace_tab_comments(conn, "tracked", "odoo/odoo#1", derive.discussion_stream(node))
    _mine(conn, "odoo/odoo#1", "master-x-andg", "2026-10-04T00:00:00Z", **node)

    def shape(tree):
        return [(g["kind"], g["entry"] and (g["entry"]["url"], g["entry"]["is_bot"]),
                 [(t["thread_id"], [c["url"] for c in t["comments"]]) for t in g["threads"]])
                for g in tree]

    [tracked], _ = render.build_tracked_payload(conn)
    [mine], _ = render.build_mine_payload(conn, "andg")
    # Newest first, threads under their review oldest first, a bot review's thread orphaned.
    assert shape(tracked["discussion"]) == shape(mine["discussion"]) == [
        ("issue", ("c1", True), []),
        ("orphan", None, [("T2", ["t2a"])]),
        ("review", ("r2", True), []),
        ("review", ("r1", False), [("T3", ["t3a"]), ("T1", ["t1a", "t1b"])]),
    ]
    assert {c["member"] for g in mine["discussion"] for t in g["threads"]
            for c in t["comments"]} == {"odoo#1"}


def test_acknowledge_through_the_listener_and_fyi_clearing_on_a_look(tmp_path):
    conn = _conn(tmp_path)
    ec, _, _ = _mine_sets(conn)
    cfg = SimpleNamespace(db_path=tmp_path / "t.db")
    sets, seen_updates = render.build_mine_payload(conn, "andg")
    render.commit_tab_seen_baseline(conn, "mine", seen_updates, "2026-10-05T00:00:00Z")
    reply = {"nodes": [{"author": {"login": "clbr-odoo"}, "body": "ping",
                        "createdAt": "2026-10-05T06:00:00Z"}]}
    _mine(conn, "odoo/enterprise#132695", ec, "2026-10-05T06:00:00Z", comments=reply)

    sets, seen_updates = render.build_mine_payload(conn, "andg")
    assert mcp_server._apply_ack_ops(cfg, [
        {"op": "ack", "key": ec, "fingerprint": sets[0]["fingerprint"]}]) == 1
    sets, _ = render.build_mine_payload(conn, "andg")
    ec_set = next(s for s in sets if s["key"] == ec)
    # Acknowledged, it sits in Open with its Action items, and the reply shows until a look.
    assert (ec_set["band"], ec_set["acknowledged"], len(ec_set["actions"]), ec_set["fyi"]) == (
        "open", True, 2, ["new reply"])
    render.commit_tab_seen_baseline(conn, "mine", seen_updates, "2026-10-05T07:00:00Z")
    assert next(s for s in render.build_mine_payload(conn, "andg")[0] if s["key"] == ec)[
        "fyi"] == []

    # A push changes the fingerprint, the refresh drops the Acknowledge and the set lifts again.
    _mine(conn, "odoo/enterprise#132695", ec, "2026-10-05T08:00:00Z", headRefOid="s2")
    sets, _ = render.build_mine_payload(conn, "andg")
    db.drop_stale_mine_acks(conn, {s["key"]: s["fingerprint"] for s in sets})
    assert (sets[0]["key"], sets[0]["band"], db.list_mine_acks(conn)) == (ec, "needs", {})


def test_dismissed_mine_set_stays_hidden_after_a_refresh(tmp_path):
    conn = _conn(tmp_path)
    ec, _, _ = _mine_sets(conn)
    cfg = SimpleNamespace(db_path=tmp_path / "t.db")
    ops = [{"op": "dismiss", "pr_id": pr_id}
           for pr_id in ("odoo/odoo#290109", "odoo/enterprise#132695")]
    assert mcp_server._apply_dismiss_ops(cfg, "mine", ops) == 2

    # The next refresh writes fresh state for the same PRs.
    _mine(conn, "odoo/enterprise#132695", ec, "2026-10-05T00:00:00Z")
    sets, _ = render.build_mine_payload(conn, "andg")
    assert ec not in [s["key"] for s in sets]
    assert len(sets) == 2

    # A PR opened later on the branch stays its own live set, the dismissed ones apart.
    _mine(conn, "odoo/odoo#291000", ec, "2026-10-06T00:00:00Z")
    sets, seen_updates = render.build_mine_payload(conn, "andg")
    gone, _ = render.build_mine_payload(conn, "andg", dismissed_only=True)
    assert [m["id"] for s in sets if s["key"] == ec for m in s["members"]] == ["odoo/odoo#291000"]
    assert [(s["key"], [(m["id"], bool(m["dismissed_at"])) for m in s["members"]]) for s in gone] == [
        (ec, [("odoo/odoo#290109", True), ("odoo/enterprise#132695", True)])]
    assert {u["pr_id"] for u in seen_updates} == {
        "odoo/odoo#291000", "odoo/odoo#290657", "odoo/odoo#269608"}


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


# --- query surface -----------------------------------------------------------

def _tracked_item(**over):
    item = {
        "id": "odoo/odoo#1", "repo": "odoo/odoo", "repo_short": "odoo",
        "number": 1, "url": "u", "title": "t", "author": "a", "state": "OPEN",
        "is_draft": False, "target_branch": "master", "ci_state": "SUCCESS",
        "body": "desc", "comment_count": 1, "review_count": 2, "thread_count": 3,
        "activity_count": 6, "unresolved_threads": 1, "discussion": [],
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
    row = prquery.summarize_tracked(_tracked_item(discussion=[{"kind": "issue"}]))
    assert "body" not in row and "discussion" not in row
    assert row["review_count"] == 2 and row["unresolved_threads"] == 1



def test_load_tracked_include_dismissed_flags_them(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "odoo/odoo#1")
    _add(conn, "odoo/odoo#2")
    db.set_dismissed(conn, "tracked", "odoo/odoo#2", "2026-08-02T00:00:00+00:00")
    conn.close()

    cfg = SimpleNamespace(db_path=tmp_path / "t.db")
    assert [t["id"] for t in prquery.load_tracked(cfg)] == ["odoo/odoo#1"]

    both = prquery.load_tracked(cfg, include_dismissed=True)
    assert {t["id"] for t in both} == {"odoo/odoo#1", "odoo/odoo#2"}
    dismissed = next(t for t in both if t["id"] == "odoo/odoo#2")
    assert dismissed["dismissed_at"] == "2026-08-02T00:00:00+00:00"
