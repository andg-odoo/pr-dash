from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pr_dash import db, derive
from pr_dash.config import RepoSpec

TEMPLATES_DIR = Path(__file__).parent / "templates"

BUCKET_RANK = {"S": 0, "M": 1, "L": 2, "XL": 3}


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )


def _json_for_script(payload: object) -> str:
    """Serialize for embedding inside an inline <script>. json.dumps does not
    escape `</script>` or the JS line separators U+2028/U+2029, so PR-controlled
    strings (titles, bodies, branch names) could otherwise break out of the
    script context and execute. Escaping `<` as `\\u003c` is parsed back to `<`
    by JSON, keeping the data identical while making breakout impossible."""
    return (
        json.dumps(payload)
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _latest_thread_comment(comments: list[dict], thread_id: str) -> dict | None:
    cs = [c for c in comments if c["kind"] == "thread" and c["thread_id"] == thread_id]
    if not cs:
        return None
    return max(cs, key=lambda c: c.get("created_at") or "")


def _build_pr_record(
    conn: sqlite3.Connection,
    pr: dict,
    modules: list[str],
    reviewers: list[dict],
    threads: list[dict],
    comments: list[dict],
    my_login: str,
    stale_review_days: int,
) -> dict:
    installable = derive.installable_modules(modules)
    complexity = db.get_complexity(conn, pr["head_sha"])
    diff = db.get_diff(conn, pr["head_sha"])

    # Files whose change-content differs from what I last reviewed (or are new).
    # None means no baseline (never reviewed, or head still == reviewed head) -
    # the frontend then folds purely by size, with no review-delta treatment.
    review_changed_paths = None
    snap = db.get_review_snapshot(conn, pr["id"])
    if snap and diff and diff["patch_text"] and snap["reviewed_sha"] != pr["head_sha"]:
        base_sigs = json.loads(snap["signatures"])
        cur_sigs = derive.file_change_signatures(diff["patch_text"])
        review_changed_paths = sorted(
            p for p, s in cur_sigs.items() if base_sigs.get(p) != s
        )

    review_row = db.get_ai_review_any(conn, pr["head_sha"])
    ai_review: dict | None = None
    if review_row:
        try:
            concerns = json.loads(review_row["concerns"] or "[]")
        except (json.JSONDecodeError, TypeError):
            concerns = []
        ai_review = {
            "summary": review_row["summary"],
            "concerns": concerns,
            "verdict": review_row["verdict"],
            "computed_at": review_row["computed_at"],
            # 'manual' for a hand-written backfill, 'auto' for a model pass.
            # Item assembly exempts manual reviews from the staleness check.
            "source": review_row["source"] or "auto",
            # Stored pair context - used to validate against current pair state
            # at item-assembly time. Stale (mismatched) reviews are dropped.
            "sibling_head_sha": review_row["sibling_head_sha"] or "",
            # Same, for the companion migration: a review written before one
            # appeared was told nothing carried the data across.
            "companion_head_sha": review_row["companion_head_sha"] or "",
        }

    companion_row = db.get_companion(conn, pr["id"])
    companion = {
        "repo": companion_row["repo"],
        "repo_short": companion_row["repo"].split("/")[-1],
        "number": companion_row["number"],
        "url": companion_row["url"],
        "title": companion_row["title"],
        "author": companion_row["author"],
        "state": companion_row["state"],
        "is_draft": bool(companion_row["is_draft"]),
        "head_sha": companion_row["head_sha"],
    } if companion_row else None

    try:
        ci_failures = json.loads(pr["ci_failures"]) if pr["ci_failures"] else []
    except (json.JSONDecodeError, TypeError):
        ci_failures = []

    age_days = derive.days_since(pr["created_at"])
    req_age_days = derive.days_since(pr["review_requested_at"])
    is_stale = req_age_days > stale_review_days

    flags = []
    if pr["previously_reviewed"]:
        flags.append("RE")
    if pr["awaiting_my_reply"]:
        flags.append("MSG")
    if pr["ci_state"] in ("FAILURE", "ERROR"):
        flags.append("CI!")
    if pr["mergeable"] == "CONFLICTING":
        flags.append("CFL")
    if is_stale:
        flags.append("OLD")
    if pr.get("my_pending_review"):
        flags.append("PEND!")

    # One-line preview of each unresolved thread's latest comment, so the detail
    # pane can surface discussion without baking full bodies into the payload.
    for t in threads:
        if t.get("is_resolved"):
            continue
        latest = _latest_thread_comment(comments, t["thread_id"])
        if latest:
            t["snippet"] = derive.comment_snippet(latest.get("body"))
            t["snippet_author"] = latest.get("author")
            t["url"] = latest.get("url")

    return {
        "id": pr["id"],
        "repo": pr["repo"],
        "repo_short": pr["repo"].split("/")[-1],
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "author": pr["author"],
        "is_draft": bool(pr["is_draft"]),
        "body": pr["body"] or "",
        "target_branch": pr["target_branch"],
        "head_branch": pr["head_branch"],
        "head_sha": pr["head_sha"],
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "changed_files": pr["changed_files"],
        "modules": modules,
        "installable": installable,
        "mergeable": pr["mergeable"],
        "ci_state": pr["ci_state"],
        "ci_failures": ci_failures,
        "runbot_url": pr["runbot_url"],
        "linked_task": pr["linked_task"],
        "linked_task_kind": pr["linked_task_kind"],
        "created_at": pr["created_at"],
        "updated_at": pr["updated_at"],
        "review_requested_at": pr["review_requested_at"],
        "age_days": age_days,
        "req_age_days": req_age_days,
        "previously_reviewed": bool(pr["previously_reviewed"]),
        "awaiting_my_reply": bool(pr["awaiting_my_reply"]),
        "my_pending_review": bool(pr.get("my_pending_review")),
        "unresolved_threads": pr["unresolved_threads"],
        "flags": flags,
        "bucket": complexity["bucket"] if complexity else "M",
        "bucket_score": complexity["score"] if complexity else 0,
        "reviewers": reviewers,
        "threads": threads,
        "diff_available": diff is not None and diff["patch_text"] is not None,
        "diff_truncated": bool(diff and diff["truncated"]),
        "diff": diff["patch_text"] if diff and diff["patch_text"] else None,
        "review_changed_paths": review_changed_paths,
        "archived_at": pr["archived_at"],
        "state": pr["state"],
        "ping_at": pr.get("ping_at"),
        "ping_author": pr.get("ping_author"),
        "ping_snippet": pr.get("ping_snippet"),
        "push_at": pr.get("push_at"),
        "push_sha": pr.get("push_sha"),
        "companion": companion,
        "ai_review": ai_review,
    }


def _make_item(members: list[dict], my_login: str,
               repos: dict[str, RepoSpec],
               command_templates: dict[str, str] | None = None) -> dict:
    """Build one renderable item from one or two PR records.

    For pairs, members are ordered with odoo/odoo first when possible.
    """
    from pr_dash.commands import PRForCommands, build as build_cmds

    # Sort so odoo/odoo is primary when paired
    members = sorted(members, key=lambda m: 0 if m["repo"] == "odoo/odoo" else 1)
    primary = members[0]
    is_pair = len(members) == 2

    # Aggregate sums
    additions = sum(m["additions"] for m in members)
    deletions = sum(m["deletions"] for m in members)
    changed_files = sum(m["changed_files"] for m in members)

    # Union modules (dedup preserving order: primary first)
    seen: dict[str, None] = {}
    for m in members:
        for mod in m["modules"]:
            seen.setdefault(mod, None)
    modules = list(seen.keys())
    installable = derive.installable_modules(modules)

    # Worst bucket across members
    bucket = max((m["bucket"] for m in members), key=lambda b: BUCKET_RANK.get(b, 1))
    # Score from the member that contributed the worst bucket
    worst_member = max(members, key=lambda m: BUCKET_RANK.get(m["bucket"], 1))
    bucket_score = worst_member["bucket_score"]

    # Union flags
    flag_set = []
    for m in members:
        for f in m["flags"]:
            if f not in flag_set:
                flag_set.append(f)
    flags = flag_set

    # Reviewers union by (kind, name); state precedence: CHANGES_REQUESTED > APPROVED > COMMENTED > PENDING
    state_rank = {"CHANGES_REQUESTED": 3, "APPROVED": 2, "COMMENTED": 1, "PENDING": 0, "DISMISSED": -1}
    by_key: dict[tuple[str, str], dict] = {}
    for m in members:
        for r in m["reviewers"]:
            key = (r["kind"], r["name"])
            existing = by_key.get(key)
            if not existing or state_rank.get(r["state"], 0) > state_rank.get(existing["state"], 0):
                by_key[key] = r
    reviewers = list(by_key.values())
    my_reviewer = next((r for r in reviewers if r["kind"] == "user" and r["name"] == my_login),
                       {"kind": "user", "name": my_login, "state": "PENDING"})
    other_reviewers = [r for r in reviewers if not (r["kind"] == "user" and r["name"] == my_login)]

    # Threads: tag each with member repo for display
    threads = []
    for m in members:
        for t in m["threads"]:
            threads.append({**t, "member_repo_short": m["repo_short"], "member_url": m["url"]})

    # Aggregated derived fields
    # A pair counts as draft if either half is - neither is ready to review.
    is_draft = any(m["is_draft"] for m in members)
    awaiting_my_reply = any(m["awaiting_my_reply"] for m in members)
    my_pending_review = any(m["my_pending_review"] for m in members)
    previously_reviewed = any(m["previously_reviewed"] for m in members)
    unresolved_threads = sum(m["unresolved_threads"] for m in members)
    age_days = max(m["age_days"] for m in members)
    req_age_days = max(m["req_age_days"] for m in members)

    # Archive state: pair is archived only when both halves are archived;
    # if one half is still active, you're still being requested somewhere.
    is_archived = all(m["archived_at"] for m in members)
    archived_ats = [m["archived_at"] for m in members if m["archived_at"]]
    archived_at = max(archived_ats) if archived_ats else None

    # Informal re-review ping (only meaningful on archived-but-open PRs): take the
    # member with the most recent ping, and flag the item so triage can see it.
    pinged = [m for m in members if m.get("ping_at")]
    ping_member = max(pinged, key=lambda m: m["ping_at"]) if pinged else None
    if is_archived and ping_member and "PING" not in flags:
        flags.append("PING")

    # Push landed after my review (same archived-but-open population): the PR is
    # no longer mine to act on, so this flags without unarchiving - it says "what
    # you reviewed is not what is there now", not "review this again".
    pushed = [m for m in members if m.get("push_at")]
    push_member = max(pushed, key=lambda m: m["push_at"]) if pushed else None
    if is_archived and push_member and "PUSH" not in flags:
        flags.append("PUSH")

    # Both halves of a bundle carry the same migration, so take whichever member
    # has one - a pair whose odoo half was never cached still shows it.
    companion = next((m["companion"] for m in members if m.get("companion")), None)

    # Runbot / task: prefer primary, fall back to other
    runbot_url = primary["runbot_url"] or (members[1]["runbot_url"] if is_pair else None)
    linked_task = primary["linked_task"] or (members[1]["linked_task"] if is_pair else None)
    linked_task_kind = primary["linked_task_kind"] or (members[1]["linked_task_kind"] if is_pair else None)

    # Commands: derived once for the item using primary repo/number and pair info
    cmds = build_cmds(
        PRForCommands(
            repo=primary["repo"],
            number=primary["number"],
            target_branch=primary["target_branch"],
            modules=installable,
            paired_repo=members[1]["repo"] if is_pair else None,
            paired_number=members[1]["number"] if is_pair else None,
        ),
        repos,
        command_templates,
    )

    # Per-member diff payloads (so we render one diff section per repo)
    diffs = [
        {
            "repo": m["repo"],
            "repo_short": m["repo_short"],
            "number": m["number"],
            "url": m["url"],
            "closed": m["state"] != "OPEN",
            "reviewed": bool(m["archived_at"]),
            "available": m["diff_available"],
            "truncated": m["diff_truncated"],
            "diff": m["diff"],
            "review_changed_paths": m["review_changed_paths"],
            "additions": m["additions"],
            "deletions": m["deletions"],
            "changed_files": m["changed_files"],
        }
        for m in members
    ]

    # Per-member AI sanity-check reviews. Drop ones whose cached pair context
    # doesn't match the current pair state - those will be re-reviewed on the
    # next live run, and showing stale pair-blind output is misleading.
    ai_reviews = []
    for idx, m in enumerate(members):
        review = m.get("ai_review")
        if not review:
            continue
        cached_sibling = review.get("sibling_head_sha") or ""
        # Demand sibling context exactly when _build_review_queue would have
        # recorded it: when the partner's diff was cached, since that is the only
        # case where there was a companion diff to put in the prompt. Keying on
        # archived_at instead used to hide two reviews that were computed
        # correctly - an active partner whose diff blew the size gates (stored
        # pair-blind, expected a sha) and an archived-but-primed partner whose
        # diff was cached (stored with context, expected pair-blind).
        sib = members[1 - idx] if is_pair else None
        expected_sibling = sib["head_sha"] if sib and sib["diff"] else ""
        # The companion needs no such diff condition: it goes in the prompt on
        # existence alone, so its presence is what the review was written under.
        expected_companion = (m.get("companion") or {}).get("head_sha") or ""
        # A hand-written review is exempt: it says what a human read, so it does
        # not go stale when the pair state moves, and the automatic pass will not
        # replace it either. Dropping it here would hide it with nothing to
        # re-derive it.
        if review.get("source") != "manual" and (
            cached_sibling != expected_sibling
            or (review.get("companion_head_sha") or "") != expected_companion
        ):
            continue
        ai_reviews.append({
            "repo_short": m["repo_short"],
            "number": m["number"],
            **{k: v for k, v in review.items()
               if k not in ("sibling_head_sha", "companion_head_sha")},
        })
    # Worst verdict for the item-level pill: major > minor > looks-good
    verdict_rank = {"major": 2, "minor": 1, "looks-good": 0}
    worst_verdict = max(
        (r["verdict"] for r in ai_reviews),
        key=lambda v: verdict_rank.get(v, -1),
        default=None,
    ) if ai_reviews else None

    # CI: worst across members (FAILURE > PENDING > SUCCESS > None)
    ci_rank = {"FAILURE": 3, "ERROR": 3, "PENDING": 1, "SUCCESS": 0}
    ci_state = max((m["ci_state"] for m in members),
                   key=lambda s: ci_rank.get(s or "", -1), default=None)
    # Failing checks across members, tagged with the repo they failed on.
    ci_failures = [
        {**f, "repo_short": m["repo_short"]}
        for m in members for f in (m.get("ci_failures") or [])
    ]
    mergeable_rank = {"CONFLICTING": 2, "UNKNOWN": 1, "MERGEABLE": 0}
    mergeable = max((m["mergeable"] for m in members),
                    key=lambda s: mergeable_rank.get(s or "", -1), default=None)

    return {
        "id": primary["id"],
        "is_pair": is_pair,
        "head_sha": primary["head_sha"],
        "members": [
            {"repo": m["repo"], "repo_short": m["repo_short"], "number": m["number"],
             "url": m["url"], "head_sha": m["head_sha"], "title": m["title"],
             "closed": m["state"] != "OPEN", "reviewed": bool(m["archived_at"])}
            for m in members
        ],
        "title": primary["title"],
        "author": primary["author"],
        "is_draft": is_draft,
        "body": primary["body"],
        "target_branch": primary["target_branch"],
        "head_branch": primary["head_branch"],
        "url": primary["url"],
        "repo": primary["repo"],
        "repo_short": primary["repo_short"],
        "number": primary["number"],
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files,
        "modules": modules,
        "installable": installable,
        "mergeable": mergeable,
        "ci_state": ci_state,
        "ci_failures": ci_failures,
        "runbot_url": runbot_url,
        "linked_task": linked_task,
        "linked_task_kind": linked_task_kind,
        "linked_task_label": (
            f"{linked_task_kind}-{linked_task}"
            if linked_task and linked_task_kind else None
        ),
        "task_url": (f"https://www.odoo.com/odoo/all-tasks/{linked_task}"
                     if linked_task else None),
        "created_at": primary["created_at"],
        "updated_at": primary["updated_at"],
        "review_requested_at": primary["review_requested_at"],
        "age_days": age_days,
        "req_age_days": req_age_days,
        "previously_reviewed": previously_reviewed,
        "awaiting_my_reply": awaiting_my_reply,
        "my_pending_review": my_pending_review,
        "unresolved_threads": unresolved_threads,
        "since_last_look": sorted({t for m in members for t in m.get("since_last_look", [])}),
        "flags": flags,
        "bucket": bucket,
        "bucket_score": bucket_score,
        "reviewers": reviewers,
        "my_review_state": my_reviewer["state"],
        "other_reviewers": other_reviewers,
        "threads": threads,
        "commands": [{"label": c.label, "command": c.command} for c in cmds],
        "diffs": diffs,
        "state": primary["state"],
        "is_archived": is_archived,
        "archived_at": archived_at,
        "ping_at": ping_member["ping_at"] if ping_member else None,
        "ping_author": ping_member["ping_author"] if ping_member else None,
        "ping_snippet": ping_member["ping_snippet"] if ping_member else None,
        "push_at": push_member["push_at"] if push_member else None,
        "push_sha": push_member["push_sha"] if push_member else None,
        "companion": companion,
        "ai_reviews": ai_reviews,
        "ai_review_verdict": worst_verdict,
    }


def build_payload(
    conn: sqlite3.Connection,
    my_login: str,
    repos: dict[str, RepoSpec],
    stale_review_days: int,
    command_templates: dict[str, str] | None = None,
) -> tuple[list[dict], list[tuple[str, str | None, str | None, str]]]:
    """Return (items, seen_updates). The caller must persist seen_updates via
    commit_seen_baseline() only *after* a successful render - otherwise a render
    failure would silently advance the "last look" baseline and drop the
    pushed/ci/reply deltas for everything that changed since."""
    pr_rows = db.list_prs(conn)
    modules_by_pr = db.list_modules(conn)
    reviewers_by_pr = db.list_reviewers(conn)
    threads_by_pr = db.list_threads(conn)
    comments_by_pr = db.list_comments(conn)

    pr_dicts = [dict(r) for r in pr_rows]
    pairs = derive.detect_pairs(pr_dicts)

    # "Since last look": diff each active PR's current state against what it was
    # at the previous render, then record the new state. First run (no baseline)
    # flags nothing. Rendering is the "look", so this also runs offline.
    seen_rows = db.list_seen(conn)
    first_seen_run = not seen_rows
    delta_map: dict[str, list[str]] = {}
    seen_updates: list[tuple[str, str | None, str | None, str]] = []
    for pr in pr_dicts:
        if pr["archived_at"]:
            continue
        thread_sig = derive.thread_signature(threads_by_pr.get(pr["id"], []))
        prev = seen_rows.get(pr["id"])
        prev_tuple = (prev["head_sha"], prev["ci_state"], prev["thread_sig"]) if prev else None
        delta_map[pr["id"]] = derive.since_last_look_tags(
            prev_tuple, pr["head_sha"], pr["ci_state"], thread_sig, first_run=first_seen_run,
        )
        seen_updates.append((pr["id"], pr["head_sha"], pr["ci_state"], thread_sig))

    records: dict[str, dict] = {}
    for pr in pr_dicts:
        records[pr["id"]] = _build_pr_record(
            conn, pr,
            modules_by_pr.get(pr["id"], []),
            reviewers_by_pr.get(pr["id"], []),
            threads_by_pr.get(pr["id"], []),
            comments_by_pr.get(pr["id"], []),
            my_login, stale_review_days,
        )
        records[pr["id"]]["since_last_look"] = delta_map.get(pr["id"], [])

    items: list[dict] = []
    seen: set[str] = set()
    for pr_id in records:
        if pr_id in seen:
            continue
        seen.add(pr_id)
        paired_id = pairs.get(pr_id)
        if paired_id and paired_id in records:
            seen.add(paired_id)
            items.append(_make_item([records[pr_id], records[paired_id]], my_login,
                                     repos, command_templates))
        else:
            items.append(_make_item([records[pr_id]], my_login, repos, command_templates))

    items.sort(key=lambda p: (
        BUCKET_RANK.get(p["bucket"], 1),
        -p["req_age_days"],
    ))

    return items, seen_updates


def build_tracked_payload(
    conn: sqlite3.Connection,
) -> tuple[list[dict], list[tuple[str, str | None, str | None, int | None]]]:
    """Return (tracked items, tracked seen_updates).

    Same contract as build_payload: the caller persists the baseline only after
    a successful render, so a render failure can't swallow the deltas.
    """
    rows = [dict(r) for r in db.list_tracked(conn)]
    comments_by_pr = db.list_tracked_comments(conn)
    seen_rows = db.list_tracked_seen(conn)
    first_run = not seen_rows

    items: list[dict] = []
    seen_updates: list[tuple[str, str | None, str | None, int | None]] = []
    for row in rows:
        prev = seen_rows.get(row["id"])
        prev_tuple = (
            (prev["state"], prev["head_sha"], prev["activity_count"]) if prev else None
        )
        deltas = derive.tracked_since_last_look(
            prev_tuple, row["state"], row["head_sha"], row["activity_count"] or 0,
            first_run=first_run,
        )
        seen_updates.append(
            (row["id"], row["state"], row["head_sha"], row["activity_count"] or 0),
        )
        repo_short = row["repo"].split("/")[-1]
        items.append({
            "id": row["id"],
            "repo": row["repo"],
            "repo_short": repo_short,
            "number": row["number"],
            "url": row["url"],
            "title": row["title"],
            "author": row["author"],
            "state": row["state"],
            "is_draft": bool(row["is_draft"]),
            "target_branch": row["target_branch"],
            "ci_state": row["ci_state"],
            "comment_count": row["comment_count"] or 0,
            "activity_count": row["activity_count"] or 0,
            "review_count": row["review_count"] or 0,
            "thread_count": row["thread_count"] or 0,
            "unresolved_threads": row["unresolved_threads"] or 0,
            "body": row["body"],
            "comments": [
                c for c in comments_by_pr.get(row["id"], [])
                if not derive.is_bot(c.get("author"))
            ],
            "source": row["source"],
            "added_at": row["added_at"],
            "updated_at": row["updated_at"],
            "merged_at": row["merged_at"],
            "closed_at": row["closed_at"],
            "age_days": derive.days_since(row["created_at"]) if row["created_at"] else 0,
            "idle_days": derive.days_since(row["updated_at"]) if row["updated_at"] else 0,
            "since_last_look": deltas,
        })

    # Resolved first (that's the event you subscribed for), then most recently
    # active. Rows never fetched yet sort last rather than crashing on None.
    # Two stable passes: newest-active first, then resolved rows pushed to the
    # bottom. A watch list accumulates a long tail of things that closed months
    # ago, and putting those on top buries the live PRs - the dashboard's
    # `merged / closed first` sort is there for a deliberate catch-up pass.
    # This only sets the first paint; the tab re-sorts client-side on load.
    # Rows not yet fetched have no updated_at and land last in their group.
    items.sort(key=lambda t: t["updated_at"] or "", reverse=True)
    items.sort(key=lambda t: 1 if t["state"] in ("MERGED", "CLOSED") else 0)
    return items, seen_updates


def commit_tracked_seen_baseline(
    conn: sqlite3.Connection,
    seen_updates: list[tuple[str, str | None, str | None, int | None]],
    now: str,
) -> None:
    with db.transaction(conn):
        for pr_id, state, head_sha, activity_count in seen_updates:
            db.upsert_tracked_seen(conn, pr_id, state, head_sha, activity_count, now)


def commit_seen_baseline(
    conn: sqlite3.Connection,
    seen_updates: list[tuple[str, str | None, str | None, str]],
    now: str,
) -> None:
    """Record the current state as the new "last look" baseline for the next
    render. Call only after the render has succeeded (see build_payload)."""
    with db.transaction(conn):
        for pr_id, head_sha, ci_state, thread_sig in seen_updates:
            db.upsert_seen(conn, pr_id, head_sha, ci_state, thread_sig, now)


def render(payload: list[dict], html_path: Path, *, offline: bool = False,
           last_refresh: str | None = None, hidden_map: dict | None = None,
           hidden_sync_port: int = 7391,
           tracked: list[dict] | None = None) -> None:
    env = _env()
    template = env.get_template("index.html.j2")
    assets_dir = TEMPLATES_DIR / "assets"
    html = template.render(
        prs_json=_json_for_script(payload),
        pr_count=len(payload),
        tracked_json=_json_for_script(tracked or []),
        tracked_count=len(tracked or []),
        offline=offline,
        last_refresh=last_refresh or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        hidden_server_json=_json_for_script(hidden_map or {}),
        hidden_sync_port=hidden_sync_port,
        app_js=(assets_dir / "app.js").read_text(),
        app_css=(assets_dir / "app.css").read_text(),
        diff2html_css=(assets_dir / "vendor" / "diff2html" / "diff2html.min.css").read_text(),
        diff2html_js=(assets_dir / "vendor" / "diff2html" / "diff2html-ui.min.js").read_text(),
        markdown_it_js=(assets_dir / "vendor" / "markdown-it" / "markdown-it.min.js").read_text(),
    )

    html_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=html_path.parent,
        delete=False, suffix=".tmp",
    ) as tmp:
        tmp.write(html)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, html_path)
