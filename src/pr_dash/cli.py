from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from pr_dash import ai, config, db, derive, github, render

console = Console()
log = logging.getLogger("pr_dash")


@click.group(invoke_without_command=True)
@click.option("--no-open", is_flag=True, help="Skip xdg-open of generated HTML")
@click.option("--force", is_flag=True, help="Ignore staleness window, refetch everything")
@click.option("--offline", is_flag=True, help="Render from cache only, no network")
@click.option("--config", "config_path", type=click.Path(path_type=Path),
              help="Override config file path")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
@click.pass_context
def cli(ctx, no_open, force, offline, config_path, verbose):
    """Personal Odoo reviewer dashboard."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(refresh, no_open=no_open, force=force, offline=offline, config_path=config_path)


@cli.command()
@click.option("--limit", default=1000, type=int,
              help="Max historical PRs to backfill (GitHub search caps at 1000).")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def backfill(limit, config_path):
    """One-shot backfill of historical reviews for accurate KPI counts.

    Fetches PRs where you've submitted a review (regardless of current request
    state) and inserts minimal archived rows keyed by your latest review
    submission. Skips already-cached IDs. No AI analysis, no diff fetch -
    if a PR ever re-enters the active set, the normal refresh path handles it.
    """
    try:
        cfg = config.load(config_path)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]Config error: {e}[/red]")
        sys.exit(1)

    conn = db.connect(cfg.db_path)

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=console, transient=True) as progress:
        task = progress.add_task("Fetching historical reviews from GitHub...", total=None)
        try:
            nodes, rate = github.search_reviewed_by(cfg.github_login, limit=limit)
        except github.GithubError as e:
            console.print(f"[red]GitHub error: {e}[/red]")
            sys.exit(1)
        if rate:
            log.debug("GraphQL rate limit: %d remaining (cost %d)",
                      rate.remaining, rate.cost)

        progress.update(task, description=f"Backfilling {len(nodes)} PRs...")
        added = skipped = no_review = self_authored = updated = 0
        now = derive.now_utc()
        with db.transaction(conn):
            for node in nodes:
                # Skip PRs the user authored - `reviewed-by:me` also matches
                # comment-only "reviews" you submit on your own PRs, which
                # aren't reviews of other people's work and shouldn't count
                # toward the reviewer KPI.
                author = (node.get("author") or {}).get("login")
                if author == cfg.github_login:
                    self_authored += 1
                    continue

                pr_id = f"{node['repository']['nameWithOwner']}#{node['number']}"

                review_nodes = (node.get("reviews") or {}).get("nodes") or []
                my_reviews = [
                    r for r in review_nodes
                    if r.get("submittedAt")
                    and (r.get("author") or {}).get("login") == cfg.github_login
                ]
                if not my_reviews:
                    no_review += 1
                    continue
                latest = max(my_reviews, key=lambda r: r["submittedAt"])
                latest_review_at = latest["submittedAt"]
                my_state = latest.get("state") or "COMMENTED"

                cached = db.get_cached_pr(conn, pr_id)
                if cached:
                    # An active PR's reviewer data from the live refresh is more
                    # current, so only fill the verdict on already-archived rows
                    # (earlier backfills that predate verdict capture).
                    if cached["archived_at"]:
                        db.set_my_review_state(conn, pr_id, cfg.github_login, my_state)
                        updated += 1
                    else:
                        skipped += 1
                    continue

                pr_row = {
                    "id": pr_id,
                    "repo": node["repository"]["nameWithOwner"],
                    "number": node["number"],
                    "title": node["title"] or "(no title)",
                    "url": node["url"],
                    "author": (node.get("author") or {}).get("login") or "(unknown)",
                    "target_branch": node["baseRefName"],
                    "head_branch": node["headRefName"],
                    "head_sha": node["headRefOid"],
                    "created_at": node["createdAt"],
                    "updated_at": node["updatedAt"],
                    "review_requested_at": latest_review_at,
                    "previously_reviewed": 1,
                    "mergeable": node.get("mergeable"),
                    "ci_state": None,
                    "runbot_url": None,
                    "additions": node.get("additions") or 0,
                    "deletions": node.get("deletions") or 0,
                    "changed_files": node.get("changedFiles") or 0,
                    "unresolved_threads": 0,
                    "awaiting_my_reply": 0,
                    "linked_task": None,
                    "linked_task_kind": None,
                    "body": None,
                    "archived_at": latest_review_at,
                    "fetched_at": now,
                }
                db.upsert_pr(conn, pr_row)
                db.set_my_review_state(conn, pr_id, cfg.github_login, my_state)
                added += 1

    console.print(
        f"[green]Backfilled {added} historical reviews.[/green] "
        f"({updated} verdicts filled, {skipped} already in cache, "
        f"{self_authored} self-authored skipped, "
        f"{no_review} with no detectable review by you)"
    )


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path),
              help="Override config file path")
def init(config_path):
    """Write a default config file."""
    path = config.write_default(config_path)
    if path.read_text().splitlines()[1].endswith('"your-github-login"'):
        console.print(f"[yellow]Wrote default config to {path}, but couldn't auto-detect "
                      "your GitHub login. Edit it before running pr-dash.[/yellow]")
    else:
        console.print(f"[green]Wrote default config to {path}[/green]")


@cli.command()
@click.option("--no-open", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--offline", is_flag=True)
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def refresh(no_open, force, offline, config_path):
    """Refresh cache and render dashboard (default action)."""
    try:
        cfg = config.load(config_path)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]Config error: {e}[/red]")
        sys.exit(1)

    conn = db.connect(cfg.db_path)
    last_refresh = derive.now_utc()

    if not offline:
        try:
            _run_refresh(conn, cfg, force=force)
        except github.GithubError as e:
            console.print(f"[red]GitHub error: {e}[/red]")
            console.print("[yellow]Falling back to cached data.[/yellow]")
            offline = True

    payload = render.build_payload(
        conn, cfg.github_login, cfg.repos, cfg.thresholds.stale_review_days,
    )
    for p in payload:
        p["my_login"] = cfg.github_login

    render.render(payload, cfg.html_path, offline=offline, last_refresh=last_refresh)
    console.print(f"[green]Rendered {len(payload)} PRs → {cfg.html_path}[/green]")

    if not no_open:
        try:
            subprocess.Popen(
                ["xdg-open", str(cfg.html_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            console.print(f"[yellow]Open manually: file://{cfg.html_path}[/yellow]")


def _run_refresh(conn, cfg, *, force: bool) -> None:
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=console, transient=True) as progress:
        task = progress.add_task("Searching for review requests...", total=None)
        nodes, rate = github.search_personal_review_requested(cfg.github_login)
        if rate:
            log.debug("GraphQL rate limit: %d remaining (cost %d)", rate.remaining, rate.cost)

        personally = [n for n in nodes if derive.is_personally_requested(
            (n.get("reviewRequests") or {}).get("nodes") or [],
            cfg.github_login,
        )]
        progress.update(task, description=f"Processing {len(personally)} PRs...")

        kept_ids: set[str] = set()
        staleness_cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=cfg.thresholds.staleness_minutes,
        )

        for i, node in enumerate(personally, 1):
            pr_id = _node_id(node)
            kept_ids.add(pr_id)
            progress.update(task, description=f"[{i}/{len(personally)}] {pr_id}")

            cached = db.get_cached_pr(conn, pr_id)
            head_sha = node["headRefOid"]
            updated_at = node["updatedAt"]

            needs_refresh = (
                force
                or cached is None
                or cached["head_sha"] != head_sha
                or cached["updated_at"] != updated_at
                or cached["archived_at"] is not None
                or derive.parse_iso(cached["fetched_at"]) < staleness_cutoff
                # Diff missing (e.g. cache cleared by a schema migration) must
                # re-fetch even when the PR detail is otherwise still fresh.
                or db.get_diff(conn, head_sha) is None
            )
            if not needs_refresh:
                continue

            pr_row, modules, reviewers, threads = _node_to_rows(node, cfg.github_login)

            with db.transaction(conn):
                db.upsert_pr(conn, pr_row)
                db.replace_modules(conn, pr_id, modules)
                db.replace_reviewers(conn, pr_id, reviewers)
                db.replace_threads(conn, pr_id, threads)

            # Patch fetch (if head_sha not cached)
            existing_diff = db.get_diff(conn, head_sha)
            if existing_diff is None or force:
                if pr_row["changed_files"] > cfg.thresholds.diff_max_files:
                    db.upsert_diff(conn, head_sha, None, True, derive.now_utc())
                else:
                    patch = github.fetch_patch(pr_row["repo"], pr_row["number"])
                    truncated = patch is not None and patch.count("\n") > cfg.thresholds.diff_max_lines
                    if truncated:
                        patch = None
                    db.upsert_diff(conn, head_sha, patch, truncated, derive.now_utc())

            # Snapshot the per-file change signatures of the diff I reviewed, so a
            # later re-review can fold files unchanged since. Only when the commit
            # I reviewed *is* the current head - otherwise the cached diff isn't
            # the one I saw, and we'd rather have no baseline than a wrong one.
            my_review = next(
                (r for r in (node.get("latestReviews") or {}).get("nodes") or []
                 if (r.get("author") or {}).get("login") == cfg.github_login),
                None,
            )
            reviewed_sha = (my_review.get("commit") or {}).get("oid") if my_review else None
            if reviewed_sha and reviewed_sha == head_sha:
                snap = db.get_review_snapshot(conn, pr_id)
                if not snap or snap["reviewed_sha"] != reviewed_sha:
                    diff_row = db.get_diff(conn, head_sha)
                    if diff_row and diff_row["patch_text"]:
                        db.upsert_review_snapshot(
                            conn, pr_id, reviewed_sha,
                            json.dumps(derive.file_change_signatures(diff_row["patch_text"])),
                            derive.now_utc(),
                        )

            # Heuristic complexity
            score = derive.heuristic_score(
                additions=pr_row["additions"],
                deletions=pr_row["deletions"],
                changed_files=pr_row["changed_files"],
                modules=modules,
                unresolved_threads=pr_row["unresolved_threads"],
                previously_reviewed=bool(pr_row["previously_reviewed"]),
            )
            bucket = derive.bucket_for(score, cfg.buckets.M, cfg.buckets.L, cfg.buckets.XL)
            db.upsert_complexity(conn, {
                "head_sha": head_sha,
                "bucket": bucket,
                "score": score,
                "method": "heuristic",
                "notes": None,
                "computed_at": derive.now_utc(),
            })

        # Before sweeping, rescue delete-candidates whose cached
        # previously_reviewed=0 is stale: a PR I just approved/changes-requested
        # drops out of `review-requested:` immediately, so its last fetch
        # predates my review. Verify those against GitHub and flip the ones I
        # actually reviewed so the sweep archives (not deletes) them.
        reconciled = _reconcile_reviewed(conn, kept_ids, cfg.github_login)

        # Sweep PRs that fell out of the result set: archive ones I reviewed,
        # delete ones I never reviewed. Skip deletes if reconciliation failed
        # this run, so an unverified PR is never lost to a transient error.
        archived, deleted = db.sweep(
            conn, kept_ids, derive.now_utc(), delete=reconciled,
        )
        if archived or deleted:
            log.debug("swept: %d archived, %d deleted", archived, deleted)

        if cfg.ai.enabled and cfg.ai.review_enabled:
            review_candidates, sibling_shas = _build_review_queue(
                conn, kept_ids, cfg.ai.review_max_diff_chars,
            )
            if review_candidates:
                progress.update(
                    task,
                    description=f"AI sanity-check {len(review_candidates)} small PRs...",
                )
                reviews = ai.review_batch(
                    review_candidates, timeout=cfg.ai.timeout_seconds,
                    model=cfg.ai.model,
                )
                for head_sha, rev in reviews.items():
                    db.upsert_ai_review(
                        conn, head_sha, sibling_shas.get(head_sha, ""),
                        rev.summary, json.dumps(rev.concerns),
                        rev.verdict, derive.now_utc(),
                    )


def _reconcile_reviewed(conn, kept_ids: set[str], login: str) -> bool:
    """Flip previously_reviewed=1 for fallen-out PRs I actually reviewed.

    Only delete-candidates (cached previously_reviewed=0, not yet archived,
    absent from the current request set) are checked, so this costs a couple of
    GraphQL requests at most and protects KPI history from silent deletion.

    Returns True if reconciliation completed (safe to delete the rest), False on
    a transient GitHub failure (caller should skip deletions this run).
    """
    candidates = db.delete_candidates(conn, kept_ids)
    if not candidates:
        return True
    refs = [(r["repo"], r["number"], r["id"]) for r in candidates]
    try:
        reviewed = github.fetch_reviewed_prs(refs, login)
    except github.GithubError as e:
        log.warning("review reconciliation failed (%s); deferring sweep deletes", e)
        return False
    if reviewed:
        db.mark_reviewed(conn, reviewed)
        log.debug("reconciled %d fallen-out PRs as reviewed", len(reviewed))
    return True


def _build_review_queue(
    conn,
    kept_ids: set[str],
    max_diff_chars: int,
) -> tuple[list, dict[str, str]]:
    """Build the AI sanity-check queue *after* all PR data is settled so pair
    detection is accurate. Gates per-PR on the actual diff size - a small-code
    breadth-XL reviews fine; a size-L with a huge diff would just truncate to
    noise.

    Returns (requests, head_sha_to_sibling_head_sha) - the latter is used when
    persisting the result, since ReviewRequest itself doesn't survive the AI call.
    """
    pr_rows = {pr_id: db.get_cached_pr(conn, pr_id) for pr_id in kept_ids}
    pr_rows = {k: v for k, v in pr_rows.items() if v is not None}
    pair_map = derive.detect_pairs([dict(r) for r in pr_rows.values()])
    modules_by_pr = db.list_modules(conn)

    requests: list = []
    sibling_shas: dict[str, str] = {}

    for pr_id, pr_row in pr_rows.items():
        head_sha = pr_row["head_sha"]
        diff_row = db.get_diff(conn, head_sha)
        diff_text = (diff_row["patch_text"] if diff_row else None) or ""
        if not diff_text or len(diff_text) > max_diff_chars:
            continue

        sibling_head_sha = ""
        sibling_kwargs: dict = {}
        sibling_id = pair_map.get(pr_id)
        if sibling_id and sibling_id in pr_rows:
            sib = pr_rows[sibling_id]
            sib_diff_row = db.get_diff(conn, sib["head_sha"])
            sib_diff = (sib_diff_row["patch_text"] if sib_diff_row else None) or ""
            if sib_diff:
                sibling_head_sha = sib["head_sha"]
                sibling_kwargs = {
                    "sibling_head_sha": sibling_head_sha,
                    "sibling_repo": sib["repo"],
                    "sibling_number": sib["number"],
                    "sibling_title": sib["title"],
                    "sibling_diff": sib_diff,
                }

        if db.get_ai_review(conn, head_sha, sibling_head_sha) is not None:
            continue

        requests.append(ai.ReviewRequest(
            head_sha=head_sha,
            title=pr_row["title"],
            body=pr_row["body"] or "",
            modules=modules_by_pr.get(pr_id, []),
            branch=pr_row["target_branch"],
            diff=diff_text,
            repo=pr_row["repo"],
            number=pr_row["number"],
            **sibling_kwargs,
        ))
        sibling_shas[head_sha] = sibling_head_sha

    return requests, sibling_shas


def _node_id(node: dict) -> str:
    return f"{node['repository']['nameWithOwner']}#{node['number']}"


def _node_to_rows(node: dict, my_login: str) -> tuple[dict, list[str], list[dict], list[dict]]:
    repo = node["repository"]["nameWithOwner"]
    paths = [f["path"] for f in (node.get("files") or {}).get("nodes", [])]
    # files() pagination beyond 100 is rare; fetch_remaining_files if needed
    files_pageinfo = (node.get("files") or {}).get("pageInfo") or {}
    if files_pageinfo.get("hasNextPage"):
        try:
            paths.extend(github.fetch_remaining_files(
                repo, node["number"], files_pageinfo.get("endCursor"),
            ))
        except github.GithubError:
            pass

    modules = derive.modules_for(repo, paths)

    timeline = (node.get("timelineItems") or {}).get("nodes") or []
    review_requested_at = derive.latest_review_requested_at(
        timeline, my_login, fallback=node["updatedAt"],
    )
    prev_reviewed = derive.previously_reviewed(timeline, my_login)

    thread_nodes = (node.get("reviewThreads") or {}).get("nodes") or []
    th = derive.derive_threads(thread_nodes, my_login)

    latest_reviews = (node.get("latestReviews") or {}).get("nodes") or []
    review_requests = (node.get("reviewRequests") or {}).get("nodes") or []
    reviewers = derive.derive_reviewers(latest_reviews, review_requests)

    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    ci_state, runbot_url = derive.status_check_state(rollup)

    parsed_task = derive.parse_linked_task(node.get("body"))
    task_kind, task_id = parsed_task if parsed_task else (None, None)

    pr_row = {
        "id": _node_id(node),
        "repo": repo,
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "author": (node.get("author") or {}).get("login") or "(unknown)",
        "target_branch": node["baseRefName"],
        "head_branch": node["headRefName"],
        "head_sha": node["headRefOid"],
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "review_requested_at": review_requested_at,
        "previously_reviewed": int(prev_reviewed),
        "mergeable": node.get("mergeable"),
        "ci_state": ci_state,
        "runbot_url": runbot_url,
        "additions": node["additions"],
        "deletions": node["deletions"],
        "changed_files": node["changedFiles"],
        "unresolved_threads": th.unresolved,
        "awaiting_my_reply": int(th.awaiting_my_reply),
        "linked_task": task_id,
        "linked_task_kind": task_kind,
        "body": node.get("body"),
        "archived_at": None,
        "fetched_at": derive.now_utc(),
    }
    return pr_row, modules, reviewers, th.threads


if __name__ == "__main__":
    cli()
