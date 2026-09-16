from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from pr_dash import ai, config, db, derive, github, hidden, render
from pr_dash import query as prquery

console = Console()
log = logging.getLogger("pr_dash")

# Waits after the first failed AI pass at a review context, then after the second.
_AI_RETRY_BACKOFF_HOURS = (1, 4)

# Room for many refreshes' worth of phase lines, without becoming a file to tidy by hand.
_CRON_LOG_MAX_BYTES = 1_000_000


class _LogProgress:
    """Progress stand-in for cron runs, where rich would write ANSI to a log."""

    def __enter__(self) -> _LogProgress:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def add_task(self, description: str, total=None) -> int:
        log.info("%s", description)
        return 0

    def update(self, task: int, description: str | None = None, **kwargs) -> None:
        if description:
            log.info("%s", description)


def _progress(cron: bool):
    if cron:
        return _LogProgress()
    return Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                    console=console, transient=True)


def _setup_cron_logging(cfg) -> None:
    """Send phase lines to a rotating cron.log and nowhere else."""
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        cfg.cache_dir / "cron.log", maxBytes=_CRON_LOG_MAX_BYTES, backupCount=2,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _notify(cron: bool, message: str, style: str = "") -> None:
    """One line to the console, or to the cron log when nobody is watching one."""
    if cron:
        log.info("%s", message)
    else:
        console.print(f"[{style}]{message}[/{style}]" if style else message)


def _acquire_refresh_lock(cfg, *, wait: bool) -> int | None:
    """Take the refresh lock, which every refresh holds, or None if someone else has it."""
    lock_path = cfg.cache_dir / "refresh.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if not wait:
            os.close(fd)
            return None
        console.print("[yellow]Another refresh is running; waiting for it...[/yellow]")
        fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _render_from_cache(conn, cfg, *, offline=False, last_refresh=None):
    """Build the payload from cache and write the dashboard HTML, baking in the
    pruned server-side hidden map. Returns (payload, seen_updates,
    tracked_seen_updates); the caller decides whether to advance either
    since-last-look baseline."""
    payload, seen_updates = render.build_payload(
        conn, cfg.github_login, cfg.repos, cfg.thresholds.stale_review_days,
        command_templates=dataclasses.asdict(cfg.commands),
        ai_max_attempts=cfg.ai.max_attempts,
    )
    for p in payload:
        p["my_login"] = cfg.github_login
    hidden_map = hidden.prune(hidden.load(cfg), payload)
    hidden.save(cfg, hidden_map)
    tracked, tracked_seen = render.build_tracked_payload(conn)
    render.render(payload, cfg.html_path, offline=offline, last_refresh=last_refresh,
                  hidden_map=hidden_map, hidden_sync_port=cfg.hidden_sync_port,
                  tracked=tracked)
    return payload, seen_updates, tracked_seen


def _load_config_or_exit(config_path):
    """Load config, printing a friendly message and exiting on failure."""
    try:
        return config.load(config_path)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]Config error: {e}[/red]")
        sys.exit(1)


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
    ctx.invoke(refresh, no_open=no_open, force=force, offline=offline, cron=False,
               config_path=config_path)


@cli.command()
@click.option("--limit", default=1000, type=int,
              help="Max historical PRs to backfill (GitHub search caps at 1000).")
@click.option("--since", default=None, metavar="YYYY-MM-DD",
              help="Only backfill PRs updated on/after this date. Useful when "
                   "you've reviewed more than 1000 PRs and only want a recent window.")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def backfill(limit, since, config_path):
    """One-shot backfill of historical reviews for accurate KPI counts.

    Fetches PRs where you've submitted a review (regardless of current request
    state) and inserts minimal archived rows keyed by your latest review
    submission. Skips already-cached IDs. No AI analysis, no diff fetch -
    if a PR ever re-enters the active set, the normal refresh path handles it.

    Results are capped at --limit (GitHub's own ceiling is 1000), newest-updated
    first. If you've reviewed more than that, narrow the window with --since.
    """
    if since is not None:
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            console.print(f"[red]--since must be a YYYY-MM-DD date, got {since!r}[/red]")
            sys.exit(1)

    cfg = _load_config_or_exit(config_path)

    conn = db.connect(cfg.db_path)

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=console, transient=True) as progress:
        task = progress.add_task("Fetching historical reviews from GitHub...", total=None)
        try:
            nodes, rate = github.search_reviewed_by(cfg.github_login, limit=limit, since=since)
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
                        if node.get("state"):
                            db.set_pr_state(conn, pr_id, node["state"])
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
                    "state": node.get("state") or "OPEN",
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

    # Re-render from cache (no network, no browser) so the dashboard reflects the
    # freshly backfilled verdicts instead of a stale render.
    payload, _, _ = _render_from_cache(conn, cfg, offline=False)
    console.print(f"[green]Re-rendered {len(payload)} PRs → {cfg.html_path}[/green]")


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path),
              help="Override config file path")
def init(config_path):
    """Write a default config file."""
    path, login = config.write_default(config_path)
    if login is None:
        console.print(f"[yellow]Config already exists at {path}; left unchanged.[/yellow]")
    elif login == config.DETECT_FAILED_LOGIN:
        console.print(f"[yellow]Wrote default config to {path}, but couldn't auto-detect "
                      "your GitHub login. Edit it before running pr-dash.[/yellow]")
    else:
        console.print(f"[green]Wrote default config to {path}[/green]")


