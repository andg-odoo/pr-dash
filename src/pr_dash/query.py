from __future__ import annotations

import copy
import dataclasses
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
            cfg.repos,
            cfg.thresholds.stale_review_days,
            command_templates=dataclasses.asdict(cfg.commands),
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


def _members(item: dict) -> list[dict]:
    return item.get("members") or [
        {"repo": item.get("repo"), "repo_short": item.get("repo_short"),
         "number": item.get("number")}
    ]


def _member_matches(member: dict, repo_full: str | None,
                    repo_short: str | None, number: int) -> bool:
    if member.get("number") != number:
        return False
    if repo_full is not None and member.get("repo") != repo_full:
        return False
    if repo_short is not None and member.get("repo_short") != repo_short:
        return False
    return True


def _ref_label(item: dict) -> str:
    parts = [f"{m.get('repo_short')}#{m.get('number')}" for m in _members(item)]
    if len(parts) > 1:
        return f"{item['id']} [{' + '.join(parts)}]"
    return item["id"]


def _candidates(items: list[dict], limit: int = 20) -> str:
    labels = [_ref_label(it) for it in items[:limit]]
    if len(items) > limit:
        labels.append(f"... (+{len(items) - limit} more)")
    return ", ".join(labels) if labels else "(cache is empty)"


def resolve_item(items: list[dict], ref: str | int) -> dict:
    """Resolve a PR reference to a single item, matching item id and every
    member (so an enterprise number resolves to its odoo+enterprise pair).
    Raises ValueError with candidates on no match or ambiguity."""
    repo_full, repo_short, number = _parse_ref(ref)
    seen: set[str] = set()
    uniq: list[dict] = []
    for it in items:
        if any(_member_matches(m, repo_full, repo_short, number) for m in _members(it)):
            if it["id"] not in seen:
                seen.add(it["id"])
                uniq.append(it)
    if len(uniq) == 1:
        return uniq[0]
    if not uniq:
        raise ValueError(f"No PR matching {ref!r}. Available: {_candidates(items)}")
    raise ValueError(
        f"Ambiguous PR reference {ref!r} matches: "
        f"{', '.join(_ref_label(it) for it in uniq)}. "
        f"Qualify with a repo, e.g. 'odoo#{number}' or 'enterprise#{number}'."
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

def summarize(item: dict) -> dict:
    """Compact triage row: no body, threads, diffs, or commands."""
    return {
        "id": item["id"],
        "url": item.get("url"),
        "title": item.get("title"),
        "author": item.get("author"),
        "is_draft": item.get("is_draft"),
        "is_pair": item.get("is_pair"),
        "members": [
            {"repo_short": m.get("repo_short"), "number": m.get("number"),
             "closed": m.get("closed"), "reviewed": m.get("reviewed")}
            for m in _members(item)
        ],
        "target_branch": item.get("target_branch"),
        "modules": item.get("modules"),
        "additions": item.get("additions"),
        "deletions": item.get("deletions"),
        "changed_files": item.get("changed_files"),
        "bucket": item.get("bucket"),
        "flags": item.get("flags"),
        "ci_state": item.get("ci_state"),
        "mergeable": item.get("mergeable"),
        "unresolved_threads": item.get("unresolved_threads"),
        "awaiting_my_reply": item.get("awaiting_my_reply"),
        "my_pending_review": item.get("my_pending_review"),
        "my_review_state": item.get("my_review_state"),
        "previously_reviewed": item.get("previously_reviewed"),
        "req_age_days": item.get("req_age_days"),
        "since_last_look": item.get("since_last_look"),
        "ai_review_verdict": item.get("ai_review_verdict"),
        "linked_task_label": item.get("linked_task_label"),
        "is_archived": item.get("is_archived"),
        "state": item.get("state"),
        "ping_at": item.get("ping_at"),
        "ping_author": item.get("ping_author"),
        "ping_snippet": item.get("ping_snippet"),
        "push_at": item.get("push_at"),
        "push_sha": item.get("push_sha"),
        # Carried on the compact row rather than left to get_pr: it answers
        # "does this data move have a migration" without a second call, which is
        # the question that keeps being answered wrongly from the diff alone.
        "companion": item.get("companion"),
    }


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
    parsed `files` list. Body, threads, reviewers, ci_failures, ai_reviews,
    commands and task links are kept."""
    out = copy.deepcopy(item)
    out["diffs"] = [_diff_meta(d) for d in item.get("diffs", [])]
    return out


def _comment_view(row: dict) -> dict:
    return {
        "author": row["author"],
        "bot": derive.is_bot(row["author"]),
        "created_at": row["created_at"],
        "body": row["body"],
        "url": row["url"],
    }


def _shape_member_comments(member: dict, rows: list[dict],
                           resolved: dict[str, bool]) -> dict:
    """Group one member's pr_comment rows into threads / reviews / conversation.
    `resolved` maps thread_id -> is_resolved (from pr_thread)."""
    threads: dict[str, dict] = {}
    reviews: list[dict] = []
    conversation: list[dict] = []
    for r in rows:
        if r["kind"] == "thread":
            tid = r["thread_id"]
            th = threads.get(tid)
            if th is None:
                th = {"thread_id": tid, "is_resolved": resolved.get(tid, False),
                      "path": r["path"], "comments": []}
                threads[tid] = th
            if not th["path"] and r["path"]:
                th["path"] = r["path"]
            th["comments"].append(_comment_view(r))
        elif r["kind"] == "review":
            reviews.append({
                "author": r["author"],
                "bot": derive.is_bot(r["author"]),
                "state": r["state"],
                "submitted_at": r["created_at"],
                # A PENDING (or never-submitted) review is an unsent draft, only
                # visible to its own author's token - the invisible-draft case.
                "pending": r["state"] == "PENDING" or r["created_at"] is None,
                "body": r["body"],
                "url": r["url"],
            })
        elif r["kind"] == "issue":
            conversation.append(_comment_view(r))
    return {
        "repo_short": member.get("repo_short"),
        "number": member.get("number"),
        "threads": list(threads.values()),
        "reviews": reviews,
        "conversation": conversation,
    }


def get_comments(cfg: Config, item: dict) -> dict:
    """Full comment/review/thread data for one PR (both halves of a pair).

    Per member: `threads` (grouped, each with is_resolved, path and its comments
    in order, full bodies), `reviews` (submissions with author, state,
    submitted_at, body; PENDING entries are unsent drafts flagged pending: true),
    and `conversation` (top-level PR comments). Bot authors (robodoo, fw-bot,
    `*[bot]`) carry bot: true so callers can filter automation noise."""
    conn = db.connect(cfg.db_path)
    try:
        threads_by_pr = db.list_threads(conn)
        members = []
        for m in _members(item):
            pr_id = f"{m.get('repo')}#{m.get('number')}"
            resolved = {t["thread_id"]: bool(t["is_resolved"])
                        for t in threads_by_pr.get(pr_id, [])}
            members.append(_shape_member_comments(
                m, db.comments_for(conn, pr_id), resolved,
            ))
    finally:
        conn.close()
    return {"id": item["id"], "members": members}


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


# --- tracked PRs -------------------------------------------------------------

def load_tracked(cfg: Config, *, include_dismissed: bool = False) -> list[dict]:
    """Build the tracked list from the cache, read-only.

    Like load_items, this deliberately skips commit_tracked_seen_baseline: an
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
                    "comments": [], "source": row["source"],
                    "added_at": row["added_at"], "updated_at": row["updated_at"],
                    "merged_at": row["merged_at"], "closed_at": row["closed_at"],
                    "age_days": 0, "idle_days": 0, "since_last_look": [],
                    "dismissed_at": row["dismissed_at"],
                })
    finally:
        conn.close()
    return items


