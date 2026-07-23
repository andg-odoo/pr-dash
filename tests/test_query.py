from __future__ import annotations

import importlib.util

import pytest

from pr_dash import query


# --- item builders -----------------------------------------------------------

def _member(repo, number, *, closed=False, reviewed=False):
    return {
        "repo": repo,
        "repo_short": repo.split("/")[-1],
        "number": number,
        "url": f"https://github.com/{repo}/pull/{number}",
        "head_sha": f"sha{number}",
        "title": "t",
        "closed": closed,
        "reviewed": reviewed,
    }


def _diff_entry(repo, number, diff, *, review_changed_paths=None,
                truncated=False, available=True):
    return {
        "repo": repo,
        "repo_short": repo.split("/")[-1],
        "number": number,
        "url": f"https://github.com/{repo}/pull/{number}",
        "closed": False,
        "reviewed": False,
        "available": available,
        "truncated": truncated,
        "diff": diff,
        "review_changed_paths": review_changed_paths,
        "additions": 1,
        "deletions": 0,
        "changed_files": (diff.count("diff --git ") if diff else 0),
    }


def _item(pr_id, *, members=None, diffs=None, is_pair=False, **overrides):
    repo, num = pr_id.split("#")
    num = int(num)
    members = members or [_member(repo, num)]
    item = {
        "id": pr_id,
        "is_pair": is_pair,
        "members": members,
        "title": "Some PR",
        "author": "alice",
        "is_draft": False,
        "body": "the body text",
        "target_branch": "18.0",
        "head_branch": "feat",
        "url": f"https://github.com/{repo}/pull/{num}",
        "repo": repo,
        "repo_short": repo.split("/")[-1],
        "number": num,
        "additions": 10,
        "deletions": 2,
        "changed_files": 1,
        "modules": ["sale"],
        "installable": ["sale"],
        "mergeable": "MERGEABLE",
        "ci_state": "SUCCESS",
        "ci_failures": [],
        "runbot_url": None,
        "linked_task": None,
        "linked_task_kind": None,
        "linked_task_label": None,
        "task_url": None,
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-02T00:00:00+00:00",
        "review_requested_at": "2026-07-01T00:00:00+00:00",
        "age_days": 3,
        "req_age_days": 3,
        "previously_reviewed": False,
        "awaiting_my_reply": False,
        "unresolved_threads": 0,
        "since_last_look": [],
        "flags": [],
        "bucket": "M",
        "bucket_score": 5,
        "reviewers": [],
        "my_review_state": "PENDING",
        "other_reviewers": [],
        "threads": [],
        "commands": [{"label": "test", "command": "otest"}],
        "diffs": diffs if diffs is not None else [_diff_entry(repo, num, _DIFF_TWO)],
        "state": "OPEN",
        "is_archived": False,
        "archived_at": None,
        "ai_reviews": [],
        "ai_review_verdict": None,
    }
    item.update(overrides)
    return item


_DIFF_A = (
    "diff --git a/sale/models/a.py b/sale/models/a.py\n"
    "index 111..222 100644\n"
    "--- a/sale/models/a.py\n"
    "+++ b/sale/models/a.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-old a\n"
    "+new a\n"
)
_DIFF_B = (
    "diff --git a/sale/models/b.py b/sale/models/b.py\n"
    "index 333..444 100644\n"
    "--- a/sale/models/b.py\n"
    "+++ b/sale/models/b.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-old b\n"
    "+new b\n"
)
_DIFF_TWO = _DIFF_A + _DIFF_B


# --- resolve_item ------------------------------------------------------------

def test_resolve_full_id():
    items = [_item("odoo/odoo#100"), _item("odoo/enterprise#200")]
    assert query.resolve_item(items, "odoo/odoo#100")["id"] == "odoo/odoo#100"


def test_resolve_short_ref():
    items = [_item("odoo/odoo#100"), _item("odoo/enterprise#200")]
    assert query.resolve_item(items, "enterprise#200")["id"] == "odoo/enterprise#200"


def test_resolve_bare_number_and_int():
    items = [_item("odoo/odoo#100"), _item("odoo/enterprise#200")]
    assert query.resolve_item(items, "100")["id"] == "odoo/odoo#100"
    assert query.resolve_item(items, 200)["id"] == "odoo/enterprise#200"


def test_resolve_url():
    items = [_item("odoo/odoo#100")]
    url = "https://github.com/odoo/odoo/pull/100"
    assert query.resolve_item(items, url)["id"] == "odoo/odoo#100"