@cli.command()
@click.option("--no-open", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--offline", is_flag=True)
@click.option("--cron", is_flag=True,
              help="Unattended run: log to cron.log, skip if another refresh "
                   "holds the lock, cap AI reviews, and leave the "
                   "since-last-look baseline where the dashboard left it.")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def refresh(no_open, force, offline, cron, config_path):
    """Refresh cache and render dashboard (default action)."""
    cfg = _load_config_or_exit(config_path)
    if cron:
        _setup_cron_logging(cfg)
    lock_fd = _acquire_refresh_lock(cfg, wait=not cron)
    if lock_fd is None:
        log.info("another refresh holds the lock; skipping this tick")
        return
    try:
        _refresh(cfg, no_open=no_open, force=force, offline=offline, cron=cron)
    finally:
        os.close(lock_fd)


def _refresh(cfg, *, no_open: bool, force: bool, offline: bool, cron: bool) -> None:
    conn = db.connect(cfg.db_path)
    last_refresh = derive.now_utc()

    if not offline:
        try:
            _run_refresh(conn, cfg, force=force, cron=cron)
        except github.GithubError as e:
            if cron:
                # Re-rendering would put an offline-bannered dashboard over a good one.
                log.error("GitHub error: %s", e)
                sys.exit(1)
            console.print(f"[red]GitHub error: {e}[/red]")
            console.print("[yellow]Falling back to cached data.[/yellow]")
            offline = True
        else:
            _run_tracked_refresh(conn, cfg, force=force, cron=cron)

    payload, seen_updates, tracked_seen = _render_from_cache(
        conn, cfg, offline=offline, last_refresh=last_refresh,
    )
    if cron:
        # Nobody looked, so advancing the baseline would hide changes never seen.
        log.info("rendered %d PRs -> %s", len(payload), cfg.html_path)
        return
    # Only now that the render succeeded do we advance the "last look" baseline,
    # so a render failure can't silently swallow the since-last-look deltas.
    render.commit_seen_baseline(conn, seen_updates, derive.now_utc())
    render.commit_tracked_seen_baseline(conn, tracked_seen, derive.now_utc())
    console.print(f"[green]Rendered {len(payload)} PRs → {cfg.html_path}[/green]")

    if not no_open:
        _open_html(cfg.html_path)


_PR_REF_RE = re.compile(
    r"^(?:https?://github\.com/)?([\w.-]+/[\w.-]+)(?:/pull/|#)(\d+)/?$"
)


def _parse_pr_ref(ref: str) -> tuple[str, int]:
    """Parse `owner/repo#123` or a github.com pull URL into (repo, number)."""
    m = _PR_REF_RE.match(ref.strip())
    if not m:
        raise ValueError(
            f"Cannot parse {ref!r}. Use owner/repo#123 or a github.com pull URL."
        )
    return m.group(1), int(m.group(2))


@cli.command()
@click.argument("refs", nargs=-1)
@click.option("--config", "config_path", type=click.Path(path_type=Path))
@click.option("--skip-invalid", is_flag=True,
              help="Warn and continue on unparseable refs instead of exiting.")
def track(refs, config_path, skip_invalid):
    """Track PRs in the dashboard's `tracked` tab.

    Takes `owner/repo#123` or a github.com pull URL. With no arguments (or a
    literal `-`) it reads refs from stdin, one per line, which is how the
    subscriptions-page import works - see `docs: tracked tab` in the README.

    This is the primary way to populate the tracked tab. The notification seed
    only finds PRs that have *generated* a notification, so a custom
    "notify on close only" subscription stays invisible until it resolves.
    """
    cfg = _load_config_or_exit(config_path)
    conn = db.connect(cfg.db_path)
    now = derive.now_utc()

    if not refs or "-" in refs:
        piped = [ln.strip() for ln in sys.stdin.read().splitlines()]
        refs = [*(r for r in refs if r != "-"), *(ln for ln in piped if ln)]
    if not refs:
        console.print("[red]No PR refs given (and nothing on stdin).[/red]")
        sys.exit(1)

    parsed = []
    for ref in refs:
        try:
            parsed.append(_parse_pr_ref(ref))
        except ValueError as e:
            if skip_invalid:
                console.print(f"[yellow]skipped: {e}[/yellow]")
                continue
            console.print(f"[red]{e}[/red]")
            sys.exit(1)

    added = 0
    with db.transaction(conn):
        for repo, number in parsed:
            pr_id = f"{repo}#{number}"
            if db.add_tracked(conn, pr_id, repo, number,
                              f"https://github.com/{repo}/pull/{number}", "manual", now):
                added += 1
                console.print(f"[green]Tracking {pr_id}[/green]")
            else:
                console.print(f"[dim]{pr_id} already tracked[/dim]")

    # Fill in title/state right away so `pr-dash rerender` shows real rows
    # instead of blank placeholders until the next full refresh. Covers revived
    # rows too, whose cached state is as old as the day they were dismissed.
    if parsed:
        try:
            fetched = _fetch_tracked_state(conn, parsed)
        except github.GithubError as e:
            console.print(f"[yellow]Tracked, but could not fetch state yet: {e}[/yellow]")
        else:
            console.print(f"[dim]{added} new, {fetched} fetched[/dim]")


@cli.command()
@click.argument("refs", nargs=-1, required=True)
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def untrack(refs, config_path):
    """Stop tracking PRs (removes them from the cache entirely)."""
    cfg = _load_config_or_exit(config_path)
    conn = db.connect(cfg.db_path)
    with db.transaction(conn):
        for ref in refs:
            try:
                repo, number = _parse_pr_ref(ref)
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
                sys.exit(1)
            pr_id = f"{repo}#{number}"
            if db.remove_tracked(conn, pr_id):
                console.print(f"[green]Untracked {pr_id}[/green]")
            else:
                console.print(f"[dim]{pr_id} was not tracked[/dim]")


def _fetch_tracked_state(conn, refs: list[tuple[str, int]]) -> int:
    """Refresh the cached GitHub state of the given tracked PRs. Returns the
    number of rows updated."""
    if not refs:
        return 0
    nodes = github.fetch_tracked_nodes(refs)
    now = derive.now_utc()
    with db.transaction(conn):
        for pr_id, node in nodes.items():
            row, comments = derive.tracked_row_from_node(node, now)
            db.update_tracked_state(conn, pr_id, row)
            db.replace_tracked_comments(conn, pr_id, comments)
    return len(nodes)


def _run_tracked_refresh(conn, cfg, *, force: bool, cron: bool = False) -> None:
    """Seed tracked PRs from manual notification subscriptions, then refresh the
    cached state of everything tracked.

    Seeding is additive only: notifications age out of GitHub's retention, so
    treating them as the full list would silently drop quiet PRs. Removal is an
    explicit `untrack` (or a dismissal from the dashboard).
    """
    now = derive.now_utc()
    with _progress(cron) as progress:
        task = progress.add_task("Reading tracked subscriptions...", total=None)
        try:
            subs = github.list_manual_subscriptions()
        except github.GithubError as e:
            # A notifications failure must not sink the review-queue refresh, the primary job.
            log.debug("tracked seed skipped: %s", e)
            _notify(cron, f"Could not read subscriptions: {e}", "yellow")
            subs = []

        added = 0
        with db.transaction(conn):
            for s in subs:
                if db.add_tracked(conn, s["id"], s["repo"], s["number"], s["url"],
                                  "notif", now):
                    added += 1

        rows = db.list_tracked(conn, include_dismissed=True)
        staleness_cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=cfg.thresholds.tracked_staleness_minutes,
        )
        stale = [
            (r["repo"], r["number"]) for r in rows
            # Fetching a dismissed row is waste, but it stays in the table to dedupe the next seed.
            if r["dismissed_at"] is None
            and (force or not r["fetched_at"]
                 or derive.parse_iso(r["fetched_at"]) < staleness_cutoff)
        ]
        updated = 0
        if stale:
            progress.update(task, description=f"Refreshing {len(stale)} tracked PRs...")
            try:
                updated = _fetch_tracked_state(conn, stale)
            except github.GithubError as e:
                _notify(cron, f"Tracked PR refresh failed: {e}", "yellow")

    if added or updated:
        _notify(cron, f"tracked: +{added} new, {updated} refreshed "
                      f"({len(rows)} total)", "dim")


