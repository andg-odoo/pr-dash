from pr_dash import derive


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


def test_detect_pairs():
    prs = [
        {"id": "odoo/odoo#1", "author": "jdoe", "head_branch": "feature-x"},
        {"id": "odoo/enterprise#2", "author": "jdoe", "head_branch": "feature-x"},
        {"id": "odoo/odoo#3", "author": "asmith", "head_branch": "other"},
    ]
    pairs = derive.detect_pairs(prs)
    assert pairs == {"odoo/odoo#1": "odoo/enterprise#2", "odoo/enterprise#2": "odoo/odoo#1"}


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