def test_resolve_pair_member_resolves_to_pair_item():
    # A pair item keyed on the odoo id; the enterprise number must resolve to it.
    pair = _item(
        "odoo/odoo#100",
        is_pair=True,
        members=[_member("odoo/odoo", 100), _member("odoo/enterprise", 999)],
    )
    items = [pair, _item("odoo/odoo#101")]
    assert query.resolve_item(items, "enterprise#999")["id"] == "odoo/odoo#100"
    assert query.resolve_item(items, 999)["id"] == "odoo/odoo#100"


def test_resolve_ambiguous_bare_number():
    # Same number in two unrelated repos -> bare number is ambiguous.
    items = [_item("odoo/odoo#100"), _item("odoo/enterprise#100")]
    with pytest.raises(ValueError, match="Ambiguous"):
        query.resolve_item(items, "100")
    # Qualifying by repo disambiguates.
    assert query.resolve_item(items, "enterprise#100")["id"] == "odoo/enterprise#100"


def test_resolve_not_found_lists_candidates():
    items = [_item("odoo/odoo#100")]
    with pytest.raises(ValueError, match="odoo/odoo#100"):
        query.resolve_item(items, "odoo/odoo#404")


# --- split_diff --------------------------------------------------------------

def test_split_diff_splits_on_headers():
    chunks = query.split_diff(_DIFF_TWO)
    assert list(chunks.keys()) == ["sale/models/a.py", "sale/models/b.py"]
    assert chunks["sale/models/a.py"] == _DIFF_A
    assert chunks["sale/models/b.py"] == _DIFF_B
    # Concatenating chunks reconstructs the original diff exactly.
    assert "".join(chunks.values()) == _DIFF_TWO


def test_split_diff_empty():
    assert query.split_diff(None) == {}
    assert query.split_diff("") == {}


# --- get_diff_text -----------------------------------------------------------

def test_get_diff_text_all_files():
    item = _item("odoo/odoo#100")
    out = query.get_diff_text(item)
    assert out["id"] == "odoo/odoo#100"
    m = out["members"][0]
    assert m["files_returned"] == ["sale/models/a.py", "sale/models/b.py"]
    assert m["omitted_files"] == []
    assert m["diff"] == _DIFF_TWO


def test_get_diff_text_file_filter_basename():
    item = _item("odoo/odoo#100")
    out = query.get_diff_text(item, files=["b.py"])
    m = out["members"][0]
    assert m["files_returned"] == ["sale/models/b.py"]
    assert m["diff"] == _DIFF_B


def test_file_wanted_segment_boundary():
    # Suffix matches must respect path-segment boundaries: "b.py" is a
    # basename, not a substring, so it must not select "ab.py".
    assert query._file_wanted("sale/models/ab.py", ["b.py"]) is False
    assert query._file_wanted("sale/models/b.py", ["b.py"]) is True
    assert query._file_wanted("sale/models/b.py", ["models/b.py"]) is True


def test_get_diff_text_changed_since_review_only():
    diffs = [_diff_entry("odoo/odoo", 100, _DIFF_TWO,
                         review_changed_paths=["sale/models/b.py"])]
    item = _item("odoo/odoo#100", diffs=diffs)
    out = query.get_diff_text(item, changed_since_review_only=True)
    m = out["members"][0]
    assert m["files_returned"] == ["sale/models/b.py"]


def test_get_diff_text_changed_since_review_none_baseline_keeps_all():
    # review_changed_paths=None means no baseline -> no filtering.
    item = _item("odoo/odoo#100")
    out = query.get_diff_text(item, changed_since_review_only=True)
    assert out["members"][0]["files_returned"] == [
        "sale/models/a.py", "sale/models/b.py",
    ]


def test_get_diff_text_max_chars_drops_whole_files():
    item = _item("odoo/odoo#100")
    # Budget fits the first file but not the second.
    out = query.get_diff_text(item, max_chars=len(_DIFF_A) + 5)
    m = out["members"][0]
    assert m["files_returned"] == ["sale/models/a.py"]
    assert m["diff"] == _DIFF_A  # never a partial file
    assert [o["path"] for o in m["omitted_files"]] == ["sale/models/b.py"]
    assert m["omitted_files"][0]["chars"] == len(_DIFF_B)


