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
    assert derive.parse_linked_task("OPW-22") is None  # too short
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
