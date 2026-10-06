from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from pr_dash import ai, branch_set, config, db, derive, github, hidden, query

# stderr only: stdout is the MCP protocol channel, so a single stray print or
# rich.Console write there corrupts the stream. Everything human-facing goes to
# stderr; the one process that legitimately writes to stdout (a `pr-dash
# refresh`) is a subprocess with its output captured, never inherited.
log = logging.getLogger("pr_dash.mcp")

mcp = FastMCP("pr-dash")

_CONFIG_PATH: Path | None = None
_cfg: config.Config | None = None
_github: github.GitHub = github.GhGitHub()


def set_config_path(path: Path | None) -> None:
    """Point the server at a specific config file (the CLI uses this)."""
    global _CONFIG_PATH
    _CONFIG_PATH = path


def _config_path() -> Path | None:
    if _CONFIG_PATH is not None:
        return _CONFIG_PATH
    env = os.environ.get("PR_DASH_CONFIG")
    return Path(env) if env else None


def _get_cfg() -> config.Config:
    # Config is static for a session; load once. Items, in contrast, are rebuilt
    # on every tool call (below) so a parallel `pr-dash refresh` is picked up.
    global _cfg
    if _cfg is None:
        _cfg = config.load(_config_path())
    return _cfg


def _hidden_ids(cfg: config.Config, items: list[dict]) -> set[str]:
    """Pruned set of hidden PR ids, persisting the prune (auto-unhide on push)."""
    mapping = hidden.prune(hidden.load(cfg), items)
    hidden.save(cfg, mapping)
    return set(mapping)


def _ai_review_shas(item: dict, ref: str) -> tuple[str, str, str]:
    """(head_sha, context_heads, companion_head_sha) of the half `ref` names, else the primary's."""
    repo_full, repo_short, number = query._parse_ref(ref)
    bset = branch_set.from_item(item)
    target = next(
        (m for m in bset.halves if query._member_matches(m, repo_full, repo_short, number)),
        bset.primary,
    )
    cached = {(d["repo_short"], d["number"]) for d in item["diffs"] if d["diff"]}
    return (target["head_sha"],
            bset.context_heads(target, lambda h: (h["repo_short"], h["number"]) in cached),
            (item["companion"] or {}).get("head_sha") or "")


# --- dashboard re-render -----------------------------------------------------

# The dashboard is a static file, so a tool that writes to the cache leaves it
# stale until something re-renders. A render is cheap but not free (a subprocess
# and a full pass over the payload) and recording several reviews in a row is the
# normal case, so renders are coalesced two ways: a trailing timer within this
# process, and a lock file across processes, since every agent session runs its
# own MCP server.
#
# Two files carry the state between those processes: every caller appends a byte
# to `request`, whose size is therefore a request counter (POSIX makes a small
# O_APPEND write atomic, so no lock is needed to bump it), and the renderer
# records in `covered` the count it has rendered. requests > covered is work not
# yet in the HTML - what both the "should I bother" check and the post-render
# re-check read. Counters rather than timestamps because file mtimes come from a
# coarse kernel clock: two events milliseconds apart can land on the same mtime,
# which would silently drop the second one.
_RERENDER_DEBOUNCE_S = 0.4
# Bounds the re-check loop under a steady stream of writes; whatever is left is
# picked up by the next caller's timer.
_RERENDER_MAX_PASSES = 3

_rerender_timer: threading.Timer | None = None
_rerender_timer_lock = threading.Lock()


def _rerender_paths(cfg: config.Config) -> tuple[Path, Path, Path]:
    return (cfg.cache_dir / "rerender.request",
            cfg.cache_dir / "rerender.covered",
            cfg.cache_dir / "rerender.lock")


def _requests(cfg: config.Config) -> int:
    request_path, _, _ = _rerender_paths(cfg)
    try:
        return request_path.stat().st_size
    except OSError:
        return 0


