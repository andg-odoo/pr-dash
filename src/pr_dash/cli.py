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
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
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
        last_queue = db.get_meta(conn, "last_queue_refresh")
        # Same minute of slack as the mine tab, a tick lands seconds short of the interval.
        queue_due = force or not cron or not last_queue or derive.parse_iso(last_queue) <= (
            datetime.now(UTC) - timedelta(minutes=cfg.thresholds.queue_interval_minutes - 1))
        try:
            if queue_due:
                with _progress(cron) as progress:
                    task = progress.add_task("", total=None)
                    sync.Sync(
                        conn, cfg, github.GhGitHub(),
                        on_phase=lambda text: progress.update(task, description=text),
                    ).refresh_queue(force=force, cron=cron)
                db.set_meta(conn, "last_queue_refresh", derive.now_utc())
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
            _run_mine_refresh(conn, cfg, force=force, cron=cron)
            # Stamped only on a run that reached GitHub, so the header dates the data.
            db.set_meta(conn, "last_refresh", derive.now_utc())

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
    nodes = github.fetch_nodes(refs, github.TRACKED_NODE_FRAGMENT)
    now = derive.now_utc()
    with db.transaction(conn):
        for pr_id, node in nodes.items():
            row, comments = derive.tracked_row_from_node(node, now)
            db.update_tab_state(conn, "tracked", pr_id, row)
            db.replace_tab_comments(conn, "tracked", pr_id, comments)
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


def _fetch_authored(refs: list[tuple[str, int]], known: list[dict], progress, task, *,
                    chunk_size: int = 50) -> tuple[dict[str, dict], dict[str, str]]:
    """Fetch Authored PR nodes and the Forward-ports confirmed against them, as (nodes, links)."""
    nodes = github.fetch_nodes(refs, github.MINE_NODE_FRAGMENT, chunk_size=chunk_size)
    # GitHub refuses author:fw-bot searches, so Forward-ports are found from their source.
    candidates = {c for node in nodes.values() for c in derive.forward_port_candidates(node)}
    candidates -= nodes.keys() | {r["id"] for r in known}
    if candidates:
        progress.update(task, description=f"Checking {len(candidates)} forward-ports...")
        fw_refs = (c.rpartition("#") for c in sorted(candidates))
        fw_nodes = github.fetch_nodes([(repo, int(n)) for repo, _, n in fw_refs],
                                      github.MINE_NODE_FRAGMENT, chunk_size=chunk_size)
    else:
        fw_nodes = {}
    sources = nodes.keys() - {r["id"] for r in known if r["source_id"]}
    links = {}
    for fw_id, node in fw_nodes.items():
        # A Forward-port of a Forward-port names every ancestor, its Source PR among them.
        source = next((a for a in derive.forward_port_ancestors(node["body"]) if a in sources),
                      None)
        if source:
            links[fw_id] = source
            nodes[fw_id] = node
    return nodes, links


def _store_authored(conn, nodes: dict[str, dict], links: dict[str, str], now: str) -> int:
    """Write fetched Authored PRs and their Forward-port links, returning how many are new."""
    added = 0
    for pr_id, node in nodes.items():
        repo, _, number = pr_id.rpartition("#")
        added += db.add_mine(conn, pr_id, repo, int(number), node["url"], now)
        row, comments = derive.mine_row_from_node(node, now)
        db.update_tab_state(conn, "mine", pr_id, row)
        db.replace_tab_comments(conn, "mine", pr_id, comments)
    for fw_id, source in links.items():
        db.link_mine_forward_port(conn, fw_id, source)
    return added


def _read_mergebot(pr_ids: list[str]) -> dict[str, mergebot.MergebotState]:
    """Read the Mergebot page of each `owner/repo#n` id."""
    refs = [pr_id.rpartition("#") for pr_id in pr_ids]
    # A page takes about a second, a few in flight keep the tick short without loading the bot.
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = pool.map(lambda ref: mergebot.fetch(ref[0], int(ref[2])), refs)
        return dict(zip(pr_ids, pages, strict=True))