def resolve_tracked(items: list[dict], ref: str | int) -> dict:
    """Find one tracked PR by '12345', 'odoo#12345', 'odoo/odoo#12345' or URL."""
    repo_full, repo_short, number = _parse_ref(ref)
    matches = [
        t for t in items
        if t["number"] == number
        and (repo_full is None or t["repo"] == repo_full)
        and (repo_short is None or t["repo_short"] == repo_short)
    ]
    if not matches:
        known = ", ".join(f"{t['repo_short']}#{t['number']}" for t in items[:20])
        raise ValueError(f"No tracked PR matches {ref!r}. Tracked: {known or '(none)'}")
    if len(matches) > 1:
        opts = ", ".join(f"{t['repo']}#{t['number']}" for t in matches)
        raise ValueError(f"{ref!r} is ambiguous - candidates: {opts}")
    return matches[0]


def summarize_tracked(t: dict) -> dict:
    """Compact watch row: no body, no discussion."""
    return {
        "id": t["id"],
        "url": t.get("url"),
        "title": t.get("title"),
        "author": t.get("author"),
        "state": t.get("state"),
        "is_draft": t.get("is_draft"),
        "target_branch": t.get("target_branch"),
        "ci_state": t.get("ci_state"),
        "comment_count": t.get("comment_count"),
        "review_count": t.get("review_count"),
        "thread_count": t.get("thread_count"),
        "unresolved_threads": t.get("unresolved_threads"),
        "age_days": t.get("age_days"),
        "idle_days": t.get("idle_days"),
        "updated_at": t.get("updated_at"),
        "merged_at": t.get("merged_at"),
        "closed_at": t.get("closed_at"),
        "source": t.get("source"),
        "since_last_look": t.get("since_last_look"),
        "dismissed_at": t.get("dismissed_at"),
    }


def tracked_detail(t: dict) -> dict:
    """Full tracked PR: summary plus body and the merged discussion stream.

    Discussion entries carry kind ('issue' | 'review' | 'thread'), and thread
    entries carry thread_id/parent_id so a consumer can rebuild the same nesting
    the dashboard shows - threads hang off the review that opened them.
    """
    out = summarize_tracked(t)
    out["body"] = t.get("body")
    out["discussion"] = [
        {**_comment_view(c), "kind": c.get("kind"),
         "thread_id": c.get("thread_id"), "parent_id": c.get("parent_id"),
         "path": c.get("path"), "state": c.get("state")}
        for c in (t.get("comments") or [])
    ]
    return out
