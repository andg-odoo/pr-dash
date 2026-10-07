from __future__ import annotations

import copy
import os
import re
from datetime import datetime, timedelta, timezone

from pr_dash import db, derive, render
from pr_dash.config import Config

# github.com/<owner>/<repo>/pull/<number>, tolerating query strings / anchors.
_URL_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")


def load_items(cfg: Config) -> list[dict]:
    """Build the dashboard's item list from the cache, read-only.

    Deliberately does *not* call render.commit_seen_baseline: an agent query is
    a peek, not a "look", so it must never consume the user's since-last-look
    baseline out from under the real dashboard render."""
    conn = db.connect(cfg.db_path)
    try:
        items, _ = render.build_payload(
            conn,
            cfg.github_login,
            cfg.thresholds.stale_review_days,
            ai_max_attempts=cfg.ai.max_attempts,
        )
    finally:
        conn.close()
    return items


def cache_fetched_at(cfg: Config) -> str | None:
    """Newest fetched_at in the cache, or None when empty. Read-only."""
    conn = db.connect(cfg.db_path)
    try:
        return db.max_fetched_at(conn)
    finally:
        conn.close()


# --- reference resolution ----------------------------------------------------

def _parse_ref(ref: str | int) -> tuple[str | None, str | None, int]:
    """Parse a PR reference into (repo_full, repo_short, number). Exactly one of
    repo_full / repo_short is set when the ref names a repo, both None for a bare
    number. Accepts: int/"12345", "odoo#12345", "odoo/odoo#12345", or a PR URL."""
    if isinstance(ref, int):
        return None, None, ref
    s = str(ref).strip()
    if not s:
        raise ValueError("empty PR reference")
    m = _URL_RE.search(s)
    if m:
        return f"{m.group(1)}/{m.group(2)}", None, int(m.group(3))
    if "#" in s:
        left, _, right = s.partition("#")
        if not right.isdigit():
            raise ValueError(f"invalid PR reference {ref!r} (expected 'repo#number')")
        num = int(right)
        if "/" in left:
            return left, None, num
        return None, left, num
    if s.isdigit():
        return None, None, int(s)
    raise ValueError(
        f"unrecognized PR reference {ref!r}; use '12345', 'odoo#12345', "
        "'odoo/odoo#12345', or a github PR URL"
    )


# --- diff handling -----------------------------------------------------------

def split_diff(patch_text: str | None) -> dict[str, str]:
    """Split a combined `.diff` into {b-side path: file chunk}. Chunks with no
    recognizable `diff --git` header (e.g. a leading preamble) are dropped."""
    out: dict[str, str] = {}
    for path, chunk in derive.iter_diff_files(patch_text or ""):
        if path is None:
            continue
        out[path] = chunk
    return out


def _file_wanted(path: str, wanted: list[str]) -> bool:
    # Suffix matches must land on a path-segment boundary so "b.py" can't
    # accidentally select "ab.py".
    base = os.path.basename(path)
    return any(path == f or path.endswith("/" + f) or base == f for f in wanted)


def get_diff_text(
    item: dict,
    files: list[str] | None = None,
    changed_since_review_only: bool = False,
    max_chars: int = 60_000,
) -> dict:
    """Return per-member diff text, whole files only, under a shared char budget.

    Filters to `files` (exact path or basename/suffix match) and, when
    changed_since_review_only is set, to each member's review_changed_paths (the
    files that differ from the last review). Enforces max_chars across the whole
    result by dropping whole-file chunks from the end into omitted_files - never
    half a file - so the caller can re-request the omitted ones individually."""
    members = []
    running = 0
    for d in item.get("diffs", []):
        chunks = split_diff(d.get("diff")) if d.get("diff") else {}
        if files:
            chunks = {p: c for p, c in chunks.items() if _file_wanted(p, files)}
        if changed_since_review_only and d.get("review_changed_paths") is not None:
            allowed = set(d["review_changed_paths"])
            chunks = {p: c for p, c in chunks.items() if p in allowed}

        included: list[str] = []
        returned: list[str] = []
        omitted: list[dict] = []
        for path, chunk in chunks.items():
            if running + len(chunk) <= max_chars:
                included.append(chunk)
                returned.append(path)
                running += len(chunk)
            else:
                omitted.append({"path": path, "chars": len(chunk)})

        members.append({
            "repo_short": d.get("repo_short"),
            "number": d.get("number"),
            "truncated_in_cache": bool(d.get("truncated")),
            "files_returned": returned,
            "omitted_files": omitted,
            "diff": "".join(included),
        })
    return {"id": item["id"], "members": members}