def test_get_diff_text_budget_shared_across_members():
    diffs = [
        _diff_entry("odoo/odoo", 100, _DIFF_A),
        _diff_entry("odoo/enterprise", 200, _DIFF_B),
    ]
    item = _item("odoo/odoo#100", is_pair=True, diffs=diffs)
    # Only the first member's file fits.
    out = query.get_diff_text(item, max_chars=len(_DIFF_A) + 5)
    assert out["members"][0]["files_returned"] == ["sale/models/a.py"]
    assert out["members"][1]["files_returned"] == []
    assert [o["path"] for o in out["members"][1]["omitted_files"]] == ["sale/models/b.py"]


def test_get_diff_text_truncated_member():
    diffs = [_diff_entry("odoo/odoo", 100, None, truncated=True, available=False)]
    item = _item("odoo/odoo#100", diffs=diffs)
    out = query.get_diff_text(item)
    m = out["members"][0]
    assert m["truncated_in_cache"] is True
    assert m["diff"] == ""
    assert m["files_returned"] == []


# --- summarize / detail ------------------------------------------------------

def test_summarize_omits_heavy_fields():
    item = _item("odoo/odoo#100")
    s = query.summarize(item)
    for absent in ("body", "threads", "diffs", "commands", "reviewers"):
        assert absent not in s
    assert s["id"] == "odoo/odoo#100"
    assert s["members"] == [
        {"repo_short": "odoo", "number": 100, "closed": False, "reviewed": False},
    ]
    assert s["state"] == "OPEN"
    assert s["bucket"] == "M"


def test_detail_has_files_but_no_diff_text():
    item = _item("odoo/odoo#100")
    d = query.detail(item)
    assert d["body"] == "the body text"
    entry = d["diffs"][0]
    assert entry["files"] == ["sale/models/a.py", "sale/models/b.py"]
    assert "diff" not in entry
    # Original item is untouched (detail deep-copies).
    assert item["diffs"][0]["diff"] == _DIFF_TWO


# --- review_history ----------------------------------------------------------

def _archived(pr_id, *, author, modules, my_state, archived_at):
    return _item(pr_id, author=author, modules=modules, is_archived=True,
                 archived_at=archived_at, my_review_state=my_state, state="MERGED")


def test_review_history_filters_and_orders():
    items = [
        _archived("odoo/odoo#1", author="alice", modules=["sale"],
                  my_state="APPROVED", archived_at="2026-07-01T00:00:00+00:00"),
        _archived("odoo/odoo#2", author="bob", modules=["account"],
                  my_state="CHANGES_REQUESTED", archived_at="2026-07-05T00:00:00+00:00"),
        _archived("odoo/odoo#3", author="alice", modules=["sale", "stock"],
                  my_state="APPROVED", archived_at="2026-07-03T00:00:00+00:00"),
        _item("odoo/odoo#4"),  # pending, excluded
    ]
    # Newest archived first.
    ids = [r["id"] for r in query.review_history(items)]
    assert ids == ["odoo/odoo#2", "odoo/odoo#3", "odoo/odoo#1"]
    # Author filter.
    assert [r["id"] for r in query.review_history(items, author="alice")] == [
        "odoo/odoo#3", "odoo/odoo#1",
    ]
    # Module filter.
    assert [r["id"] for r in query.review_history(items, module="stock")] == ["odoo/odoo#3"]
    # Verdict filter.
    assert [r["id"] for r in query.review_history(items, verdict="CHANGES_REQUESTED")] == [
        "odoo/odoo#2",
    ]
    # Limit.
    assert len(query.review_history(items, limit=1)) == 1


# --- stats -------------------------------------------------------------------

def test_stats_counts():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = "2026-01-01T00:00:00+00:00"
    items = [
        _item("odoo/odoo#1", bucket="S", flags=["MSG", "CI!"]),
        _item("odoo/odoo#2", bucket="M", flags=["MSG"], is_draft=True),
        _archived("odoo/odoo#3", author="a", modules=["sale"],
                  my_state="APPROVED", archived_at=now),
        _archived("odoo/odoo#4", author="a", modules=["sale"],
                  my_state="CHANGES_REQUESTED", archived_at=old),
    ]
    st = query.stats(items)
    assert st["pending"]["total"] == 2
    assert st["pending"]["by_bucket"] == {"S": 1, "M": 1}
    assert st["pending"]["by_flag"] == {"MSG": 2, "CI!": 1}
    assert st["pending"]["drafts"] == 1
    assert st["archived"]["total"] == 2
    assert st["archived"]["by_my_review_state"] == {
        "APPROVED": 1, "CHANGES_REQUESTED": 1,
    }
    assert st["archived"]["by_state"] == {"MERGED": 2}
    assert st["archived"]["last_30_days"] == 1  # only the `now` one