def _covered(cfg: config.Config) -> int:
    _, covered_path, _ = _rerender_paths(cfg)
    try:
        return int(covered_path.read_text())
    except (OSError, ValueError):
        return 0


def _set_covered(cfg: config.Config, count: int) -> None:
    """Written under the render lock, but atomically all the same - a reader that
    catches a half-written file would render one time too few."""
    _, covered_path, _ = _rerender_paths(cfg)
    tmp = covered_path.with_name(covered_path.name + ".tmp")
    tmp.write_text(str(count))
    os.replace(tmp, covered_path)


def _schedule_rerender(cfg: config.Config) -> None:
    """Record that the cache changed and arm the debounce timer."""
    request_path, _, _ = _rerender_paths(cfg)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, b"\x01")
    finally:
        os.close(fd)
    _arm_rerender(cfg)


def _arm_rerender(cfg: config.Config) -> None:
    """(Re-)start the trailing timer, so a burst of writes renders once."""
    global _rerender_timer
    with _rerender_timer_lock:
        if _rerender_timer is not None:
            _rerender_timer.cancel()
        _rerender_timer = threading.Timer(_RERENDER_DEBOUNCE_S, _rerender, (cfg,))
        _rerender_timer.daemon = True
        _rerender_timer.start()


def _rerender(cfg: config.Config) -> None:
    """Render the dashboard if the cache has moved since the last render.

    Only one process renders at a time. A caller that loses the lock re-arms its
    timer instead of waiting: by the time it fires the holder has usually covered
    the request already, which the counter check sees and skips.
    """
    _, _, lock_path = _rerender_paths(cfg)
    if _requests(cfg) <= _covered(cfg):
        return
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        log.warning("dashboard re-render skipped: %s", e)
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _arm_rerender(cfg)
            return
        for _ in range(_RERENDER_MAX_PASSES):
            # Read the counter before rendering: a render reads the cache once,
            # at the start, so anything committed while it runs (by us or by
            # another session) is not in the HTML it writes and must not be
            # counted as covered.
            seen = _requests(cfg)
            if not _run_rerender():
                return
            _set_covered(cfg, seen)
            if _requests(cfg) <= seen:
                return
    finally:
        os.close(fd)