# --- projections -------------------------------------------------------------

_SUMMARY_KEYS = (
    "id", "url", "title", "author", "is_draft", "is_pair", "members", "target_branch", "modules",
    "additions", "deletions", "changed_files", "bucket", "flags", "ci_state", "mergeable",
    "unresolved_threads", "awaiting_my_reply", "my_pending_review", "my_review_state",
    "previously_reviewed", "req_age_days", "since_last_look", "ai_review_verdict",
    "linked_task_label", "is_archived", "state", "ping_at", "ping_author", "ping_snippet",
    "push_at", "push_sha",
    # On the compact row so "does this data move have a migration" needs no get_pr call.
    "companion",
)


def summarize(item: dict) -> dict:
    """Compact triage row: no body, Discussion or diffs."""
    row = {k: item.get(k) for k in _SUMMARY_KEYS}
    row["members"] = [
        {"repo_short": m.get("repo_short"), "number": m.get("number"),
         "closed": m.get("closed"), "reviewed": m.get("reviewed")}
        for m in item.get("members") or [item]
    ]
    return row


def _diff_meta(d: dict) -> dict:
    files = list(split_diff(d.get("diff")).keys()) if d.get("diff") else []
    return {
        "repo_short": d.get("repo_short"),
        "number": d.get("number"),
        "available": d.get("available"),
        "truncated": d.get("truncated"),
        "additions": d.get("additions"),
        "deletions": d.get("deletions"),
        "changed_files": d.get("changed_files"),
        "review_changed_paths": d.get("review_changed_paths"),
        "files": files,
    }


def detail(item: dict) -> dict:
    """Full item minus diff text: each diff entry is replaced by metadata plus a
    parsed `files` list. Body, Discussion, reviewers, ci_failures, ai_reviews
    and task links are kept."""
    out = copy.deepcopy(item)
    out["diffs"] = [_diff_meta(d) for d in item.get("diffs", [])]
    return out


