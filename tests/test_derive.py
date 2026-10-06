import dataclasses
from pathlib import Path

from pr_dash import derive, mergebot

MERGEBOT_FIXTURES = Path(__file__).parent / "fixtures" / "mergebot"


def test_signatures_ignore_context_and_line_numbers():
    # Same +/- lines, different context + hunk position (as after a rebase onto
    # newer master) must hash identically.
    before = "diff --git a/m/x.py b/m/x.py\n@@ -1,3 +1,3 @@\n ctx_a\n-old\n+new\n ctx_b\n"
    after = "diff --git a/m/x.py b/m/x.py\n@@ -50,3 +50,3 @@\n shifted\n-old\n+new\n moved\n"
    assert (derive.file_change_signatures(before)["m/x.py"]
            == derive.file_change_signatures(after)["m/x.py"])


def test_signatures_detect_real_change():
    a = "diff --git a/m/x.py b/m/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    b = "diff --git a/m/x.py b/m/x.py\n@@ -1 +1 @@\n-old\n+newer\n"
    assert (derive.file_change_signatures(a)["m/x.py"]
            != derive.file_change_signatures(b)["m/x.py"])


def test_signatures_per_file():
    d = ("diff --git a/a.py b/a.py\n@@ -1 +1 @@\n+a\n"
         "diff --git a/b.py b/b.py\n@@ -1 +1 @@\n+b\n")
    assert set(derive.file_change_signatures(d)) == {"a.py", "b.py"}
    assert derive.file_change_signatures("") == {}