def _run_mine_refresh(conn, cfg, *, force: bool, cron: bool = False) -> None:
    """Refresh the open and the undismissed Authored PRs, and their Mergebot pages."""
    known = db.list_mine(conn)
    last = max((r["fetched_at"] for r in known if r["fetched_at"]), default=None)
    # A timer tick lands seconds short of the threshold after the last one, hence the minute off.
    due = datetime.now(UTC) - timedelta(
        minutes=cfg.thresholds.mine_staleness_minutes - 1)
    if not force and last and derive.parse_iso(last) > due:
        return
    with _progress(cron) as progress:
        task = progress.add_task("Searching for authored PRs...", total=None)
        try:
            refs = github.search_authored_open(cfg.github_login)
            # Known PRs that left the open search are still fetched, so they can turn Done.
            refs = list(dict.fromkeys([*refs, *((r["repo"], r["number"]) for r in known)]))
            progress.update(task, description=f"Fetching {len(refs)} authored PRs...")
            nodes, links = _fetch_authored(refs, known, progress, task)
        except github.GithubError as e:
            _notify(cron, f"Authored PR refresh failed: {e}", "yellow")
            return
        with db.transaction(conn):
            added = _store_authored(conn, nodes, links, derive.now_utc())

        stored = db.list_mine_mergebot(conn)
        # A resolved PR's final Mergebot read cannot change, so it is not fetched again.
        unread = [
            r["id"] for r in db.list_mine(conn)
            if r["state"] == "OPEN"
            or stored.get(r["id"], {}).get("state") not in ("merged", "closed", "unmanaged")
        ]
        progress.update(task, description=f"Reading {len(unread)} Mergebot pages...")
        reads = _read_mergebot(unread)
        with db.transaction(conn):
            for pr_id, state in reads.items():
                db.upsert_mine_mergebot(conn, pr_id, dataclasses.asdict(state), derive.now_utc())
        sets, _ = render.build_mine_payload(conn, cfg.github_login)
        db.drop_stale_mine_acks(conn, {s["key"]: s["fingerprint"] for s in sets})

    _notify(cron, f"mine: +{added} new, {len(nodes)} refreshed, "
                  f"{len(reads)} Mergebot pages read", "dim")


@cli.command("import-history")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
def import_history(config_path):
    """Import every closed Authored PR once, dismissing the Branch sets already resolved."""
    cfg = _load_config_or_exit(config_path)
    # Run by hand, as minutes of fetching on a timer tick would hold the lock over later ticks.
    lock_fd = _acquire_refresh_lock(cfg, wait=True)
    try:
        conn = db.connect(cfg.db_path)
        if db.get_meta(conn, "mine_history_imported"):
            console.print("[yellow]History already imported; nothing to do.[/yellow]")
            return
        try:
            _import_mine_history(conn, cfg)
        except github.GithubError as e:
            console.print(f"[red]GitHub error: {e}[/red]")
            console.print("[yellow]Nothing was stored, run import-history again.[/yellow]")
            sys.exit(1)
        payload, _, _ = _render_from_cache(conn, cfg, offline=False)
        console.print(f"[green]Re-rendered {len(payload)} PRs → {cfg.html_path}[/green]")
    finally:
        os.close(lock_fd)


def _import_mine_history(conn, cfg) -> None:
    """Fetch the closed Authored PRs not in Mine yet, then store them in one transaction."""
    known = db.list_mine(conn, include_dismissed=True)
    known_ids = {r["id"] for r in known}
    with _progress(False) as progress:
        task = progress.add_task("Searching for closed authored PRs...", total=None)
        refs = [(repo, n) for repo, n in github.search_authored_closed(cfg.github_login)
                if f"{repo}#{n}" not in known_ids]
        progress.update(task, description=f"Fetching {len(refs)} closed authored PRs...")
        # A closed PR carries its whole discussion, and 50 of them overran GitHub's 10 s limit.
        nodes, links = _fetch_authored(refs, known, progress, task, chunk_size=10)
        progress.update(task, description=f"Reading {len(nodes)} Mergebot pages...")
        reads = _read_mergebot(list(nodes))

    now = derive.now_utc()
    with db.transaction(conn):
        _store_authored(conn, nodes, links, now)
        for pr_id, state in reads.items():
            db.upsert_mine_mergebot(conn, pr_id, dataclasses.asdict(state), now)
        sets, _ = render.build_mine_payload(conn, cfg.github_login)
        imported = [s for s in sets if not any(m["id"] in known_ids for m in s["members"])]
        # A Chain with an open Forward-port is not Done, so its set stays visible.
        resolved = [s for s in imported if s["band"] == "done"]
        for s in resolved:
            for m in s["members"]:
                db.set_dismissed(conn, "mine", m["id"], now)
        db.set_meta(conn, "mine_history_imported", now)

    unread = sum(state.state == "unknown" for state in reads.values())
    console.print(
        f"[green]Imported {len(nodes) - len(links)} closed PRs and {len(links)} "
        f"Forward-ports.[/green] {len(resolved)} resolved Branch sets dismissed, "
        f"{len(imported) - len(resolved)} left open"
        + (f", {unread} Mergebot pages unread." if unread else "."),
    )


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