def _run_rerender() -> bool:
    """Shell out to `pr-dash rerender`, the way the refresh tool does - the
    render's stdout must stay out of the MCP protocol channel."""
    cmd = [sys.executable, "-m", "pr_dash", "rerender", "--no-open"]
    cfg_path = _config_path()
    if cfg_path is not None:
        cmd += ["--config", str(cfg_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("dashboard re-render failed: %s", e)
        return False
    if proc.returncode != 0:
        log.warning("dashboard re-render failed: %s", proc.stderr[-500:].strip())
        return False
    return True


@mcp.tool()
def list_prs(status: str = "pending", include_hidden: bool = False) -> dict:
    """List PRs as compact triage rows, in dashboard order (worst bucket / oldest
    request first).

    status: 'pending' (default, not yet reviewed) | 'archived' | 'all'.
    include_hidden: PRs you've hidden on the dashboard are excluded from pending
    results by default; set True to include them (each is marked "hidden": true).
    Returns {cache_fetched_at, count, prs}.

    Flags: RE=re-review requested, MSG=awaiting my reply, CI!=failing CI,
    CFL=merge conflict, OLD=stale request, PEND!=you have an unsent (PENDING)
    review draft, PING=an archived-but-open PR where the author informally asked
    for a re-review after your last review (no formal re-request), PUSH=an
    archived-but-open PR whose head moved after your review (no action implied -
    it marks that the cached diff is no longer what you reviewed). Buckets
    S/M/L/XL = rough complexity. Rows also carry my_pending_review, pinged rows
    carry ping_at/ping_author/ping_snippet, and pushed rows push_at/push_sha.

    `companion` is the bundle's migration PR in odoo/upgrade, matched by head
    branch - it never shows up in the diff, so a null companion is the evidence
    that a data move has no upgrade script, and a non-null one is why a "missing
    migration" concern would be wrong.
    """
    cfg = _get_cfg()
    items = query.load_items(cfg)
    hidden_ids = _hidden_ids(cfg, items)
    if status == "pending":
        sel = [it for it in items if not it.get("is_archived")]
    elif status == "archived":
        sel = [it for it in items if it.get("is_archived")]
    elif status == "all":
        sel = list(items)
    else:
        raise ValueError(f"status must be 'pending', 'archived', or 'all', got {status!r}")
    if not include_hidden:
        # Hiding only applies to the active queue; archived rows are never filtered.
        sel = [it for it in sel if it.get("is_archived") or it["id"] not in hidden_ids]
    prs = []
    for it in sel:
        row = query.summarize(it)
        if it["id"] in hidden_ids:
            row["hidden"] = True
        prs.append(row)
    return {
        "cache_fetched_at": query.cache_fetched_at(cfg),
        "count": len(prs),
        "prs": prs,
    }


@mcp.tool()
def get_pr(ref: str) -> dict:
    """Full detail for one PR: body, threads, reviewers, ci_failures, ai_reviews,
    commands, task links, companion migration PR, and per-file diff metadata (no
    diff text - use get_diff for that).

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    An enterprise number resolves to its odoo+enterprise pair.

    `companion` is the bundle's migration PR in odoo/upgrade (repo, number, url,
    title, state, head_sha), matched by head branch. Its diff is in no repo this
    PR touches, so before flagging a data move as unmigrated, check it: null
    means there genuinely is no upgrade script.

    Flags: RE=re-review requested, MSG=awaiting my reply, CI!=failing CI,
    CFL=merge conflict, OLD=stale request. Buckets S/M/L/XL = rough complexity.
    my_pending_review is true when you have an unsent review draft (get_comments
    shows its body). Output stays slim - no comment bodies here.

    A ref naming no review-queue PR but one of your Authored PRs returns its
    Branch set instead, as get_mine does but without the discussion.
    """
    cfg = _get_cfg()
    items = query.load_items(cfg)
    authored = query.resolve_authored(cfg, items, ref)
    if authored is not None:
        members = [
            {**{k: v for k, v in m.items() if k != "discussion"},
             "fw": [{k: v for k, v in f.items() if k != "discussion"} for f in m["fw"]]}
            for m in authored["members"]
        ]
        return {**authored, "members": members}
    return query.detail(query.resolve_item(items, ref))


@mcp.tool()
def get_comments(ref: str) -> dict:
    """Full comment/review/thread data for one PR (both halves of a pair).

    Per member: threads (grouped, each with is_resolved, path and its ordered
    comments incl. full bodies), reviews (submissions with author, state,
    submitted_at, body - PENDING entries are unsent drafts flagged pending: true,
    visible only to their own author), and conversation (top-level PR comments).
    Bot authors (robodoo, fw-bot, *[bot]) carry bot: true so you can filter them.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    A ref naming no review-queue PR but one of your Authored PRs returns its
    Branch set instead, the same output as get_mine.
    """
    cfg = _get_cfg()
    items = query.load_items(cfg)
    authored = query.resolve_authored(cfg, items, ref)
    if authored is not None:
        return authored
    return query.get_comments(cfg, query.resolve_item(items, ref))


@mcp.tool()
def list_tracked(state: str = "all", include_dismissed: bool = False) -> dict:
    """List tracked PRs - ones being *watched*, not reviewed.

    These are separate from the review queue: nobody asked you to review them,
    they were subscribed to on GitHub or added with `pr-dash track`. Use this to
    answer "what am I keeping an eye on" and "did any of it land".

    state: 'all' (default) | 'open' | 'resolved' (merged or closed).
    include_dismissed: dismissed rows are excluded by default; True adds them
    back carrying dismissed_at.

    Rows are active-first, newest activity first. since_last_look holds what
    moved since the dashboard was last rendered: 'resolved' (merged/closed),
    'reopened', 'pushed', 'reply', 'new'. Returns {cache_fetched_at, count,
    tracked}.
    """
    cfg = _get_cfg()
    items = query.load_tracked(cfg, include_dismissed=include_dismissed)
    if state == "open":
        sel = [t for t in items if t["state"] not in ("MERGED", "CLOSED")]
    elif state == "resolved":
        sel = [t for t in items if t["state"] in ("MERGED", "CLOSED")]
    elif state == "all":
        sel = items
    else:
        raise ValueError(
            f"state must be 'all', 'open', or 'resolved', got {state!r}"
        )
    return {
        "cache_fetched_at": query.cache_fetched_at(cfg),
        "count": len(sel),
        "tracked": [query.summarize_tracked(t) for t in sel],
    }


@mcp.tool()
def get_tracked(ref: str) -> dict:
    """Full detail for one tracked PR: body plus its merged discussion stream.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.

    Discussion merges conversation comments, review submissions and inline
    review threads, oldest first. Each entry carries kind ('issue' | 'review' |
    'thread'); review entries carry state (APPROVED / CHANGES_REQUESTED /
    COMMENTED / DISMISSED) and thread entries carry path plus thread_id and
    parent_id, so threads can be regrouped under the review that opened them.

    Only a recent window is cached (last 10 reviews, last 15 threads), so
    review_count / thread_count can exceed what appears here on a busy PR.
    """
    cfg = _get_cfg()
    items = query.load_tracked(cfg, include_dismissed=True)
    return query.tracked_detail(query.resolve_tracked(items, ref))


@mcp.tool()
def list_mine(band: str | None = None, include_dismissed: bool = False) -> dict:
    """List your Authored PRs as Branch sets - PRs sharing one head branch across
    repos, one row per set, Needs you first (oldest Action item first), then Open by
    most recent activity, then Done.

    Use this to answer "what needs doing on my PRs" instead of shelling out to gh.

    band: only sets in this band ('needs', 'open' or 'done'), every band by default.
    include_dismissed: dismissed members are excluded by default; True adds them
    back carrying dismissed_at.

    Each set carries key (the head branch), task, title and url (the primary's), band
    ('needs', 'open' or 'done'), actions [{member, kind, text, since}] (what waits on you: thread, ci,
    conflict, changes, reviewers, linked, fw), fyi labels (movement since the last
    look, plus 'idle Nd', 'waiting on re-review', 'source merged' and 'fw k/n merged'),
    acknowledged, and members. Members
    carry repo, num, state, ci (green / red / pending) with ci_failing and
    override (Mergebot Overrides), decision (GitHub review), r_plus, requested
    people and teams, conflict, and mergebot_unknown when the Mergebot page could
    not be read, plus fw: the member's Forward-ports in target branch order, each with
    ref, base (target branch), state, ci and flag (conflict / red / null). A set is
    Done only once every member and Forward-port is. Returns {cache_fetched_at, count,
    branch_sets}.
    """
    cfg = _get_cfg()
    sets = query.load_mine(cfg, include_dismissed=include_dismissed)
    if band is not None:
        sets = [s for s in sets if s["band"] == band]
    return {
        "cache_fetched_at": query.cache_fetched_at(cfg),
        "count": len(sets),
        "branch_sets": sets,
    }


@mcp.tool()
def get_mine(ref: str) -> dict:
    """Full detail for the Branch set holding one of your Authored PRs: every
    member with its body and merged discussion stream, each Forward-port with its own stream.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL,
    naming any member of the set or one of their Forward-ports.

    Discussion entries have the get_tracked shape: kind ('issue' | 'review' |
    'thread'), review state, and thread path, thread_id and parent_id.
    """
    cfg = _get_cfg()
    sets = query.load_mine(cfg, include_dismissed=True)
    found = query.resolve_mine(sets, ref)
    if found is None:
        known = ", ".join(f"{m['repo'].split('/')[-1]}#{m['num']}"
                          for s in sets[:20] for m in s["members"])
        raise ValueError(f"No Authored PR matches {ref!r}. Authored: {known or '(none)'}")
    return query.mine_detail(cfg, found)


@mcp.tool()
def hide_pr(ref: str) -> dict:
    """Hide a PR from the pending queue (same as the dashboard's × button). It
    stays hidden until a head commit changes - a push to any code half of its
    Branch set auto-unhides it.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    Returns {id, hidden: true, hidden_count}.
    """
    cfg = _get_cfg()
    item = query.resolve_item(query.load_items(cfg), ref)
    # An archived row's cached sha can be long stale; a hide recorded at it
    # would auto-unhide against the live sha immediately. Best-effort live
    # lookup per member, cached shas as the offline fallback.
    members = []
    for member in item.get("members") or []:
        live = None
        try:
            live = _github.head_sha(member.get("repo") or "", member.get("number") or 0)
        except (github.GithubError, ValueError):
            pass
        members.append({**member, "head_sha": live or member["head_sha"]})
    live_set = branch_set.from_item(item, members)
    mapping = hidden.apply_ops(cfg, [{
        "op": "hide",
        "pr_id": item["id"],
        "head_sha": live_set.heads_key,
        "hidden_at": derive.now_utc(),
    }])
    return {"id": item["id"], "hidden": True, "hidden_count": len(mapping)}


@mcp.tool()
def unhide_pr(ref: str) -> dict:
    """Unhide a previously hidden PR, returning it to the pending queue.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    Returns {id, hidden: false, hidden_count}.
    """
    cfg = _get_cfg()
    item = query.resolve_item(query.load_items(cfg), ref)
    mapping = hidden.apply_ops(cfg, [{
        "op": "unhide",
        "pr_id": item["id"],
        "head_sha": None,
        "hidden_at": None,
    }])
    return {"id": item["id"], "hidden": False, "hidden_count": len(mapping)}


@mcp.tool()
def get_diff(
    ref: str,
    files: list[str] | None = None,
    changed_since_review_only: bool = False,
    max_chars: int = 60_000,
) -> dict:
    """Per-member diff text for one PR, whole files only, under a shared char
    budget (files past the budget go to omitted_files - re-request them).

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    files: restrict to these paths (exact, basename, or suffix match).
    changed_since_review_only: only files that changed since your last review.
    """
    items = query.load_items(_get_cfg())
    item = query.resolve_item(items, ref)
    return query.get_diff_text(
        item, files=files,
        changed_since_review_only=changed_since_review_only,
        max_chars=max_chars,
    )


@mcp.tool()
def get_ai_review(ref: str) -> dict:
    """The cached AI first-pass sanity check for one PR (may be empty - not every
    PR gets one). Returns {id, title, ai_review_verdict, ai_reviews}.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    """
    items = query.load_items(_get_cfg())
    item = query.resolve_item(items, ref)
    ai_reviews = item.get("ai_reviews") or []
    out = {
        "id": item["id"],
        "title": item.get("title"),
        "ai_review_verdict": item.get("ai_review_verdict"),
        "ai_reviews": ai_reviews,
    }
    if not ai_reviews:
        out["note"] = (
            "No AI review cached for this PR (too large, AI disabled, or not yet "
            "computed on the last refresh)."
        )
    return out


@mcp.tool()
def set_ai_review(ref: str, summary: str, verdict: str,
                  concerns: list[dict] | None = None) -> dict:
    """Store a first-pass triage review for one PR, in the same slot the
    automatic pass writes to.

    This is a manual backfill. The refresh only reviews PRs whose diff is cached
    and small, so the big ones - often the ones most worth a second opinion - come
    back empty from get_ai_review forever. Review such a PR yourself (get_pr,
    get_diff), then record the verdict here so it shows on the dashboard.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    On a pair the ref picks which half to record, with the other half kept as its
    pair context - the same shape a paired automatic review has. Each half holds
    its own review, so call this once per half; a pair with only one recorded is
    shown on the dashboard as partially reviewed.

    summary: 1-2 sentences on what the PR actually does.
    verdict: 'looks-good' (nothing concerning) | 'minor' (small things to ask
    about) | 'major' (should block merge). Anything else is rejected.
    concerns: most important first, each {"severity": "high"|"med"|"low",
    "message": "one specific line", "where": "file path"}. where is optional,
    entries without a message are dropped, and only the first 5 are kept.

    Calling again for the same PR updates the row in place (replaced: true),
    including over an automatic review. Returns {id, head_sha, context_heads,
    verdict, concern_count, replaced, computed_at}, plus replaced_source and
    replaced_at describing the row that was overwritten - a 'manual' one stamped
    moments ago means another session was reviewing the same PR.

    The dashboard HTML is re-rendered shortly after the write, so the row shows
    up on the next browser reload without a `pr-dash refresh`.
    """
    cfg = _get_cfg()
    item = query.resolve_item(query.load_items(cfg), ref)
    if verdict not in ai._VERDICTS:
        raise ValueError(
            f"verdict must be one of {', '.join(ai._VERDICTS)}, got {verdict!r}",
        )
    head_sha, context_heads, companion_head_sha = _ai_review_shas(item, ref)
    # Normalised by the same parser the pipeline runs model output through
    # (unknown severity clamped, blank messages dropped, capped at 5), so a
    # hand-written row is indistinguishable from a generated one downstream. Its
    # lenient verdict fallback is not wanted here though - a caller that meant
    # something by an unknown verdict deserves the error above, not "looks-good".
    result = ai._parse_review(
        {"summary": summary, "verdict": verdict, "concerns": concerns or []},
        head_sha,
    )
    if result is None:
        raise ValueError("nothing to store: pass a summary, at least one concern, or both")

    computed_at = derive.now_utc()
    conn = db.connect(cfg.db_path)
    try:
        # Keyed on head_sha alone, so a row under another context is overwritten and reported.
        previous = db.get_ai_review_any(conn, head_sha)
        with db.transaction(conn):
            db.upsert_ai_review(
                conn, head_sha, context_heads, result.summary,
                json.dumps(result.concerns), result.verdict, computed_at,
                source="manual", companion_head_sha=companion_head_sha,
            )
    finally:
        conn.close()
    _schedule_rerender(cfg)
    out = {
        "id": item["id"],
        "head_sha": head_sha,
        "context_heads": context_heads,
        "companion_head_sha": companion_head_sha,
        "verdict": result.verdict,
        "concern_count": len(result.concerns),
        "replaced": previous is not None,
        "computed_at": computed_at,
    }
    if previous is not None:
        # Sessions write independently and the last one wins, so say what lost:
        # replacing a 'manual' row stamped seconds ago means another agent was
        # reviewing the same PR, which is worth noticing.
        out["replaced_source"] = previous["source"]
        out["replaced_at"] = previous["computed_at"]
    return out


@mcp.tool()
def review_history(
    author: str | None = None,
    module: str | None = None,
    verdict: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Your archived (already-reviewed) PRs as triage rows, newest first.

    author: exact GitHub login. module: an Odoo module the PR touched.
    verdict: your review decision (APPROVED / CHANGES_REQUESTED / COMMENTED).
    """
    items = query.load_items(_get_cfg())
    return query.review_history(items, author=author, module=module,
                                verdict=verdict, limit=limit)


@mcp.tool()
def stats() -> dict:
    """Counts over the pending queue (by bucket, by flag, drafts, hidden) and the
    archived history (by review verdict, by PR state, last 30 days, pinged)."""
    cfg = _get_cfg()
    items = query.load_items(cfg)
    out = query.stats(items)
    hidden_ids = _hidden_ids(cfg, items)
    out["pending"]["hidden"] = sum(
        1 for it in items if not it.get("is_archived") and it["id"] in hidden_ids
    )
    return out


@mcp.tool()
def refresh(force: bool = False) -> dict:
    """Refresh the cache from GitHub (and run AI reviews), same as running
    `pr-dash refresh`. Slow and hits the network + AI - most tools read the
    existing cache and don't need this. force re-fetches everything.

    Returns {ok, stdout_tail, stderr_tail}.
    """
    cmd = [sys.executable, "-m", "pr_dash", "refresh", "--no-open"]
    if force:
        cmd.append("--force")
    cfg_path = _config_path()
    if cfg_path is not None:
        cmd += ["--config", str(cfg_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return {
        "ok": proc.returncode == 0,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def _make_handler(cfg: config.Config) -> type[BaseHTTPRequestHandler]:
    class HiddenSyncHandler(BaseHTTPRequestHandler):
        # The dashboard is opened as file:// (origin "null"), so every response
        # needs permissive CORS and a preflight answer.
        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send_json(self, code: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:  # never touch stdout/stderr
            pass

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/hidden":
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(200, hidden.load(cfg))

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path not in ("/hidden", "/tracked", "/mine", "/mine-ack"):
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                parsed = json.loads(raw or b"{}")
                ops = parsed.get("ops") or []
            except (ValueError, AttributeError):
                self._send_json(400, {"error": "invalid body"})
                return
            if path == "/mine-ack":
                self._send_json(200, {"ok": True, "count": _apply_ack_ops(cfg, ops)})
                return
            if path != "/hidden":
                count = _apply_dismiss_ops(cfg, path[1:], ops)
                self._send_json(200, {"ok": True, "count": count})
                return
            mapping = hidden.apply_ops(cfg, ops)
            self._send_json(200, {"ok": True, "count": len(mapping)})

    return HiddenSyncHandler


def _apply_dismiss_ops(cfg: config.Config, tab: str, ops: list) -> int:
    """Write dashboard dismiss/restore ops through to the `tab` table.

    Unlike hides (a JSON file), dismissals live in SQLite, so this opens its own
    short-lived connection - the handler runs on the listener thread and sqlite3
    connections are not shareable across threads.
    """
    applied = 0
    conn = db.connect(cfg.db_path)
    try:
        with db.transaction(conn):
            for op in ops:
                pr_id = (op or {}).get("pr_id")
                kind = (op or {}).get("op")
                if not pr_id or kind not in ("dismiss", "restore"):
                    continue
                when = (op.get("dismissed_at") or derive.now_utc()) \
                    if kind == "dismiss" else None
                db.set_dismissed(conn, tab, pr_id, when)
                applied += 1
    finally:
        conn.close()
    return applied


def _apply_ack_ops(cfg: config.Config, ops: list) -> int:
    """Store dashboard Acknowledge ops, each an ack of a Branch set at the fingerprint it showed."""
    applied = 0
    conn = db.connect(cfg.db_path)
    try:
        with db.transaction(conn):
            for op in ops:
                key = (op or {}).get("key")
                kind = (op or {}).get("op")
                if not key or kind not in ("ack", "unack") or (
                        kind == "ack" and not op.get("fingerprint")):
                    continue
                db.set_mine_ack(conn, key, op["fingerprint"] if kind == "ack" else None,
                                op.get("at") or derive.now_utc())
                applied += 1
    finally:
        conn.close()
    return applied


def start_hidden_listener(cfg: config.Config) -> ThreadingHTTPServer | None:
    """Bind the localhost hidden-state write-through listener. Returns the
    server, or None if the port is already taken (another MCP instance owns it -
    first one wins)."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", cfg.hidden_sync_port), _make_handler(cfg))
    except OSError as e:
        log.info("hidden-sync listener not started (port %d unavailable: %s)",
                 cfg.hidden_sync_port, e)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("hidden-sync listener on 127.0.0.1:%d", server.server_address[1])
    return server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        start_hidden_listener(_get_cfg())
    except (FileNotFoundError, ValueError) as e:
        # No usable config yet: run the server anyway (tools surface the error),
        # matching the prior behaviour where config was only loaded on first use.
        log.warning("hidden-sync listener skipped: %s", e)
    mcp.run()