@cli.command()
@click.option("--no-open", is_flag=True)
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def rerender(no_open, config_path):
    """Re-render the dashboard from cache (no fetch, no offline banner).

    Use after editing templates or static assets: rebuilds the HTML from the
    existing cache without contacting GitHub. Unlike `refresh --offline`, it
    omits the offline banner and leaves the since-last-look baseline untouched,
    since no new data was fetched.
    """
    cfg = _load_config_or_exit(config_path)
    conn = db.connect(cfg.db_path)

    payload, _, _ = _render_from_cache(conn, cfg, offline=False)
    console.print(f"[green]Re-rendered {len(payload)} PRs → {cfg.html_path}[/green]")

    if not no_open:
        _open_html(cfg.html_path)


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path),
              help="Override config file path")
def mcp(config_path):
    """Run the MCP server (stdio) for AI-agent access to the local cache.

    Read-only over the cache (the `refresh` tool being the exception, same as
    running `pr-dash refresh`). Spawned per-session by the MCP client over
    stdio; there is no daemon. Register with e.g.
    `claude mcp add pr-dash -- pr-dash mcp`.
    """
    try:
        from pr_dash import mcp_server
    except ImportError:
        print(
            "The MCP server needs the optional 'mcp' dependency: "
            "pip install 'pr-dash[mcp]'",
            file=sys.stderr,
        )
        sys.exit(1)
    if config_path is not None:
        mcp_server.set_config_path(config_path)
    mcp_server.main()


@cli.group()
def query():
    """Emit dashboard data as JSON (for agents and debugging)."""


def _emit(obj) -> None:
    click.echo(json.dumps(obj, indent=2))


def _resolve_or_exit(items, ref):
    try:
        return prquery.resolve_item(items, ref)
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)


@query.command("list")
@click.option("--status", type=click.Choice(["pending", "archived", "all"]),
              default="pending")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def query_list(status, config_path):
    """List PRs as compact triage rows."""
    cfg = _load_config_or_exit(config_path)
    items = prquery.load_items(cfg)
    if status == "pending":
        sel = [it for it in items if not it.get("is_archived")]
    elif status == "archived":
        sel = [it for it in items if it.get("is_archived")]
    else:
        sel = items
    _emit({
        "cache_fetched_at": prquery.cache_fetched_at(cfg),
        "count": len(sel),
        "prs": [prquery.summarize(it) for it in sel],
    })


@query.command("show")
@click.argument("ref")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def query_show(ref, config_path):
    """Full detail for one PR (no diff text)."""
    cfg = _load_config_or_exit(config_path)
    items = prquery.load_items(cfg)
    _emit(prquery.detail(_resolve_or_exit(items, ref)))