def review_history(
    items: list[dict],
    author: str | None = None,
    module: str | None = None,
    verdict: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Archived (reviewed) items, filtered by author / module / verdict, newest
    archived_at first, as summarize rows. `verdict` matches my_review_state
    (APPROVED / CHANGES_REQUESTED / COMMENTED)."""
    rows = [it for it in items if it.get("is_archived")]
    if author:
        rows = [it for it in rows if it.get("author") == author]
    if module:
        rows = [it for it in rows if module in (it.get("modules") or [])]
    if verdict:
        rows = [it for it in rows if it.get("my_review_state") == verdict]
    rows.sort(key=lambda it: it.get("archived_at") or "", reverse=True)
    return [summarize(it) for it in rows[:limit]]


def stats(items: list[dict]) -> dict:
    """Counts over the pending queue and the archived history."""
    pending = [it for it in items if not it.get("is_archived")]
    archived = [it for it in items if it.get("is_archived")]

    by_bucket: dict[str, int] = {}
    by_flag: dict[str, int] = {}
    drafts = 0
    for it in pending:
        by_bucket[it.get("bucket") or "?"] = by_bucket.get(it.get("bucket") or "?", 0) + 1
        for f in it.get("flags") or []:
            by_flag[f] = by_flag.get(f, 0) + 1
        if it.get("is_draft"):
            drafts += 1

    by_review: dict[str, int] = {}
    by_state: dict[str, int] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    last_30 = 0
    pinged = 0
    pushed = 0
    for it in archived:
        state = it.get("my_review_state") or "PENDING"
        by_review[state] = by_review.get(state, 0) + 1
        by_state[it.get("state") or "OPEN"] = by_state.get(it.get("state") or "OPEN", 0) + 1
        if it.get("ping_at"):
            pinged += 1
        if it.get("push_at"):
            pushed += 1
        ts = it.get("archived_at")
        if ts:
            try:
                if derive.parse_iso(ts) >= cutoff:
                    last_30 += 1
            except (ValueError, TypeError):
                pass

    return {
        "pending": {
            "total": len(pending),
            "by_bucket": by_bucket,
            "by_flag": by_flag,
            "drafts": drafts,
        },
        "archived": {
            "total": len(archived),
            "by_my_review_state": by_review,
            "by_state": by_state,
            "last_30_days": last_30,
            "pinged": pinged,
            "pushed": pushed,
        },
    }


# --- authored PRs ------------------------------------------------------------

def load_mine(cfg: Config, *, include_dismissed: bool = False) -> list[dict]:
    """Build the Branch sets from the cache, read-only, so no seen baseline moves."""
    conn = db.connect(cfg.db_path)
    try:
        return render.build_mine_payload(conn, cfg.github_login,
                                         include_dismissed=include_dismissed)[0]
    finally:
        conn.close()


def summarize_mine(branch_set: dict) -> dict:
    """A Branch set without its Discussion, which mine_detail serves per member."""
    return {k: v for k, v in branch_set.items() if k != "discussion"}


def mine_detail(cfg: Config, branch_set: dict) -> dict:
    """A Branch set with each member's body and Discussion tree."""
    conn = db.connect(cfg.db_path)
    try:
        bodies = {r["id"]: r["body"] for r in db.list_mine(conn, include_dismissed=True)}
        comments = db.list_discussions(conn, "mine")
    finally:
        conn.close()
    members = []
    for m in branch_set["members"]:
        pr_id = f"{m['repo']}#{m['num']}"
        fw = [{**f, "discussion": derive.group_discussion(comments.get(f["id"], []))}
              for f in m["fw"]]
        members.append({**m, "fw": fw, "body": bodies.get(pr_id),
                        "discussion": derive.group_discussion(comments.get(pr_id, []))})
    return {**summarize_mine(branch_set), "members": members}


# --- tracked PRs -------------------------------------------------------------

def load_tracked(cfg: Config, *, include_dismissed: bool = False) -> list[dict]:
    """Build the tracked list from the cache, read-only.

    Like load_items, this deliberately skips commit_tab_seen_baseline: an
    agent peeking must not consume the since-last-look deltas the dashboard is
    about to show the user.
    """
    conn = db.connect(cfg.db_path)
    try:
        items, _ = render.build_tracked_payload(conn)
        if include_dismissed:
            # build_tracked_payload only returns undismissed rows; fold the rest
            # back in, flagged, rather than duplicating its shaping here.
            live = {it["id"] for it in items}
            for row in db.list_tracked(conn, include_dismissed=True):
                if row["id"] in live or row["dismissed_at"] is None:
                    continue
                items.append({
                    "id": row["id"], "repo": row["repo"],
                    "repo_short": row["repo"].split("/")[-1],
                    "number": row["number"], "url": row["url"],
                    "title": row["title"], "author": row["author"],
                    "state": row["state"], "is_draft": bool(row["is_draft"]),
                    "target_branch": row["target_branch"],
                    "ci_state": row["ci_state"], "body": row["body"],
                    "comment_count": row["comment_count"] or 0,
                    "review_count": row["review_count"] or 0,
                    "thread_count": row["thread_count"] or 0,
                    "activity_count": row["activity_count"] or 0,
                    "unresolved_threads": row["unresolved_threads"] or 0,
                    "discussion": [], "source": row["source"],
                    "added_at": row["added_at"], "updated_at": row["updated_at"],
                    "merged_at": row["merged_at"], "closed_at": row["closed_at"],
                    "age_days": 0, "idle_days": 0, "since_last_look": [],
                    "dismissed_at": row["dismissed_at"],
                })
    finally:
        conn.close()
    return items


_TRACKED_KEYS = (
    "id", "url", "title", "author", "state", "is_draft", "target_branch", "ci_state",
    "comment_count", "review_count", "thread_count", "unresolved_threads", "age_days",
    "idle_days", "updated_at", "merged_at", "closed_at", "source", "since_last_look",
    "dismissed_at",
)


def summarize_tracked(t: dict) -> dict:
    """Compact watch row: no body, no discussion."""
    return {k: t.get(k) for k in _TRACKED_KEYS}


def tracked_detail(t: dict) -> dict:
    """Full tracked PR: summary plus body and its Discussion tree, bots flagged."""
    return {**summarize_tracked(t), "body": t.get("body"), "discussion": t["discussion"]}