def _difffile(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


def test_compact_stubs_the_giant_and_leaves_the_rest_alone():
    # The odoo#277589 shape: one data file drowning a few kb of reviewable code.
    diff = (_difffile("m/models/x.py", "+code\n")
            + _difffile("m/data/res.city.csv", "+row\n" * 5000)
            + _difffile("m/i18n/fr.po", "+msgid\n"))
    out = derive.compact_diff(diff, repo="odoo/odoo", number=277589, max_file_chars=1000)

    assert out.stubbed == ["m/data/res.city.csv"]
    assert out.dropped == ["m/i18n/fr.po"]
    assert out.partial is True
    assert _difffile("m/models/x.py", "+code\n") in out.text
    assert "+row" not in out.text and len(out.text) < 1000
    # The stub still splits as one file, keeps its path, and says whose doing it
    # is - including a deep link to the file on GitHub.
    files = {p: c for p, c in derive.iter_diff_files(out.text) if p}
    assert set(files) == {"m/models/x.py", "m/data/res.city.csv"}
    stub = files["m/data/res.city.csv"]
    assert "+5000/-0 lines" in stub and "omitted by pr-dash" in stub
    assert derive.pr_file_url("odoo/odoo", 277589, "m/data/res.city.csv") in stub
    # Idempotent: the review path re-compacts what the cache already compacted.
    again = derive.compact_diff(out.text, repo="odoo/odoo", number=277589, max_file_chars=1000)
    assert again.text == out.text and again.stubbed == out.stubbed


def test_compact_keeps_noise_as_a_stub_for_the_cache():
    diff = _difffile("m/i18n/fr.po", "+msgid\n" * 500)
    out = derive.compact_diff(diff, stub_noise=True, max_file_chars=100_000)
    assert out.dropped == [] and out.stubbed == ["m/i18n/fr.po"]
    assert "msgid" not in out.text
    # ...and the review path drops that stub outright, still reporting it so the
    # prompt can say the translations were omitted by us.
    assert derive.compact_diff(out.text).text == ""
    assert derive.compact_diff(out.text).dropped == ["m/i18n/fr.po"]


def test_stubbed_file_signature_tracks_its_omitted_content():
    small = derive.compact_diff(_difffile("m/data/big.csv", "+row\n" * 500), max_file_chars=100)
    grown = derive.compact_diff(_difffile("m/data/big.csv", "+row\n" * 600), max_file_chars=100)
    # A stub has no +/- lines of its own; without special care every stubbed
    # file would hash alike and a re-pushed data file would render as
    # "unchanged since your review".
    assert (derive.file_change_signatures(small.text)["m/data/big.csv"]
            != derive.file_change_signatures(grown.text)["m/data/big.csv"])


def test_thread_signature_detects_new_reply():
    a = [{"thread_id": "t1", "last_reply_at": "2026-05-01"}]
    b = [{"thread_id": "t1", "last_reply_at": "2026-05-02"}]  # someone replied
    assert derive.thread_signature(a) != derive.thread_signature(b)
    # order-independent
    two = [{"thread_id": "t1", "last_reply_at": "x"}, {"thread_id": "t2", "last_reply_at": "y"}]
    assert derive.thread_signature(two) == derive.thread_signature(list(reversed(two)))


def test_since_last_look_first_run_flags_nothing():
    assert derive.since_last_look_tags(None, "sha", "SUCCESS", "sig", first_run=True) == []


def test_since_last_look_new_pr():
    assert derive.since_last_look_tags(None, "sha", "SUCCESS", "sig", first_run=False) == ["new"]


def test_since_last_look_detects_each_change():
    prev = ("old_sha", "PENDING", "old_sig")
    assert derive.since_last_look_tags(prev, "new_sha", "PENDING", "old_sig", first_run=False) == ["pushed"]
    assert derive.since_last_look_tags(prev, "old_sha", "SUCCESS", "old_sig", first_run=False) == ["ci"]
    assert derive.since_last_look_tags(prev, "old_sha", "PENDING", "new_sig", first_run=False) == ["reply"]
    # nothing changed
    assert derive.since_last_look_tags(prev, "old_sha", "PENDING", "old_sig", first_run=False) == []
    # multiple at once, stable order
    assert derive.since_last_look_tags(prev, "new_sha", "SUCCESS", "new_sig", first_run=False) == ["pushed", "ci", "reply"]


def test_path_to_module_enterprise():
    assert derive.path_to_module("odoo/enterprise", "account_accountant/models/foo.py") == "account_accountant"
    assert derive.path_to_module("odoo/enterprise", "README.md") is None


def test_path_to_module_odoo_addons():
    assert derive.path_to_module("odoo/odoo", "addons/sale/models/sale_order.py") == "sale"
    assert derive.path_to_module("odoo/odoo", "odoo/addons/base/models/res_partner.py") == "base"


def test_path_to_module_odoo_core():
    assert derive.path_to_module("odoo/odoo", "odoo/orm/models.py") == "_core"
    assert derive.path_to_module("odoo/odoo", "setup.py") == "_root"


def test_path_to_module_unknown_repo():
    assert derive.path_to_module("unknown/repo", "foo/bar.py") is None


def test_modules_for_dedupes_and_orders():
    paths = ["addons/sale/a.py", "addons/sale/b.py", "addons/account/c.py"]
    assert derive.modules_for("odoo/odoo", paths) == ["sale", "account"]


def test_installable_modules_filters_underscores():
    assert derive.installable_modules(["sale", "_core", "_root", "account"]) == ["sale", "account"]


def test_parse_linked_task():
    assert derive.parse_linked_task("Fixes task-123456 something") == ("task", "123456")
    assert derive.parse_linked_task("Task-789012") == ("task", "789012")
    assert derive.parse_linked_task("task 4567") == ("task", "4567")
    assert derive.parse_linked_task("opw-987654") == ("opw", "987654")
    assert derive.parse_linked_task("Opw 12345") == ("opw", "12345")
    # markdown-linked / tilde forms found in real PR bodies
    assert derive.parse_linked_task("task-[6234716](https://odoo.com/web#id=6234716)") == ("task", "6234716")
    assert derive.parse_linked_task("task ~6234716") == ("task", "6234716")
    assert derive.parse_linked_task("task-~6234716") == ("task", "6234716")
    assert derive.parse_linked_task("OPW-22") is None  # too short
    assert derive.parse_linked_task("task list has 12345 items") is None  # noise class stops at letters
    assert derive.parse_linked_task("no task here") is None
    assert derive.parse_linked_task(None) is None


def test_derive_threads_basic():
    threads = [
        {
            "id": "t1",
            "isResolved": False,
            "comments": {"nodes": [
                {"author": {"login": "me"}, "createdAt": "2026-05-01T00:00:00Z"},
                {"author": {"login": "them"}, "createdAt": "2026-05-02T00:00:00Z"},
            ]},
        },
        {
            "id": "t2",
            "isResolved": False,
            "comments": {"nodes": [
                {"author": {"login": "them"}, "createdAt": "2026-05-03T00:00:00Z"},
            ]},
        },
        {
            "id": "t3",
            "isResolved": True,
            "comments": {"nodes": [
                {"author": {"login": "me"}, "createdAt": "2026-05-04T00:00:00Z"},
                {"author": {"login": "them"}, "createdAt": "2026-05-05T00:00:00Z"},
            ]},
        },
    ]
    result = derive.derive_threads(threads, "me")
    assert result.unresolved == 2
    assert result.awaiting_my_reply is True
    assert len(result.threads) == 3


def test_derive_threads_no_awaiting_when_i_replied_last():
    threads = [{
        "id": "t1",
        "isResolved": False,
        "comments": {"nodes": [
            {"author": {"login": "them"}, "createdAt": "2026-05-01T00:00:00Z"},
            {"author": {"login": "me"}, "createdAt": "2026-05-02T00:00:00Z"},
        ]},
    }]
    result = derive.derive_threads(threads, "me")
    assert result.awaiting_my_reply is False


def test_is_personally_requested():
    reqs = [
        {"requestedReviewer": {"__typename": "Team", "slug": "rd-accounting"}},
        {"requestedReviewer": {"__typename": "User", "login": "me"}},
    ]
    assert derive.is_personally_requested(reqs, "me") is True
    assert derive.is_personally_requested(reqs, "other") is False
    assert derive.is_personally_requested([reqs[0]], "me") is False


def test_heuristic_score_monotonic_in_size():
    base = dict(changed_files=1, modules=["sale"], unresolved_threads=0, previously_reviewed=False)
    small = derive.heuristic_score(additions=5, deletions=2, **base)
    big = derive.heuristic_score(additions=500, deletions=200, **base)
    assert big > small


def test_heuristic_score_reread_discount():
    args = dict(additions=200, deletions=100, changed_files=4, modules=["sale"], unresolved_threads=0)
    fresh = derive.heuristic_score(previously_reviewed=False, **args)
    reread = derive.heuristic_score(previously_reviewed=True, **args)
    assert abs(reread - fresh * 0.5) < 0.01


def test_bucket_for():
    assert derive.bucket_for(2, 5, 20, 60) == "S"
    assert derive.bucket_for(5, 5, 20, 60) == "M"
    assert derive.bucket_for(19, 5, 20, 60) == "M"
    assert derive.bucket_for(20, 5, 20, 60) == "L"
    assert derive.bucket_for(59, 5, 20, 60) == "L"
    assert derive.bucket_for(60, 5, 20, 60) == "XL"


def test_status_check_state_picks_runbot():
    rollup = {
        "state": "SUCCESS",
        "contexts": {"nodes": [
            {"__typename": "StatusContext", "context": "ci/style", "state": "SUCCESS",
             "targetUrl": "https://runbot.odoo.com/runbot/batch/1/build/100"},
            {"__typename": "StatusContext", "context": "ci/runbot", "state": "PENDING",
             "targetUrl": "https://runbot.odoo.com/runbot/batch/2/build/200"},
        ]},
    }
    state, url = derive.status_check_state(rollup)
    assert state == "SUCCESS"
    assert url == "https://runbot.odoo.com/runbot/batch/2/build/200"


def test_status_check_state_falls_back_to_any_runbot():
    rollup = {
        "state": "SUCCESS",
        "contexts": {"nodes": [
            {"__typename": "StatusContext", "context": "ci/lint", "state": "SUCCESS",
             "targetUrl": "https://runbot.odoo.com/runbot/batch/9/build/9"},
        ]},
    }
    _, url = derive.status_check_state(rollup)
    assert url == "https://runbot.odoo.com/runbot/batch/9/build/9"


def test_status_check_state_none():
    state, url = derive.status_check_state(None)
    assert state is None and url is None


def test_failing_checks_extracts_failures():
    rollup = {"contexts": {"nodes": [
        {"__typename": "StatusContext", "context": "ci/runbot", "state": "FAILURE",
         "targetUrl": "https://runbot.odoo.com/x"},
        {"__typename": "StatusContext", "context": "ci/style", "state": "SUCCESS",
         "targetUrl": "https://x"},
        {"__typename": "CheckRun", "name": "tests", "conclusion": "FAILURE",
         "detailsUrl": "https://gh/checks/1"},
        {"__typename": "CheckRun", "name": "lint", "conclusion": "SUCCESS",
         "detailsUrl": "https://gh/checks/2"},
        {"__typename": "CheckRun", "name": "flaky", "conclusion": "TIMED_OUT",
         "detailsUrl": "https://gh/checks/3"},
    ]}}
    failures = derive.failing_checks(rollup)
    names = {f["name"] for f in failures}
    assert names == {"ci/runbot", "tests", "flaky"}
    runbot = next(f for f in failures if f["name"] == "ci/runbot")
    assert runbot["url"] == "https://runbot.odoo.com/x"


def test_failing_checks_empty_when_green_or_none():
    assert derive.failing_checks(None) == []
    green = {"contexts": {"nodes": [
        {"__typename": "StatusContext", "context": "ci", "state": "SUCCESS", "targetUrl": "u"},
        {"__typename": "CheckRun", "name": "t", "conclusion": "SUCCESS", "detailsUrl": "u"},
    ]}}
    assert derive.failing_checks(green) == []


def test_latest_review_requested_at_picks_latest_for_me():
    timeline = [
        {"__typename": "ReviewRequestedEvent", "createdAt": "2026-05-01T00:00:00Z",
         "requestedReviewer": {"__typename": "User", "login": "me"}},
        {"__typename": "ReviewRequestedEvent", "createdAt": "2026-05-05T00:00:00Z",
         "requestedReviewer": {"__typename": "User", "login": "other"}},
        {"__typename": "ReviewRequestedEvent", "createdAt": "2026-05-10T00:00:00Z",
         "requestedReviewer": {"__typename": "User", "login": "me"}},
    ]
    assert derive.latest_review_requested_at(timeline, "me", "fallback") == "2026-05-10T00:00:00Z"


def test_previously_reviewed():
    timeline = [
        {"__typename": "PullRequestReview", "author": {"login": "me"}, "submittedAt": "2026-05-01T00:00:00Z"},
    ]
    assert derive.previously_reviewed(timeline, "me") is True
    assert derive.previously_reviewed(timeline, "other") is False
    assert derive.previously_reviewed([], "me") is False


# --- comments / pending-review derivation ------------------------------------

def _node_with_comments(*, threads=None, reviews=None, issue_comments=None):
    return {
        "reviewThreads": {"nodes": threads or []},
        "reviews": {"nodes": reviews or []},
        "comments": {"nodes": issue_comments or []},
    }


def test_is_bot():
    assert derive.is_bot("robodoo") is True
    assert derive.is_bot("fw-bot") is True
    assert derive.is_bot("dependabot[bot]") is True
    assert derive.is_bot("ROBODOO") is True
    assert derive.is_bot("alice") is False
    assert derive.is_bot(None) is False


def test_comment_snippet_strips_markdown_and_truncates():
    body = "Please **fix** the [helper](https://x/y)\nand add a test."
    assert derive.comment_snippet(body) == "Please fix the helper and add a test."
    assert derive.comment_snippet(None) == ""
    long = "x" * 250
    out = derive.comment_snippet(long, limit=100)
    assert len(out) == 101 and out.endswith("…")  # 100 chars + ellipsis


def test_derive_comments_flattens_all_kinds():
    node = _node_with_comments(
        threads=[{
            "id": "T1", "isResolved": False,
            "comments": {"nodes": [
                {"author": {"login": "alice"}, "createdAt": "2026-05-01T00:00:00Z",
                 "body": "line?", "path": "sale/x.py", "databaseId": 11,
                 "url": "https://c/11"},
            ]},
        }],
        reviews=[
            {"id": "R1", "author": {"login": "bob"}, "state": "APPROVED",
             "submittedAt": "2026-05-02T00:00:00Z", "body": "lgtm", "url": "https://r/1"},
        ],
        issue_comments=[
            {"author": {"login": "carol"}, "createdAt": "2026-05-03T00:00:00Z",
             "body": "ping", "databaseId": 99, "url": "https://i/99"},
        ],
    )
    rows, my_pending = derive.derive_comments(node, "me")
    assert my_pending is False
    kinds = {r["kind"] for r in rows}
    assert kinds == {"thread", "review", "issue"}
    thread_row = next(r for r in rows if r["kind"] == "thread")
    assert thread_row["thread_id"] == "T1"
    assert thread_row["comment_id"] == "11"
    assert thread_row["path"] == "sale/x.py"
    review_row = next(r for r in rows if r["kind"] == "review")
    assert review_row["comment_id"] == "R1"
    assert review_row["state"] == "APPROVED"


def test_derive_comments_my_pending_review():
    # A PENDING review by the viewer -> my_pending True.
    node = _node_with_comments(reviews=[
        {"id": "R1", "author": {"login": "me"}, "state": "PENDING",
         "submittedAt": None, "body": "draft note", "url": None},
    ])
    _, my_pending = derive.derive_comments(node, "me")
    assert my_pending is True


def test_derive_comments_pending_by_other_is_not_mine():
    node = _node_with_comments(reviews=[
        {"id": "R1", "author": {"login": "someone"}, "state": "PENDING",
         "submittedAt": None, "body": "", "url": None},
    ])
    _, my_pending = derive.derive_comments(node, "me")
    assert my_pending is False


def test_derive_comments_skips_missing_ids():
    node = _node_with_comments(
        threads=[{"id": "T1", "isResolved": True, "comments": {"nodes": [
            {"author": {"login": "a"}, "createdAt": "t", "body": "b",
             "path": None, "databaseId": None, "url": None},
        ]}}],
        issue_comments=[{"author": {"login": "a"}, "createdAt": "t", "body": "b",
                         "databaseId": None, "url": None}],
    )
    rows, _ = derive.derive_comments(node, "me")
    assert rows == []


# --- detect_review_ping ------------------------------------------------------

def _pnode(*, comments=None, threads=None, reviews=None, requests=None, author=None):
    return {
        "author": {"login": author} if author else None,
        "comments": {"nodes": comments or []},
        "reviewThreads": {"nodes": [{"comments": {"nodes": tc}} for tc in (threads or [])]},
        "reviews": {"nodes": reviews or []},
        "timelineItems": {"nodes": requests or []},
    }


def _c(login, at, body="please re-review"):
    return {"author": {"login": login}, "createdAt": at, "body": body}


def _rv(login, at, state="APPROVED"):
    return {"author": {"login": login}, "submittedAt": at, "state": state}


def _req(login, at):
    return {"createdAt": at, "requestedReviewer": {"login": login}}


def test_ping_set_from_conversation_comment():
    node = _pnode(reviews=[_rv("me", "2026-07-01T00:00:00Z")],
                  comments=[_c("alice", "2026-07-02T00:00:00Z", "done, ready for r+")])
    p = derive.detect_review_ping(node, "me")
    assert p["ping_author"] == "alice"
    assert p["ping_at"] == "2026-07-02T00:00:00Z"
    assert "ready" in p["ping_body"]


def test_ping_set_from_thread_reply():
    node = _pnode(reviews=[_rv("me", "2026-07-01T00:00:00Z")],
                  threads=[[_c("alice", "2026-07-02T00:00:00Z")]])
    assert derive.detect_review_ping(node, "me")["ping_author"] == "alice"


def test_ping_none_without_my_activity():
    node = _pnode(comments=[_c("alice", "2026-07-02T00:00:00Z")])
    assert derive.detect_review_ping(node, "me") is None


def test_ping_none_for_bot_only_traffic():
    node = _pnode(reviews=[_rv("me", "2026-07-01T00:00:00Z")],
                  comments=[_c("robodoo", "2026-07-02T00:00:00Z")])
    assert derive.detect_review_ping(node, "me") is None


def test_ping_cleared_by_formal_rerequest():
    node = _pnode(reviews=[_rv("me", "2026-07-01T00:00:00Z")],
                  comments=[_c("alice", "2026-07-02T00:00:00Z")],
                  requests=[_req("me", "2026-07-03T00:00:00Z")])
    assert derive.detect_review_ping(node, "me") is None


def test_ping_cleared_by_my_own_later_reply():
    # My reply after the ping lifts my-last-activity past it -> no candidate.
    node = _pnode(reviews=[_rv("me", "2026-07-01T00:00:00Z")],
                  comments=[_c("alice", "2026-07-02T00:00:00Z"),
                            _c("me", "2026-07-03T00:00:00Z")])
    assert derive.detect_review_ping(node, "me") is None


def test_ping_cleared_by_other_reviewer_submitted_review():
    # Author pinged, but a final reviewer submitted a review afterwards.
    node = _pnode(reviews=[_rv("me", "2026-07-01T00:00:00Z"),
                           _rv("bob", "2026-07-03T00:00:00Z")],
                  comments=[_c("alice", "2026-07-02T00:00:00Z")])
    assert derive.detect_review_ping(node, "me") is None


def test_ping_cleared_by_other_reviewer_thread_reply():
    # bob is an established reviewer (submitted earlier) who replies after the ping.
    node = _pnode(reviews=[_rv("me", "2026-07-01T00:00:00Z"),
                           _rv("bob", "2026-06-01T00:00:00Z")],
                  comments=[_c("alice", "2026-07-02T00:00:00Z")],
                  threads=[[_c("bob", "2026-07-03T00:00:00Z")]])
    assert derive.detect_review_ping(node, "me") is None


def test_ping_stands_when_other_reviewer_acted_before_ping():
    # A reviewer's earlier activity must not clear a later ping.
    node = _pnode(reviews=[_rv("me", "2026-07-01T00:00:00Z"),
                           _rv("bob", "2026-06-01T00:00:00Z")],
                  comments=[_c("alice", "2026-07-02T00:00:00Z")])
    p = derive.detect_review_ping(node, "me")
    assert p and p["ping_author"] == "alice"


def test_ping_stands_despite_authors_commented_review_wrapper():
    # Replying to an inline review comment wraps the author's reply in a
    # COMMENTED review object. That must neither make the author an
    # "established reviewer" (excluded from pinging) nor clear their own ping.
    node = _pnode(
        author="alice",
        reviews=[_rv("me", "2026-07-01T00:00:00Z"),
                 _rv("alice", "2026-07-02T00:00:00Z", state="COMMENTED")],
        threads=[[_c("alice", "2026-07-02T00:00:00Z",
                     "I already pushed your suggestions, please check")]],
    )
    p = derive.detect_review_ping(node, "me")
    assert p and p["ping_author"] == "alice"


# --- detect_push_since_review ------------------------------------------------

def _shnode(*, reviews=None, head=None, head_at=None, head_ref=None):
    node = {"reviews": {"nodes": reviews or []}, "headRefOid": head_ref}
    if head:
        node["commits"] = {"nodes": [{"commit": {"oid": head, "committedDate": head_at}}]}
    return node


def _rvc(login, at, sha, state="APPROVED"):
    return {"author": {"login": login}, "submittedAt": at, "state": state,
            "commit": {"oid": sha}}


def test_push_set_when_head_moved_past_my_review():
    node = _shnode(reviews=[_rvc("me", "2026-07-01T00:00:00Z", "aaa")],
                   head="bbb", head_at="2026-07-02T00:00:00Z")
    p = derive.detect_push_since_review(node, "me")
    assert p == {"push_at": "2026-07-02T00:00:00Z", "push_sha": "bbb"}


def test_push_none_when_head_is_the_sha_i_reviewed():
    node = _shnode(reviews=[_rvc("me", "2026-07-01T00:00:00Z", "aaa")],
                   head="aaa", head_at="2026-07-01T00:00:00Z")
    assert derive.detect_push_since_review(node, "me") is None


def test_push_anchors_on_my_latest_review_not_my_first():
    # Reviewed at aaa, they pushed bbb, I reviewed again at bbb: nothing new.
    node = _shnode(reviews=[_rvc("me", "2026-07-01T00:00:00Z", "aaa"),
                            _rvc("me", "2026-07-03T00:00:00Z", "bbb")],
                   head="bbb", head_at="2026-07-02T00:00:00Z")
    assert derive.detect_push_since_review(node, "me") is None


def test_push_ignores_other_peoples_reviews():
    # A final reviewer's review sits on the new head; mine is still on the old
    # one. The anchor must be mine, so this is a push since *my* review.
    node = _shnode(reviews=[_rvc("me", "2026-07-01T00:00:00Z", "aaa"),
                            _rvc("bob", "2026-07-04T00:00:00Z", "bbb")],
                   head="bbb", head_at="2026-07-02T00:00:00Z")
    p = derive.detect_push_since_review(node, "me")
    assert p and p["push_sha"] == "bbb"


def test_push_none_without_a_review_of_mine():
    node = _shnode(reviews=[_rvc("bob", "2026-07-01T00:00:00Z", "aaa")],
                   head="bbb", head_at="2026-07-02T00:00:00Z")
    assert derive.detect_push_since_review(node, "me") is None


def test_push_none_when_my_review_has_no_commit():
    node = _shnode(
        reviews=[{"author": {"login": "me"}, "submittedAt": "2026-07-01T00:00:00Z",
                  "state": "APPROVED", "commit": None}],
        head="bbb", head_at="2026-07-02T00:00:00Z",
    )
    assert derive.detect_push_since_review(node, "me") is None


def test_push_falls_back_to_head_ref_oid_without_commits():
    node = _shnode(reviews=[_rvc("me", "2026-07-01T00:00:00Z", "aaa")], head_ref="bbb")
    p = derive.detect_push_since_review(node, "me")
    assert p == {"push_at": "", "push_sha": "bbb"}


# --- derive.branch_sets ------------------------------------------------------

def _mine_row(repo, number, branch, *, checks=(("ci/runbot", "SUCCESS"),),
              pushed="2026-10-02T00:00:00Z", source=None, **over):
    node = {
        "title": f"PR {number}", "state": "OPEN", "isDraft": False, "headRefName": branch,
        "createdAt": "2026-09-01T00:00:00Z",
        "updatedAt": "2026-10-01T00:00:00Z", "reviewDecision": None,
        "mergeable": "MERGEABLE",
        "reviewRequests": {"nodes": [
            {"requestedReviewer": {"__typename": "User", "login": "clbr-odoo"}},
            {"requestedReviewer": {"__typename": "Team", "slug": "rd-accounting"}},
        ]},
        "commits": {"nodes": [{"commit": {
            "committedDate": pushed,
            "statusCheckRollup": {"contexts": {"nodes": [
                {"__typename": "StatusContext", "context": name, "state": state}
                for name, state in checks
            ]}},
        }}]},
        **over,
    }
    row, _ = derive.mine_row_from_node(node, "2026-10-05T00:00:00+00:00")
    return {**row, "id": f"{repo}#{number}", "repo": repo, "number": number, "url": "u",
            "dismissed_at": None, "source_id": source}


def _sets(rows, pages, *, streams=None, acks=None, seen=None, now="2026-10-05T00:00:00Z"):
    return derive.branch_sets(rows, pages, streams=streams or {}, login="andg",
                              acks=acks or {}, seen=seen or {}, now=now)


def _entry(kind, author, at, *, state=None, thread=None, path=None, body=None):
    return {"kind": kind, "author": author, "created_at": at, "state": state,
            "thread_id": thread, "path": path, "body": body}


def _actions(s):
    return [(a["member"], a["kind"], a["text"]) for a in s["actions"]]


def _page(name):
    return dataclasses.asdict(mergebot.parse((MERGEBOT_FIXTURES / f"{name}.html").read_text()))


def _member(sets, repo, number):
    return next(m for s in sets for m in s["members"] if (m["repo"], m["num"]) == (repo, number))


def test_branch_sets_group_by_head_branch_across_repos():
    ec, cr = "master-l10n_ec-drop-account_edi-6396725-andg", "master-l10n_cr-base-6439180-andg"
    rows = [
        _mine_row("odoo/odoo-ls", 658, "alpha-xmlid-andg"),
        _mine_row("odoo/odoo", 292635, cr), _mine_row("odoo/odoo", 290109, ec),
        _mine_row("odoo/upgrade", 11485, cr, updatedAt="2026-10-04T00:00:00Z"),
        _mine_row("odoo/enterprise", 132695, ec),
        _mine_row("odoo/upgrade", 11389, ec, updatedAt="2026-10-05T00:00:00Z"),
    ]
    sets = _sets(rows, {})
    assert [(s["key"], s["task"], [(m["repo"], m["num"]) for m in s["members"]]) for s in sets] == [
        (ec, "6396725",
         [("odoo/odoo", 290109), ("odoo/enterprise", 132695), ("odoo/upgrade", 11389)]),
        (cr, "6439180", [("odoo/odoo", 292635), ("odoo/upgrade", 11485)]),
        ("alpha-xmlid-andg", None, [("odoo/odoo-ls", 658)]),
    ]
    assert sets[0]["title"] == "PR 290109"
    member = sets[0]["members"][0]
    assert (member["requested_people"], member["requested_teams"]) == (
        ["clbr-odoo"], ["rd-accounting"])


def test_override_greens_ci_but_an_unlisted_red_check_stays_red():
    branch = "master-x-1234567-andg"
    rows = [
        _mine_row("odoo/odoo", 290109, branch, reviewDecision="APPROVED",
                  checks=[("ci/style", "ERROR"), ("ci/runbot", "SUCCESS")]),
        _mine_row("odoo/upgrade", 11485, branch,
                  checks=[("upgradeci/matt", "ERROR"), ("ci/runbot", "SUCCESS")]),
    ]
    upgrade_page = mergebot.MergebotState(
        "blocked", checks=[mergebot.Check("ci/runbot", "ok", "", False, None)], r_plus=False)
    sets = _sets(rows, {
        "odoo/odoo#290109": _page("odoo_odoo_290109_blocked_linked"),
        "odoo/upgrade#11485": dataclasses.asdict(upgrade_page),
    })
    odoo = _member(sets, "odoo/odoo", 290109)
    assert (odoo["ci"], odoo["ci_failing"], odoo["override"]) == (
        "green", [], [{"check": "ci/style", "by": "kmagusiak"}])
    # A GitHub approval is not an r+, the two are shown apart.
    assert (odoo["decision"], odoo["r_plus"], odoo["mergebot_unknown"]) == (
        "APPROVED", False, False)
    upgrade = _member(sets, "odoo/upgrade", 11485)
    assert (upgrade["ci"], upgrade["ci_failing"]) == ("red", ["upgradeci/matt"])
    # The Overridden check raises nothing, and linked PRs missing only an r+ wait on a reviewer.
    [s] = sets
    assert (s["band"], _actions(s)) == (
        "needs", [("upgrade#11485", "ci", "CI red: upgradeci/matt")])


def test_lazy_page_check_is_pending_not_red():
    pages = {"odoo/odoo#291953": _page("odoo_odoo_291953_missing_statuses")}
    sets = _sets([_mine_row("odoo/odoo", 291953, "b")], pages)
    assert _member(sets, "odoo/odoo", 291953)["ci"] == "pending"


def test_merged_vs_closed_and_done_only_when_every_member_resolved():
    rows = [
        _mine_row("odoo/odoo", 290657, "b", state="CLOSED"),
        _mine_row("odoo/odoo", 255698, "b", state="CLOSED"),
        _mine_row("odoo/enterprise", 1, "b"),
    ]
    pages = {"odoo/odoo#290657": _page("odoo_odoo_290657_merged"),
             "odoo/odoo#255698": _page("odoo_odoo_255698_closed")}
    [open_set] = _sets(rows, pages)
    assert [m["state"] for m in open_set["members"]] == ["CLOSED", "MERGED", "OPEN"]
    assert open_set["band"] == "open"
    [done] = _sets(rows[:2], pages)
    assert done["band"] == "done"


def test_unmanaged_repo_trusts_github_and_unknown_falls_back_flagged():
    rows = [
        _mine_row("odoo/odoo-ls", 658, "a", mergeable="CONFLICTING"),
        _mine_row("odoo/odoo", 269608, "b", checks=[("ci/style", "ERROR")]),
    ]
    sets = _sets(rows, {
        "odoo/odoo-ls#658": dataclasses.asdict(mergebot.MergebotState("unmanaged")),
        "odoo/odoo#269608": dataclasses.asdict(mergebot.MergebotState("unknown")),
    })
    ls = _member(sets, "odoo/odoo-ls", 658)
    assert (ls["conflict"], ls["ci"], ls["r_plus"], ls["mergebot_unknown"]) == (
        True, "green", None, False)
    odoo = _member(sets, "odoo/odoo", 269608)
    assert (odoo["ci"], odoo["r_plus"], odoo["mergebot_unknown"]) == ("red", None, True)


def test_thread_lifts_when_someone_else_spoke_last_and_a_draft_never_lifts():
    rows = [_mine_row("odoo/odoo", 1, "a"),
            _mine_row("odoo/odoo", 2, "b", isDraft=True, checks=[("ci/style", "ERROR")])]
    streams = {
        "odoo/odoo#1": [
            _entry("thread", "clbr-odoo", "2026-09-10T00:00:00Z", state="UNRESOLVED", thread="t1"),
            _entry("thread", "andg", "2026-09-11T00:00:00Z", state="UNRESOLVED", thread="t1"),
            _entry("thread", "andg", "2026-09-12T00:00:00Z", state="UNRESOLVED", thread="t2"),
            _entry("thread", "odoo-pda", "2026-09-13T00:00:00Z", state="UNRESOLVED",
                   thread="t2", path="x.py"),
            _entry("thread", "odoo-pda", "2026-09-14T00:00:00Z", state="RESOLVED", thread="t3"),
        ],
        "odoo/odoo#2": [
            _entry("thread", "odoo-pda", "2026-09-13T00:00:00Z", state="UNRESOLVED", thread="t4"),
        ],
    }
    lifted, draft = _sets(rows, {}, streams=streams)
    assert (lifted["key"], lifted["band"], _actions(lifted)) == (
        "a", "needs", [("odoo#1", "thread", "odoo-pda is waiting in a thread on x.py")])
    assert lifted["actions"][0]["since"] == "2026-09-13T00:00:00Z"
    assert (draft["key"], draft["band"], draft["actions"]) == ("b", "open", [])


def test_the_list_folds_one_authors_waiting_threads_into_one_line():
    stream = [_entry("thread", "clbr-odoo", f"2026-09-1{i}T00:00:00Z", state="UNRESOLVED",
                     thread=f"t{i}", path=path)
              for i, path in enumerate(["a.py", "b.py", "a.py"])]
    stream.append(_entry("thread", "jbw-odoo", "2026-09-19T00:00:00Z", state="UNRESOLVED",
                         thread="t9", path="c.py"))
    [s] = _sets([_mine_row("odoo/odoo", 1, "a")], {}, streams={"odoo/odoo#1": stream})
    assert len(s["actions"]) == 4
    assert [(a["text"], a["since"]) for a in s["action_lines"]] == [
        ("clbr-odoo is waiting in 3 threads on 2 files", "2026-09-10T00:00:00Z"),
        ("jbw-odoo is waiting in a thread on c.py", "2026-09-19T00:00:00Z")]


def test_changes_requested_lift_until_a_push_then_wait_on_re_review():
    stream = [
        _entry("review", "jbw-odoo", "2026-10-01T00:00:00Z", state="CHANGES_REQUESTED"),
        _entry("review", "jbw-odoo", "2026-10-01T12:00:00Z", state="APPROVED"),
        _entry("review", "jco-odoo", "2026-10-03T00:00:00Z", state="CHANGES_REQUESTED"),
    ]
    streams = {"odoo/odoo#1": stream}
    [before] = _sets([_mine_row("odoo/odoo", 1, "a")], {}, streams=streams)
    assert (before["band"], _actions(before), before["fyi"]) == (
        "needs", [("odoo#1", "changes", "changes requested by jco-odoo")], [])
    pushed = _mine_row("odoo/odoo", 1, "a", pushed="2026-10-04T00:00:00Z")
    [after] = _sets([pushed], {}, streams=streams)
    assert (after["band"], after["actions"], after["fyi"]) == (
        "open", [], ["waiting on re-review"])


def test_teams_never_count_as_a_reviewer_and_needs_you_is_oldest_first():
    removed = {"nodes": [{"__typename": "ReviewRequestRemovedEvent",
                          "createdAt": "2026-09-20T00:00:00Z",
                          "requestedReviewer": {"__typename": "User", "login": "svs-odoo"}}]}
    teams = {"nodes": [{"requestedReviewer": {"__typename": "Team", "slug": "rd-accounting"}}]}
    rows = [
        _mine_row("odoo/odoo", 1, "teams", reviewRequests=teams, timelineItems=removed,
                  updatedAt="2026-10-04T00:00:00Z"),
        _mine_row("odoo/odoo", 2, "nobody", reviewRequests={"nodes": []}),
        _mine_row("odoo/odoo", 3, "person"),
    ]
    sets = _sets(rows, {})
    assert [(s["key"], s["band"], _actions(s), [a["since"] for a in s["actions"]])
            for s in sets] == [
        ("nobody", "needs", [("odoo#2", "reviewers", "no reviewer requested")],
         ["2026-09-01T00:00:00Z"]),
        ("teams", "needs", [("odoo#1", "reviewers", "only teams requested")],
         ["2026-09-20T00:00:00Z"]),
        ("person", "open", [], []),
    ]


def test_linked_pr_lifts_only_from_outside_the_set_and_not_for_a_missing_r_plus():
    pages = {"odoo/odoo#291953": _page("odoo_odoo_291953_missing_statuses")}
    [alone] = _sets([_mine_row("odoo/odoo", 291953, "b")], pages)
    assert _actions(alone) == [("odoo#291953", "linked",
                                "linked odoo/enterprise#133776: missing statuses, missing r+")]
    rows = [_mine_row("odoo/odoo", 291953, "b"), _mine_row("odoo/enterprise", 133776, "b")]
    [together] = _sets(rows, pages)
    assert (together["band"], together["actions"]) == ("open", [])


def test_idle_label_once_quiet_more_than_seven_days():
    rows = [_mine_row("odoo/odoo", 1, "a")]
    assert _sets(rows, {}, now="2026-10-09T00:00:00Z")[0]["fyi"] == ["idle 8d"]
    assert _sets(rows, {}, now="2026-10-08T00:00:00Z")[0]["fyi"] == []


def test_acknowledge_holds_until_a_push_a_comment_or_a_ci_change():
    red = [("ci/style", "ERROR")]
    row = _mine_row("odoo/odoo", 1, "a", checks=red, headRefOid="s1")
    [s] = _sets([row], {})
    acks = {"a": s["fingerprint"]}
    [acked] = _sets([row], {}, acks=acks)
    assert (acked["band"], acked["acknowledged"], len(acked["actions"])) == ("open", True, 1)
    changed = [
        ([_mine_row("odoo/odoo", 1, "a", checks=red, headRefOid="s2")], {}),
        ([row], {"odoo/odoo#1": [_entry("issue", "clbr-odoo", "2026-10-04T00:00:00Z")]}),
        ([_mine_row("odoo/odoo", 1, "a", headRefOid="s1",
                    checks=[*red, ("ci/runbot", "FAILURE")])], {}),
    ]
    for rows, streams in changed:
        [back] = _sets(rows, {}, acks=acks, streams=streams)
        assert (back["band"], back["acknowledged"]) == ("needs", False)


_DASHBOARD = ("[![Pull request status dashboard](https://mergebot.odoo.com/odoo/odoo/pull/1.png)]"
              "(https://mergebot.odoo.com/odoo/odoo/pull/1)")
_FW_CHAIN = ("This PR targets master and is part of the forward-port chain. Further PRs will be"
             " created up to master.\n\nMore info at https://github.com/odoo/odoo/wiki/Mergebot")


def test_fyi_labels_are_movement_since_the_last_look():
    events = {"nodes": [
        {"__typename": "ReviewRequestedEvent", "createdAt": "2026-10-04T00:00:00Z",
         "requestedReviewer": {"__typename": "User", "login": "jbw-odoo"}},
        {"__typename": "ReviewRequestRemovedEvent", "createdAt": "2026-10-04T00:00:00Z",
         "requestedReviewer": {"__typename": "User", "login": "svs-odoo"}},
        {"__typename": "ReviewRequestedEvent", "createdAt": "2026-10-04T00:00:00Z",
         "requestedReviewer": {"__typename": "Team", "slug": "rd-accounting"}},
    ]}
    rows = [_mine_row("odoo/odoo", 1, "a", timelineItems=events)]
    streams = {"odoo/odoo#1": [
        _entry("issue", "clbr-odoo", "2026-10-02T00:00:00Z"),
        _entry("review", "jco-odoo", "2026-10-04T00:00:00Z", state="APPROVED"),
        _entry("issue", "andg", "2026-10-04T00:00:00Z"),
        _entry("issue", "robodoo", "2026-10-04T00:00:00Z", body=_DASHBOARD),
        _entry("issue", "clbr-odoo", "2026-10-04T01:00:00Z"),
    ]}
    seen = {"odoo/odoo#1": {"fetched_at": "2026-10-03T00:00:00+00:00", "r_plus": 0}}

    def fyi(r_plus, seen):
        page = dataclasses.asdict(mergebot.MergebotState("blocked", r_plus=r_plus))
        return _sets(rows, {"odoo/odoo#1": page}, streams=streams, seen=seen)[0]["fyi"]

    assert fyi(False, seen) == ["approved · r+ missing", "new reply",
                                "reviewer added: jbw-odoo", "reviewer removed: svs-odoo"]
    assert fyi(True, seen) == ["approved", "new reply", "r+",
                               "reviewer added: jbw-odoo", "reviewer removed: svs-odoo"]
    # Nothing to compare against before the first interactive look.
    assert fyi(True, {}) == []


def test_a_review_since_the_last_push_stands_in_for_a_dropped_request():
    rows = [_mine_row("odoo/odoo", 1, "a", reviewRequests={"nodes": []})]
    after = [_entry("review", "jco-odoo", "2026-10-03T00:00:00Z", state="APPROVED")]
    assert _sets(rows, {}, streams={"odoo/odoo#1": after})[0]["actions"] == []
    # Neither a plain comment nor the user's own review since the push counts.
    before = [
        _entry("review", "jco-odoo", "2026-10-01T00:00:00Z", state="APPROVED"),
        _entry("issue", "clbr-odoo", "2026-10-03T00:00:00Z"),
        _entry("review", "andg", "2026-10-03T00:00:00Z", state="COMMENTED"),
    ]
    [s] = _sets(rows, {}, streams={"odoo/odoo#1": before})
    assert _actions(s) == [("odoo#1", "reviewers", "no reviewer requested")]


def _fw(number, base, **over):
    return _mine_row("odoo/odoo", number, f"{base}-saas-19.1-arca-6470810-andg-5729-fw",
                     source="odoo/odoo#290657", baseRefName=base, **over)


_SOURCE = _mine_row("odoo/odoo", 290657, "saas-19.1-arca-6470810-andg", state="CLOSED")


def test_chain_runs_in_odoo_branch_order_and_is_done_only_when_every_forward_port_is():
    merged = _page("odoo_odoo_290657_merged")
    pages = {pr: merged for pr in ("odoo/odoo#290657", "odoo/odoo#291580", "odoo/odoo#291857")}
    chain = [_fw(291981, "master"), _fw(291857, "20.0", state="CLOSED"),
             _fw(291580, "saas-19.2", state="CLOSED")]
    [s] = _sets([_SOURCE, *chain], pages)
    assert [(f["base"], f["ref"], f["state"], f["flag"]) for f in s["members"][0]["fw"]] == [
        ("saas-19.2", "odoo#291580", "MERGED", None), ("20.0", "odoo#291857", "MERGED", None),
        ("master", "odoo#291981", "OPEN", None)]
    # A healthy open Forward-port keeps the row Open and raises nothing.
    assert (s["band"], s["actions"], s["fyi"]) == ("open", [], ["source merged", "fw 2/3 merged"])
    pages["odoo/odoo#291981"] = merged
    [done] = _sets([_SOURCE, _fw(291981, "master", state="CLOSED"), *chain[1:]], pages)
    assert (done["band"], done["fyi"]) == ("done", ["fw 3/3 merged"])


def test_conflicted_or_red_forward_port_lifts_its_source():
    rows = [_SOURCE, _fw(291981, "master", checks=[("ci/runbot", "FAILURE")]),
            _fw(291857, "20.0", mergeable="CONFLICTING")]
    [s] = _sets(rows, {"odoo/odoo#290657": _page("odoo_odoo_290657_merged")})
    assert [(f["base"], f["flag"]) for f in s["members"][0]["fw"]] == [
        ("20.0", "conflict"), ("master", "red")]
    assert (s["band"], _actions(s)) == ("needs", [
        ("odoo#291857", "fw", "forward-port to 20.0: merge conflict"),
        ("odoo#291981", "fw", "forward-port to master: CI red: ci/runbot")])


def test_human_comment_on_a_forward_port_lifts_until_the_user_answers():
    rows = [_SOURCE, _fw(291981, "master")]
    pages = {"odoo/odoo#290657": _page("odoo_odoo_290657_merged")}
    stream = [_entry("issue", "fw-bot", "2026-10-03T00:00:00Z", body=_FW_CHAIN),
              _entry("issue", "clbr-odoo", "2026-10-04T00:00:00Z")]
    [s] = _sets(rows, pages, streams={"odoo/odoo#291981": stream})
    assert (s["band"], _actions(s), s["actions"][0]["since"]) == (
        "needs", [("odoo#291981", "fw", "forward-port to master: clbr-odoo commented")],
        "2026-10-04T00:00:00Z")
    answered = [*stream, _entry("issue", "andg", "2026-10-04T01:00:00Z"),
                _entry("issue", "fw-bot", "2026-10-04T02:00:00Z", body=_FW_CHAIN)]
    [s] = _sets(rows, pages, streams={"odoo/odoo#291981": answered})
    assert (s["band"], s["actions"]) == ("open", [])


# Real robodoo and fw-bot bodies from the user's PRs, the user's login shortened to andg.
_BOT_ACTIONS = [
    ("robodoo", ("@andg @jorenvo staging failed: ci/runbot (view more at "
                 "https://runbot.odoo.com/runbot/batch/2698708/build/121407522)"),
     "robodoo: staging failed: ci/runbot"),
    ("robodoo", "@andg @clbr-odoo 'ci/runbot' failed on this reviewed PR.",
     "robodoo: 'ci/runbot' failed on this reviewed PR"),
    ("robodoo", "@jorenvo you may want to rebuild or fix this PR as it has failed CI.",
     "robodoo: you may want to rebuild or fix this PR as it has failed CI"),
    ("robodoo", "@andg @william-andre unable to stage: merge conflict",
     "robodoo: unable to stage: merge conflict"),
    ("robodoo", ("@andg @clbr-odoo because this PR has multiple commits, I need to know how to "
                 "merge it:\n\n* `merge` to merge directly, using the PR as merge commit message"),
     "robodoo: because this PR has multiple commits, I need to know how to merge it"),
    ("fw-bot", ("@andg @william-andre cherrypicking of pull request odoo/enterprise#37939 failed."
                "\n\nstdout:\n```\nCONFLICT (content): Merge conflict in account/x.py\n```"),
     "fw-bot: cherrypicking of pull request odoo/enterprise#37939 failed"),
    ("fw-bot", ("@andg @william-andre the next pull request (odoo/enterprise#38761) is in "
                "conflict. You can merge the chain up to here by saying\n> @fw-bot r+\n"),
     "fw-bot: the next pull request (odoo/enterprise#38761) is in conflict"),
    ("fw-bot", ("@andg @william-andre while this was properly forward-ported, at least one "
                "co-dependent PR (odoo/enterprise#38761) did not succeed. You will need to fix "
                "it before this can be merged."),
     ("fw-bot: while this was properly forward-ported, at least one co-dependent PR "
      "(odoo/enterprise#38761) did not succeed")),
    ("fw-bot", ("@xavierbol there is no branch 'saas~15.2', it can't be used as a forward port "
                "target."),
     "fw-bot: there is no branch 'saas~15.2', it can't be used as a forward port target"),
]
_BOT_FYI = [
    ("robodoo", _DASHBOARD, None),
    ("robodoo", ("@andg @jorenvo linked pull request(s) odoo/upgrade#10698 not ready. Linked PRs "
                 "are not staged until all of them are ready."), None),
    ("robodoo", ("Currently available commands for @andg:\n\n|command||\n|-|-|\n|`help`|displays "
                 "this help|\n|`r(eview)-`|removes approval of a previously approved PR|"), None),
    ("fw-bot", _FW_CHAIN, None),
    ("robodoo", "Merge method set to rebase and fast-forward.", "merge method set"),
    ("robodoo", "Forward-porting to 'saas-18.2'.", "forward-porting to saas-18.2"),
    ("robodoo", "Starting forward-port. Not waiting for merge to create followup forward-ports.",
     "forward-port started"),
    ("robodoo", "Disabled forward-porting.", "forward-porting disabled"),
    ("fw-bot", ("@andg @william-andre this pull request has forward-port PRs awaiting action (not "
                "merged or closed):\nodoo/enterprise#38753\n- odoo/enterprise#38761"),
     "forward-ports awaiting action"),
    ("fw-bot", ("child PR odoo/enterprise#119039 has become a normal PR because head updated from "
                "98fe2e68f5b3ac5fb95ebdd518ebcddbfbdc0913 to "
                "69d2fadc709d20cfbb90b4724e6a957c347d80a4. This PR (and any of its parents) will "
                "need to be merged independently as approvals won't cross."),
     "forward-port detached"),
]


def _bot_set(stream, *, pages=None, **over):
    row = _mine_row("odoo/odoo", 1, "a", checks=(), **over)
    seen = {"odoo/odoo#1": {"fetched_at": "2026-10-02T12:00:00+00:00", "r_plus": 0}}
    [s] = _sets([row], pages or {}, streams={"odoo/odoo#1": stream}, seen=seen)
    return s


def test_bot_failures_lift_and_the_other_bot_templates_are_fyi_at_most():
    for bot, body, text in _BOT_ACTIONS:
        s = _bot_set([_entry("issue", bot, "2026-10-03T00:00:00Z", body=body)])
        assert (s["band"], _actions(s), s["fyi"]) == ("needs", [("odoo#1", "bot", text)], []), body
    for bot, body, label in _BOT_FYI:
        s = _bot_set([_entry("issue", bot, "2026-10-03T00:00:00Z", body=body)])
        assert (s["band"], s["fyi"]) == ("open", [label] if label else []), body


def test_a_rejected_command_lifts_only_when_the_user_sent_it():
    def rejected(commander, reply):
        return _bot_set([
            _entry("issue", commander, "2026-10-03T00:00:00Z", body="@robodoo r+"),
            _entry("issue", "robodoo", "2026-10-03T00:01:00Z", body=reply)])

    mine = rejected("andg", "I'm sorry, @andg: you can't review+.")
    assert (mine["band"], _actions(mine)) == ("needs", [
        ("odoo#1", "command", 'robodoo rejected "@robodoo r+": you can\'t review+')])
    theirs = rejected("jorenvo", "I'm sorry, @jorenvo. I'm afraid I can't do that.")
    assert (theirs["band"], theirs["fyi"]) == (
        "open", ["new reply", "robodoo rejected jorenvo's command"])


def test_a_bot_failure_clears_on_a_push_or_once_the_page_moved_on():
    failed = [_entry("issue", "robodoo", "2026-10-03T00:00:00Z", body=_BOT_ACTIONS[0][1])]
    assert _bot_set(failed)["band"] == "needs"
    assert _bot_set(failed, pushed="2026-10-04T00:00:00Z")["actions"] == []
    for state in ("error", "ready"):
        page = dataclasses.asdict(mergebot.MergebotState(state, merge_method=True))
        assert bool(_bot_set(failed, pages={"odoo/odoo#1": page})["actions"]) is (state == "error")


def test_a_ci_failure_comment_on_an_overridden_check_raises_nothing():
    stream = [_entry("issue", "robodoo", "2026-10-03T00:00:00Z", body=body)
              for _, body, _ in _BOT_ACTIONS[1:3]]
    row = _mine_row("odoo/odoo", 290109, "b", checks=[("ci/style", "ERROR")])
    [s] = _sets([row], {"odoo/odoo#290109": _page("odoo_odoo_290109_blocked_linked")},
                streams={"odoo/odoo#290109": stream})
    assert (s["band"], s["actions"]) == ("open", [])


def test_a_cherry_pick_failure_on_a_forward_port_lifts_its_source():
    rows = [_SOURCE, _fw(291981, "master")]
    body = ("cherrypicking of pull request odoo/odoo#290657 failed.\n\nstdout:\n```\n"
            "CONFLICT (content): Merge conflict in addons/account/models/account_move.py\n```")
    [s] = _sets(rows, {"odoo/odoo#290657": _page("odoo_odoo_290657_merged")}, streams={
        "odoo/odoo#291981": [_entry("issue", "fw-bot", "2026-10-03T00:00:00Z", body=body)]})
    assert (s["band"], _actions(s)) == ("needs", [(
        "odoo#291981", "fw",
        "forward-port to master: fw-bot: cherrypicking of pull request odoo/odoo#290657 failed")])