@query.command("diff")
@click.argument("ref")
@click.option("--file", "files", multiple=True, help="Restrict to these paths (repeatable).")
@click.option("--changed-only", is_flag=True, help="Only files changed since my last review.")
@click.option("--max-chars", type=int, default=60000)
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def query_diff(ref, files, changed_only, max_chars, config_path):
    """Per-member diff text for one PR."""
    cfg = _load_config_or_exit(config_path)
    items = prquery.load_items(cfg)
    item = _resolve_or_exit(items, ref)
    _emit(prquery.get_diff_text(
        item, files=list(files) or None,
        changed_since_review_only=changed_only, max_chars=max_chars,
    ))


@query.command("history")
@click.option("--author")
@click.option("--module")
@click.option("--verdict")
@click.option("--limit", type=int, default=50)
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def query_history(author, module, verdict, limit, config_path):
    """Archived review history as triage rows."""
    cfg = _load_config_or_exit(config_path)
    items = prquery.load_items(cfg)
    _emit(prquery.review_history(items, author=author, module=module,
                                 verdict=verdict, limit=limit))


@query.command("stats")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def query_stats(config_path):
    """Pending / archived counts."""
    cfg = _load_config_or_exit(config_path)
    items = prquery.load_items(cfg)
    _emit(prquery.stats(items))


@query.command("tracked")
@click.argument("ref", required=False)
@click.option("--state", type=click.Choice(["all", "open", "resolved"]), default="all")
@click.option("--include-dismissed", is_flag=True)
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def query_tracked(ref, state, include_dismissed, config_path):
    """List watched PRs, or show one in full with REF."""
    cfg = _load_config_or_exit(config_path)
    items = prquery.load_tracked(cfg, include_dismissed=include_dismissed)
    if ref:
        try:
            _emit(prquery.tracked_detail(prquery.resolve_tracked(items, ref)))
        except ValueError as e:
            click.echo(str(e), err=True)
            sys.exit(1)
        return
    if state == "open":
        items = [t for t in items if t["state"] not in ("MERGED", "CLOSED")]
    elif state == "resolved":
        items = [t for t in items if t["state"] in ("MERGED", "CLOSED")]
    _emit({"count": len(items),
           "tracked": [prquery.summarize_tracked(t) for t in items]})