# --- MCP import guard --------------------------------------------------------

@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp extra not installed",
)
def test_mcp_server_imports():
    from pr_dash import mcp_server

    assert mcp_server.mcp.name == "pr-dash"


# --- my_pending_review / get_comments ----------------------------------------

def test_summarize_includes_pending_review_flag():
    item = _item("odoo/odoo#1", my_pending_review=True, flags=["PEND!"])
    s = query.summarize(item)
    assert s["my_pending_review"] is True
    assert "PEND!" in s["flags"]


def _insert_pr_row(conn, pr_id):
    repo, number = pr_id.split("#")
    conn.execute(
        "INSERT INTO pr (id, repo, number, title, url, author, target_branch, "
        "head_branch, head_sha, created_at, updated_at, review_requested_at, "
        "previously_reviewed, additions, deletions, changed_files, fetched_at) "
        "VALUES (?, ?, ?, '', '', 'a', 'b', 'b', 'sha', 't', 't', 't', 0, 0, 0, 0, 't')",
        (pr_id, repo, int(number)),
    )


def test_get_comments_shape_and_bot_flag(tmp_path):
    from pr_dash import db
    from pr_dash.config import Config

    cfg = Config(github_login="me", repos={}, cache_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    _insert_pr_row(conn, "odoo/odoo#100")
    db.replace_threads(conn, "odoo/odoo#100", [
        {"thread_id": "T1", "is_resolved": 0, "i_participated": 1,
         "last_reply_at": "t", "last_reply_author": "alice"},
    ])
    db.replace_comments(conn, "odoo/odoo#100", [
        {"kind": "thread", "thread_id": "T1", "comment_id": "11", "author": "alice",
         "created_at": "2026-05-01", "body": "why?", "path": "sale/x.py",
         "state": None, "url": "https://c/11"},
        {"kind": "review", "thread_id": None, "comment_id": "R1", "author": "robodoo",
         "created_at": None, "body": "", "path": None, "state": "PENDING", "url": None},
        {"kind": "issue", "thread_id": None, "comment_id": "99", "author": "me",
         "created_at": "2026-05-02", "body": "ping", "path": None,
         "state": None, "url": "https://i/99"},
    ])
    conn.close()

    out = query.get_comments(cfg, _item("odoo/odoo#100"))
    assert out["id"] == "odoo/odoo#100"
    m = out["members"][0]

    th = m["threads"][0]
    assert th["is_resolved"] is False
    assert th["path"] == "sale/x.py"
    assert th["comments"][0]["author"] == "alice"
    assert th["comments"][0]["bot"] is False
    assert th["comments"][0]["body"] == "why?"

    review = m["reviews"][0]
    assert review["bot"] is True         # robodoo flagged
    assert review["pending"] is True     # PENDING draft
    assert review["state"] == "PENDING"

    assert m["conversation"][0]["author"] == "me"
    assert m["conversation"][0]["url"] == "https://i/99"


def test_summarize_exposes_ping_and_flag():
    # render._make_item adds the PING flag; summarize copies flags + ping fields.
    item = _item("odoo/odoo#1", is_archived=True, flags=["PING"],
                 ping_at="2026-07-02T00:00:00Z", ping_author="alice",
                 ping_snippet="ready for r+")
    s = query.summarize(item)
    assert "PING" in s["flags"]
    assert s["ping_at"] == "2026-07-02T00:00:00Z"
    assert s["ping_author"] == "alice"
    assert s["ping_snippet"] == "ready for r+"


def test_stats_counts_pinged_archived():
    archived_ping = _item("odoo/odoo#7", is_archived=True, state="OPEN",
                          my_review_state="APPROVED",
                          archived_at="2026-07-01T00:00:00+00:00",
                          ping_at="2026-07-02T00:00:00Z")
    archived_plain = _item("odoo/odoo#8", is_archived=True, state="MERGED",
                           my_review_state="APPROVED",
                           archived_at="2026-07-01T00:00:00+00:00")
    st = query.stats([archived_ping, archived_plain, _item("odoo/odoo#9")])
    assert st["archived"]["pinged"] == 1
