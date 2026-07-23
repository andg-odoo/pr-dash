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


def _build_pr_record(
    conn: sqlite3.Connection,
    pr: dict,
    modules: list[str],
    reviewers: list[dict],
    threads: list[dict],
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
            # Stored pair context - used to validate against current pair state
            # at item-assembly time. Stale (mismatched) reviews are dropped.
            "sibling_head_sha": review_row["sibling_head_sha"] or "",
        }

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
    previously_reviewed = any(m["previously_reviewed"] for m in members)
    unresolved_threads = sum(m["unresolved_threads"] for m in members)
    age_days = max(m["age_days"] for m in members)
    req_age_days = max(m["req_age_days"] for m in members)

    # Archive state: pair is archived only when both halves are archived;
    # if one half is still active, you're still being requested somewhere.
    is_archived = all(m["archived_at"] for m in members)
    archived_ats = [m["archived_at"] for m in members if m["archived_at"]]
    archived_at = max(archived_ats) if archived_ats else None

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
        # Only demand sibling context when the partner is still active -
        # _build_review_queue skips archived siblings, so a review computed
        # while the partner was already closed is legitimately pair-blind.
        sib = members[1 - idx] if is_pair else None
        expected_sibling = sib["head_sha"] if sib and not sib["archived_at"] else ""
        if cached_sibling != expected_sibling:
            continue
        ai_reviews.append({
            "repo_short": m["repo_short"],
            "number": m["number"],
            **{k: v for k, v in review.items() if k != "sibling_head_sha"},
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
           last_refresh: str | None = None) -> None:
    env = _env()
    template = env.get_template("index.html.j2")
    assets_dir = TEMPLATES_DIR / "assets"
    html = template.render(
        prs_json=_json_for_script(payload),
        pr_count=len(payload),
        offline=offline,
        last_refresh=last_refresh or datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