def _open_html(html_path: Path) -> None:
    try:
        subprocess.Popen(
            ["xdg-open", str(html_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        console.print(f"[yellow]Open manually: file://{html_path}[/yellow]")


def _run_refresh(conn, cfg, *, force: bool, cron: bool = False) -> None:
    with _progress(cron) as progress:
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

            pr_row, modules, reviewers, threads, comments = _node_to_rows(
                node, cfg.github_login,
            )

            with db.transaction(conn):
                db.upsert_pr(conn, pr_row)
                db.replace_modules(conn, pr_id, modules)
                db.replace_reviewers(conn, pr_id, reviewers)
                db.replace_threads(conn, pr_id, threads)
                db.replace_comments(conn, pr_id, comments)

            # Patch fetch (if head_sha not cached)
            _store_patch(conn, cfg, pr_row, force=force)

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

        # Prime open pair-siblings I already reviewed (so no longer in the
        # review-requested search): their threads/reviews/comments are fetched
        # nowhere else. Adds them to kept_ids so the sweep leaves them be.
        progress.update(task, description="Priming reviewed pair siblings...")
        _prime_reviewed_siblings(conn, cfg, kept_ids, force=force)

        # Before sweeping, rescue delete-candidates whose cached
        # previously_reviewed=0 is stale: a PR I just approved/changes-requested
        # drops out of `review-requested:` immediately, so its last fetch
        # predates my review. Verify those against GitHub and flip the ones I
        # actually reviewed so the sweep archives (not deletes) them.
        progress.update(task, description="Reconciling PRs that left the queue...")
        reconciled = _reconcile_reviewed(conn, kept_ids, cfg.github_login)

        # Sweep PRs that fell out of the result set: archive ones I reviewed,
        # delete ones I never reviewed. Skip deletes if reconciliation failed
        # this run, so an unverified PR is never lost to a transient error.
        archived, deleted = db.sweep(
            conn, kept_ids, derive.now_utc(), delete=reconciled,
        )
        if archived or deleted:
            log.debug("swept: %d archived, %d deleted", archived, deleted)

        # Archived halves of still-active pairs may have genuinely closed since
        # they left the request set (the search only returns open PRs, so their
        # cached state never updates). Re-check just those so the UI can tell
        # "closed on GitHub" apart from "merely reviewed by me".
        progress.update(task, description="Re-checking archived siblings...")
        _reconcile_sibling_states(conn, cfg, kept_ids)

        # Attach each bundle's migration PR. Before the review queue is built, so
        # the AI pass is told whether one exists rather than inferring from a
        # diff that could never contain it.
        progress.update(task, description="Matching migration PRs...")
        searched_repo = _refresh_companions(conn, cfg, kept_ids)

        if cfg.ai.enabled and cfg.ai.review_enabled:
            review_candidates, review_context = _build_review_queue(
                conn, kept_ids, cfg.ai.review_max_diff_chars,
                companion_repo=searched_repo,
                max_attempts=cfg.ai.max_attempts,
                limit=cfg.ai.cron_max_reviews if cron else None,
            )
            if review_candidates:
                progress.update(
                    task,
                    description=f"AI sanity-check {len(review_candidates)} small PRs...",
                )
                outcomes = ai.review_batch(
                    review_candidates, timeout=cfg.ai.timeout_seconds,
                    model=cfg.ai.model, max_diff_chars=cfg.ai.review_max_diff_chars,
                )
                _store_reviews(conn, outcomes, review_context)


def _store_reviews(conn, outcomes: list, review_context: dict) -> None:
    """Persist each outcome: the review itself, or the failure that stands in for it."""
    now = derive.now_utc()
    for outcome in outcomes:
        sibling_sha, companion_sha = review_context.get(outcome.head_sha, ("", ""))
        if outcome.result is not None:
            rev = outcome.result
            with db.transaction(conn):
                db.upsert_ai_review(
                    conn, outcome.head_sha, sibling_sha,
                    rev.summary, json.dumps(rev.concerns),
                    rev.verdict, now, companion_head_sha=companion_sha,
                )
                db.clear_ai_attempt(conn, outcome.head_sha, sibling_sha, companion_sha)
        elif outcome.reason != "cli-missing":
            # An absent CLI is the batch's problem, not this PR's or its budget's.
            db.record_ai_attempt(conn, outcome.head_sha, sibling_sha, companion_sha,
                                 outcome.reason, now)


def _store_patch(conn, cfg, pr_row: dict, *, force: bool) -> None:
    """Cache the combined diff for pr_row's head sha, unless already cached.

    Keyed by head sha, so a PR that moved head needs this again even though its
    old diff is still on disk - without it the new row points at a sha with no
    patch and every diff consumer silently reports "unavailable".

    Over the size thresholds the diff is compacted rather than dropped: one huge
    data file used to cost the whole PR its cached diff and its AI first pass.
    `truncated` then means "what is stored is not the whole diff", which is what
    the dashboard says with it.

    diff_max_files stays an all-or-nothing bail. Compaction only removes depth -
    a PR spread over hundreds of files is wide, not deep, so there is nothing
    for it to take out, and bailing before the fetch is what keeps a megabyte we
    would only throw away off the wire.
    """
    head_sha = pr_row["head_sha"]
    if db.get_diff(conn, head_sha) is not None and not force:
        return
    if pr_row["changed_files"] > cfg.thresholds.diff_max_files:
        db.upsert_diff(conn, head_sha, None, True, derive.now_utc())
        return
    patch = github.fetch_patch(pr_row["repo"], pr_row["number"])
    truncated = False
    if patch is not None and (
        patch.count("\n") > cfg.thresholds.diff_max_lines
        or len(patch) > cfg.thresholds.diff_max_bytes
    ):
        # Noise is stubbed, not dropped, so the dashboard's file list still
        # shows every file the PR touches; the review path drops those stubs.
        compacted = derive.compact_diff(
            patch, repo=pr_row["repo"], number=pr_row["number"], stub_noise=True,
        )
        patch, truncated = compacted.text, compacted.partial
        # Still enormous with every oversized file taken out: a diff no one -
        # not the model, not diff2html, not the page embedding it - can use,
        # only now measured on what compaction could not shrink.
        if len(patch) > cfg.thresholds.diff_max_bytes:
            patch, truncated = None, True
    db.upsert_diff(conn, head_sha, patch, truncated, derive.now_utc())


def _refresh_pr_rows(conn, cfg, targets: list[dict], *, force: bool,
                     kept_ids: set[str] | None = None) -> int:
    """Re-fetch full nodes for already-cached rows and re-persist them in place.

    Shared by the two populations the review-requested search cannot return: open
    pair-siblings I already reviewed, and archived-but-open rows whose head or
    discussion moved after I reviewed. Both must keep their archived/reviewed
    bookkeeping, so this refreshes content only - it never resurrects a row into
    the pending queue.

    Returns the number of rows re-persisted.
    """
    if not targets:
        return 0
    by_id = {r["id"]: r for r in targets}
    refs = [(r["repo"], r["number"], r["id"]) for r in targets]
    try:
        nodes = github.fetch_pr_nodes(refs)
    except github.GithubError as e:
        log.warning("row refresh failed (%s); keeping cached data", e)
        return 0

    refreshed = 0
    for node in nodes:
        pr_id = _node_id(node)
        cached_row = by_id.get(pr_id)
        # Kept regardless of whether we re-persist, so the sweep never archives a
        # reviewed-open sibling out of an active pair.
        if kept_ids is not None:
            kept_ids.add(pr_id)
        if not _sibling_needs_refresh(
            cached_row, node, db.has_comments(conn, pr_id), force=force,
        ):
            continue

        pr_row, modules, reviewers, threads, comments = _node_to_rows(
            node, cfg.github_login,
        )
        # These rows left the request queue because I already reviewed them; the
        # timeline (last:30) may no longer carry that review. Keep the cached
        # previously_reviewed so the sweep never mistakes one for an un-reviewed,
        # deletable row, and keep archived_at so refreshing content does not
        # resurrect the row into the pending queue.
        if cached_row:
            pr_row["previously_reviewed"] = (
                cached_row["previously_reviewed"] or pr_row["previously_reviewed"]
            )
            pr_row["archived_at"] = cached_row["archived_at"]
        with db.transaction(conn):
            db.upsert_pr(conn, pr_row)
            db.replace_modules(conn, pr_id, modules)
            db.replace_reviewers(conn, pr_id, reviewers)
            db.replace_threads(conn, pr_id, threads)
            db.replace_comments(conn, pr_id, comments)
        _store_patch(conn, cfg, pr_row, force=force)
        # Complexity is keyed by head sha, so a moved head leaves the row with no
        # score and a bucket silently defaulted to M. AI review is deliberately
        # not re-run: that budget is for PRs still in my queue.
        score = derive.heuristic_score(
            additions=pr_row["additions"],
            deletions=pr_row["deletions"],
            changed_files=pr_row["changed_files"],
            modules=modules,
            unresolved_threads=pr_row["unresolved_threads"],
            previously_reviewed=bool(pr_row["previously_reviewed"]),
        )
        db.upsert_complexity(conn, {
            "head_sha": pr_row["head_sha"],
            "bucket": derive.bucket_for(
                score, cfg.buckets.M, cfg.buckets.L, cfg.buckets.XL,
            ),
            "score": score,
            "method": "heuristic",
            "notes": None,
            "computed_at": derive.now_utc(),
        })
        refreshed += 1
    return refreshed


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


def _reconcile_sibling_states(conn, cfg, kept_ids: set[str]) -> None:
    """Refresh archived-but-still-OPEN rows: their GitHub state and any informal
    "please re-review" ping the author left after my last review.

    The sweep archives whatever fell out of the search without asking GitHub why,
    so a closed-without-my-review PR would otherwise stay OPEN in the history
    forever, and a re-review ping with no formal re-request would be invisible to
    all tooling. This is exactly the archived-and-OPEN population, in one batched
    request; the set self-shrinks as rows flip to CLOSED/MERGED. Best-effort: on
    a GitHub failure the cached state and pings stay.
    """
    prs = [dict(r) for r in db.list_prs(conn)]
    stale = [
        p for p in prs
        if p["id"] not in kept_ids and p["archived_at"] and p["state"] == "OPEN"
    ]
    if not stale:
        return
    refs = [(p["repo"], p["number"], p["id"]) for p in stale]
    try:
        activity = github.fetch_archived_activity(refs)
    except github.GithubError as e:
        log.warning("archived activity check failed (%s); keeping cached data", e)
        return

    # Hidden PRs never carry a ping - hiding is the dismissal for dead PRs the
    # user won't close (a push auto-unhides, resurfacing a revived PR). Archived
    # rows never get their cached head_sha refreshed, so the auto-unhide must
    # compare against the live sha from this fetch, not the stale DB one.
    def _live_sha(p: dict) -> str:
        node = activity.get(p["id"]) or {}
        return node.get("headRefOid") or p["head_sha"]

    # A hide is keyed on every member's sha (hidden.item_sha), so the pseudo
    # items handed to prune must carry the whole pair - a one-member stand-in
    # would never match a pair's stored value and would silently un-hide it.
    pairs = derive.detect_pairs(prs)
    by_id = {p["id"]: p for p in prs}
    hidden_ids = set(hidden.prune(hidden.load(cfg), [
        {
            "id": p["id"],
            "head_sha": _live_sha(p),
            "members": [
                {"head_sha": _live_sha(m)}
                for m in (p, by_id.get(pairs.get(p["id"]) or ""))
                if m is not None
            ],
        }
        for p in stale
    ]))

    closed = pinged = pushed = 0
    outdated: list[dict] = []
    with db.transaction(conn):
        for p in stale:
            node = activity.get(p["id"])
            if node is None:
                continue
            new_state = node.get("state") or "OPEN"
            if new_state != p["state"]:
                db.set_pr_state(conn, p["id"], new_state)
                if new_state != "OPEN":
                    closed += 1
            ping = None
            push = None
            if new_state == "OPEN" and p["id"] not in hidden_ids:
                ping = derive.detect_review_ping(node, cfg.github_login)
                push = derive.detect_push_since_review(node, cfg.github_login)
            if ping:
                db.set_ping(conn, p["id"], ping["ping_at"], ping["ping_author"],
                            derive.comment_snippet(ping.get("ping_body")))
                pinged += 1
            elif p["ping_at"]:
                db.set_ping(conn, p["id"], None, None, None)
            if push:
                db.set_push(conn, p["id"], push["push_at"], push["push_sha"])
                pushed += 1
            elif p["push_at"]:
                db.set_push(conn, p["id"], None, None)

            # A moved head or a bumped updatedAt means everything cached for this
            # row - diff, reviews, threads, file list, complexity - predates what
            # is on GitHub now. Nothing else refreshes an archived row, so
            # without this it stays a fossil of the moment I reviewed it.
            if new_state == "OPEN" and (
                (node.get("headRefOid") or p["head_sha"]) != p["head_sha"]
                or (node.get("updatedAt") or p["updated_at"]) != p["updated_at"]
            ):
                outdated.append(p)
    if closed or pinged or pushed:
        log.debug("archived reconcile: %d closed, %d pinged, %d pushed",
                  closed, pinged, pushed)

    # Outside the transaction above: this re-fetches over the network and opens
    # its own per-row transactions.
    refreshed = _refresh_pr_rows(conn, cfg, outdated, force=False)
    if refreshed:
        log.debug("refreshed %d stale archived rows", refreshed)


def _reviewed_open_siblings(cached: list[dict], active_ids: set[str]) -> list[dict]:
    """Cached rows that are open (GitHub state) and the pair-sibling of a PR in
    this run's active set, yet absent from it themselves - the halves I already
    reviewed, which the review-requested search no longer returns. No other
    refresh path fetches their threads/reviews/comments. Local archived_at is
    deliberately ignored: the sweep archives a reviewed half on the very next
    refresh, so requiring non-archived would exclude nearly every real case."""
    pairs = derive.detect_pairs(cached)
    out = []
    for row in cached:
        pid = row["id"]
        sib = pairs.get(pid)
        if (sib in active_ids and pid not in active_ids
                and row.get("state") == "OPEN"):
            out.append(row)
    return out


def _sibling_needs_refresh(cached_row, node: dict, has_comments: bool, *,
                           force: bool) -> bool:
    """Whether a primed sibling's node must be re-persisted: on force, when never
    cached, when it has no comment rows yet (so pre-feature rows are primed once),
    or when its head/updated_at moved."""
    if force or cached_row is None or not has_comments:
        return True
    return (cached_row["head_sha"] != node["headRefOid"]
            or cached_row["updated_at"] != node["updatedAt"])


def _prime_reviewed_siblings(conn, cfg, kept_ids: set[str], *, force: bool) -> None:
    cached = [dict(r) for r in db.list_prs(conn)]
    targets = _reviewed_open_siblings(cached, kept_ids)
    primed = _refresh_pr_rows(conn, cfg, targets, force=force, kept_ids=kept_ids)
    if primed:
        log.debug("primed %d reviewed-open pair-siblings", primed)


def _refresh_companions(conn, cfg, kept_ids: set[str]) -> str:
    """Attach the bundle's migration PR to every cached PR sharing its head
    branch, and cache its diff. Returns the repo actually searched, or "" when
    the lookup did not run - the AI prompt may only call a migration absent when
    it has been checked for.

    A change that moves data between modules ships its upgrade script in a third
    repo, which appears in no addons diff and requests no reviewer - so "this
    data move has no migration" keeps being raised against changes that have one.
    robodoo groups a bundle by head branch name and runbot matches its members
    the same way, so that name is the key.

    The author deliberately is not part of it: the migration is regularly written
    by someone other than the author of the half it migrates.

    Best-effort end to end. The upgrade repo is private, so a reviewer without
    access - or an offline moment - has to end up with no companions rather than
    a failed refresh.
    """
    if not (cfg.companion.enabled and cfg.companion.repo):
        return ""
    repo = cfg.companion.repo
    try:
        by_branch = github.list_open_prs_by_head_branch(repo)
    except github.GithubError as e:
        log.debug("companion lookup skipped (%s): %s", repo, e)
        return ""

    now = derive.now_utc()
    stored = db.list_companions(conn)
    matched: dict[str, dict] = {}
    dropped: list[str] = []
    # By companion number, not by pr_id: both halves of a pair point at the same
    # migration and must not disagree about what became of it.
    vanished: set[int] = set()
    for pr in db.list_prs(conn):
        pr_id, branch = pr["id"], pr["head_branch"]
        if pr["repo"] == repo:
            continue
        entry = by_branch.get(branch)
        prev = stored.get(pr_id)
        if entry:
            matched[pr_id] = entry
        elif prev is None:
            continue
        elif prev["head_branch"] != branch:
            # The half was force-pushed onto another branch; whatever migration
            # was attached belongs to a change this PR no longer is.
            dropped.append(pr_id)
        elif prev["state"] == "OPEN":
            # The migration left the open listing while the half it migrates is
            # still here: upgrade PRs have their own review flow and are often
            # merged ahead of their bundle. Dropping the row would put the false
            # positive straight back, so re-check what became of it instead.
            vanished.add(prev["number"])

    states: dict[int, str] = {}
    if vanished:
        try:
            states = github.fetch_pr_states(repo, sorted(vanished))
        except github.GithubError as e:
            log.debug("companion state re-check skipped: %s", e)

    with db.transaction(conn):
        for pr_id in dropped:
            db.delete_companion(conn, pr_id)
        for pr_id, entry in matched.items():
            db.upsert_companion(conn, pr_id, {
                "repo": repo,
                "number": entry["number"],
                "url": entry["url"],
                "title": entry.get("title") or "",
                "author": entry.get("author") or "",
                "state": (entry.get("state") or "open").upper(),
                "is_draft": int(bool(entry.get("draft"))),
                "head_branch": entry["head_branch"],
                "head_sha": entry.get("head_sha") or "",
                "fetched_at": now,
            })
        for number, state in states.items():
            db.set_companion_state(conn, repo, number, state)

    # Only the active queue's migrations need their diff fetched: it exists to be
    # prompt context, and the AI pass only runs on PRs still in the queue. One
    # migration serves both halves of a pair - _store_patch is keyed by head sha,
    # so the second half is a cache hit.
    for pr_id, entry in matched.items():
        if pr_id not in kept_ids:
            continue
        _store_patch(conn, cfg, {
            "repo": repo,
            "number": entry["number"],
            "head_sha": entry.get("head_sha") or "",
            # The listing endpoint carries no file count, and a migration is a
            # script rather than a wide change, so nothing is gated on breadth
            # here; the line/byte guards inside _store_patch still apply.
            "changed_files": 0,
        }, force=False)

    if matched or dropped:
        log.debug("companions: %d attached, %d dropped, %d re-checked",
                  len(matched), len(dropped), len(states))
    return repo


def _ai_attempt_exhausted(conn, head_sha: str, sibling_head_sha: str,
                          companion_head_sha: str, max_attempts: int, now) -> bool:
    """Whether this review context has failed too often, or too recently."""
    row = db.get_ai_attempt(conn, head_sha, sibling_head_sha, companion_head_sha)
    if row is None:
        return False
    if row["attempts"] >= max_attempts:
        return True
    hours = _AI_RETRY_BACKOFF_HOURS[min(row["attempts"], len(_AI_RETRY_BACKOFF_HOURS)) - 1]
    return derive.parse_iso(row["last_attempt_at"]) + timedelta(hours=hours) > now


def _build_review_queue(
    conn,
    kept_ids: set[str],
    max_diff_chars: int,
    *,
    companion_repo: str = "",
    max_attempts: int = 3,
    limit: int | None = None,
) -> tuple[list, dict[str, tuple[str, str]]]:
    """Build the AI sanity-check queue *after* all PR data is settled so pair
    detection is accurate. Gates per-PR on the actual diff size - a small-code
    breadth-XL reviews fine; a size-L with a huge diff would just truncate to
    noise.

    The gate measures the *compacted* diff, because that is what the prompt will
    hold: judging the raw one skipped PRs over bytes the model would never have
    been shown, and enterprise#125240 - 53% .po files the stripper already knew
    to drop - was skipped as a 178k diff over 84k of code. max_diff_chars is the
    prompt's own diff budget, so a PR is skipped only when it genuinely does not
    fit, and one that fits is never handed to the model half-cut.

    `companion_repo` is the repo _refresh_companions searched, passed through to
    the prompt so it can only claim a migration is missing when one was looked
    for.

    A context that already failed `max_attempts` times is left out for good, and
    one that failed fewer waits out its backoff. `limit` keeps a scheduled tick
    to the cheapest few, the rest landing on the next tick or a manual run.

    Returns (requests, head_sha -> (sibling_head_sha, companion_head_sha)) - the
    latter is the review's cache key, needed when persisting the result since
    ReviewRequest itself doesn't survive the AI call.
    """
    now = datetime.now(timezone.utc)
    pr_rows = {pr_id: db.get_cached_pr(conn, pr_id) for pr_id in kept_ids}
    pr_rows = {k: v for k, v in pr_rows.items() if v is not None}
    pair_map = derive.detect_pairs([dict(r) for r in pr_rows.values()])
    modules_by_pr = db.list_modules(conn)

    # (size, head_sha, request), sorted so a capped run takes the cheapest, in a fixed order.
    sized: list[tuple[int, str, ai.ReviewRequest]] = []
    review_context: dict[str, tuple[str, str]] = {}

    for pr_id, pr_row in pr_rows.items():
        head_sha = pr_row["head_sha"]
        diff_row = db.get_diff(conn, head_sha)
        diff_text = (diff_row["patch_text"] if diff_row else None) or ""
        if not diff_text:
            continue
        # The raw text travels on: _diff_for_prompt compacts it again (a no-op
        # on an already compacted diff) and needs to see what it omits to tell
        # the model, so handing over the compacted text would lose the note.
        compacted = derive.compact_diff(
            diff_text, repo=pr_row["repo"], number=pr_row["number"],
        )
        if not compacted.text or len(compacted.text) > max_diff_chars:
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

        # Unlike the sibling, a companion counts as context whether or not its
        # diff could be cached: the prompt changes on its mere existence, since
        # that is what decides whether a missing migration is worth flagging.
        companion_row = db.get_companion(conn, pr_id)
        companion_head_sha = companion_row["head_sha"] if companion_row else ""
        companion = None
        if companion_row:
            comp_diff_row = db.get_diff(conn, companion_row["head_sha"])
            companion = ai.Companion(
                repo=companion_row["repo"],
                number=companion_row["number"],
                title=companion_row["title"],
                state=companion_row["state"],
                diff=(comp_diff_row["patch_text"] if comp_diff_row else None) or "",
            )

        if db.has_manual_ai_review(conn, head_sha):
            continue
        if db.get_ai_review(
            conn, head_sha, sibling_head_sha, companion_head_sha,
        ) is not None:
            continue
        if _ai_attempt_exhausted(conn, head_sha, sibling_head_sha,
                                 companion_head_sha, max_attempts, now):
            continue

        sized.append((len(compacted.text), head_sha, ai.ReviewRequest(
            head_sha=head_sha,
            title=pr_row["title"],
            body=pr_row["body"] or "",
            modules=modules_by_pr.get(pr_id, []),
            branch=pr_row["target_branch"],
            diff=diff_text,
            repo=pr_row["repo"],
            number=pr_row["number"],
            companion=companion,
            companion_repo=companion_repo,
            **sibling_kwargs,
        )))
        review_context[head_sha] = (sibling_head_sha, companion_head_sha)

    sized.sort(key=lambda item: (item[0], item[1]))
    if limit is not None:
        sized = sized[:limit]
    requests = [r for _, _, r in sized]
    return requests, {k: review_context[k] for _, k, _ in sized}


def _node_id(node: dict) -> str:
    return f"{node['repository']['nameWithOwner']}#{node['number']}"


def _node_to_rows(
    node: dict, my_login: str,
) -> tuple[dict, list[str], list[dict], list[dict], list[dict]]:
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
    comments, my_pending_review = derive.derive_comments(node, my_login)

    latest_reviews = (node.get("latestReviews") or {}).get("nodes") or []
    review_requests = (node.get("reviewRequests") or {}).get("nodes") or []
    reviewers = derive.derive_reviewers(latest_reviews, review_requests)

    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    ci_state, runbot_url = derive.status_check_state(rollup)
    ci_failures = derive.failing_checks(rollup)

    parsed_task = derive.parse_linked_task(node.get("body"))
    task_kind, task_id = parsed_task if parsed_task else (None, None)

    pr_row = {
        "id": _node_id(node),
        "repo": repo,
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "author": (node.get("author") or {}).get("login") or "(unknown)",
        "is_draft": int(bool(node.get("isDraft"))),
        "target_branch": node["baseRefName"],
        "head_branch": node["headRefName"],
        "head_sha": node["headRefOid"],
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "review_requested_at": review_requested_at,
        "previously_reviewed": int(prev_reviewed),
        "mergeable": node.get("mergeable"),
        "ci_state": ci_state,
        "ci_failures": json.dumps(ci_failures) if ci_failures else None,
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
        "state": node.get("state") or "OPEN",
        "my_pending_review": int(my_pending_review),
        "fetched_at": derive.now_utc(),
    }
    return pr_row, modules, reviewers, th.threads, comments


if __name__ == "__main__":
    cli()
