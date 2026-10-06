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
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from pr_dash import config, db, derive, github, hidden, mergebot, render, sync
from pr_dash import query as prquery

console = Console()
log = logging.getLogger("pr_dash")

# Room for many refreshes' worth of phase lines, without becoming a file to tidy by hand.
_CRON_LOG_MAX_BYTES = 1_000_000


class _LogProgress:
    """Progress stand-in for cron runs, where rich would write ANSI to a log."""

    def __enter__(self) -> _LogProgress:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def add_task(self, description: str, total=None) -> int:
        self.update(0, description)
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


def _sync(conn, cfg, progress) -> sync.Sync:
    """A Sync over the real GitHub, announcing its phases on `progress`."""
    task = progress.add_task("", total=None)
    return sync.Sync(conn, cfg, github.GhGitHub(),
                     on_phase=lambda text: progress.update(task, description=text))


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


def _render_from_cache(conn, cfg, *, offline=False):
    """Build the payload from cache and write the dashboard HTML, baking in the
    pruned server-side hidden map. Returns (payload, seen_updates,
    tab_seen_updates keyed by tab); the caller decides whether to advance the
    since-last-look baselines."""
    payload, seen_updates = render.build_payload(
        conn, cfg.github_login, cfg.repos, cfg.thresholds.stale_review_days,
        command_templates=dataclasses.asdict(cfg.commands),
        ai_max_attempts=cfg.ai.max_attempts,
    )
    for p in payload:
        p["my_login"] = cfg.github_login
    hidden_map = hidden.prune(hidden.load(cfg), payload)
    hidden.save(cfg, hidden_map)
    tracked, tracked_seen = render.build_tracked_payload(conn, include_dismissed=True)
    mine, mine_seen = render.build_mine_payload(conn, cfg.github_login)
    # Built apart so a dismissed member never joins a live set sharing its head branch.
    dismissed_mine, _ = render.build_mine_payload(conn, cfg.github_login, dismissed_only=True)
    render.render(payload, cfg.html_path, offline=offline,
                  last_refresh=db.get_meta(conn, "last_refresh"),
                  hidden_map=hidden_map, hidden_sync_port=cfg.hidden_sync_port,
                  tracked=tracked, mine=mine + dismissed_mine)
    return payload, seen_updates, {"tracked": tracked_seen, "mine": mine_seen}


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

    if not offline:
        try:
            with _progress(cron) as progress:
                report = _sync(conn, cfg, progress).refresh(force=force, cron=cron)
        except github.GithubError as e:
            if cron:
                # Re-rendering would put an offline-bannered dashboard over a good one.
                log.error("GitHub error: %s", e)
                sys.exit(1)
            console.print(f"[red]GitHub error: {e}[/red]")
            console.print("[yellow]Falling back to cached data.[/yellow]")
            offline = True
        else:
            for warning in report.warnings:
                _notify(cron, warning, "yellow")
            if report.tracked_added or report.tracked_refreshed:
                _notify(cron, f"tracked: +{report.tracked_added} new, {report.tracked_refreshed} "
                              f"refreshed ({report.tracked_total} total)", "dim")
            if report.mine_refreshed is not None:
                _notify(cron, f"mine: +{report.mine_added} new, {report.mine_refreshed} refreshed, "
                              f"{report.mergebot_read} Mergebot pages read", "dim")

    payload, seen_updates, tab_seen = _render_from_cache(conn, cfg, offline=offline)
    if cron:
        # Nobody looked, so advancing the baseline would hide changes never seen.
        log.info("rendered %d PRs -> %s", len(payload), cfg.html_path)
        return
    # Only now that the render succeeded do we advance the "last look" baseline,
    # so a render failure can't silently swallow the since-last-look deltas.
    render.commit_seen_baseline(conn, seen_updates, derive.now_utc())
    for tab, updates in tab_seen.items():
        render.commit_tab_seen_baseline(conn, tab, updates, derive.now_utc())
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
            fetched = sync.Sync(conn, cfg, github.GhGitHub()).fetch_tracked(parsed)
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


@cli.command("import-history")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def import_history(config_path):
    """Import every closed Authored PR once, dismissing the Branch sets already resolved."""
    cfg = _load_config_or_exit(config_path)
    # Run by hand, as minutes of fetching on a timer tick would hold the lock over later ticks.
    lock_fd = _acquire_refresh_lock(cfg, wait=True)
    try:
        conn = db.connect(cfg.db_path)
        try:
            with _progress(False) as progress:
                report = _sync(conn, cfg, progress).import_history()
        except github.GithubError as e:
            console.print(f"[red]GitHub error: {e}[/red]")
            console.print("[yellow]Nothing was stored, run import-history again.[/yellow]")
            sys.exit(1)
        if report is None:
            console.print("[yellow]History already imported; nothing to do.[/yellow]")
            return
        console.print(
            f"[green]Imported {report.closed} closed PRs and {report.forward_ports} "
            f"Forward-ports.[/green] {report.dismissed} resolved Branch sets dismissed, "
            f"{report.left_open} left open"
            + (f", {report.unread} Mergebot pages unread." if report.unread else "."),
        )
        payload, _, _ = _render_from_cache(conn, cfg, offline=False)
        console.print(f"[green]Re-rendered {len(payload)} PRs → {cfg.html_path}[/green]")
    finally:
        os.close(lock_fd)


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


@query.command("mine")
@click.option("--include-dismissed", is_flag=True)
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def query_mine(include_dismissed, config_path):
    """List Authored PRs as Branch sets."""
    cfg = _load_config_or_exit(config_path)
    sets = prquery.load_mine(cfg, include_dismissed=include_dismissed)
    _emit({"count": len(sets), "branch_sets": sets})


@query.command("mergebot")
@click.argument("ref")
def query_mergebot(ref):
    """Live Mergebot readiness for one PR (odoo#123, odoo/odoo#123 or a PR URL)."""
    try:
        repo, short, number = prquery._parse_ref(ref.replace("mergebot.odoo.com/", "github.com/"))
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    if repo is None and short is None:
        click.echo(f"{ref!r} names no repo, use e.g. odoo#{number}", err=True)
        sys.exit(1)
    # the Mergebot only serves repos of the odoo organization
    _emit(dataclasses.asdict(mergebot.fetch(repo or f"odoo/{short}", number)))


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


if __name__ == "__main__":
    cli()
