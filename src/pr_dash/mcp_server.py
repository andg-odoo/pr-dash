from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from pr_dash import ai, branch_set, checkout, config, db, derive, github, query, runbot_cache, tab

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
    """Ids of the items whose hide still holds, a push to any half having ended it."""
    conn = db.connect(cfg.db_path)
    try:
        return set(db.live_marks(conn, "hide", {it["id"]: it["heads_key"] for it in items}))
    finally:
        conn.close()


def _ai_review_shas(item: dict, ref: str) -> tuple[str, str, str]:
    """(head_sha, context_heads, companion_head_sha) of the half `ref` names, else the primary's."""
    bset = branch_set.from_item(item)
    target = next((pr for _, pr in tab.hits([item], ref)), bset.primary)
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


def _refresh_prs(refs: list[str]) -> str | None:
    """Run `pr-dash refresh --pr` over `owner/repo#n` refs, returning its output when it failed."""
    cmd = [sys.executable, "-m", "pr_dash", "refresh", "--no-open", *(f"--pr={r}" for r in refs)]
    cfg_path = _config_path()
    if cfg_path is not None:
        cmd += ["--config", str(cfg_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if proc.returncode == 0:
        return None
    return (proc.stdout + proc.stderr).strip()[-1000:] or f"exit status {proc.returncode}"


def _fresh(pr_id: str) -> dict:
    """Refetch `pr_id` and its Branch set, as the warning field to answer with when that failed."""
    error = _refresh_prs([pr_id])
    return {"warning": f"Refresh failed, so this answer may be stale: {error}"} if error else {}


@mcp.tool()
def list_prs(
    status: str = "pending", include_hidden: bool = False, include_drafts: bool = False, sort: str = "dashboard"
) -> dict:
    """List PRs as compact triage rows, an absent key meaning empty or false.

    sort: 'dashboard' (default, smallest bucket first, oldest request first within
    a bucket) | 'requested' (oldest first review request first across all buckets,
    rows without a request date last). Both order a re-review by its first request.

    status: 'pending' (default, not yet reviewed) | 'archived' | 'all'.
    include_hidden: PRs you've hidden are excluded from pending results by default,
    True includes them marked "hidden": true.
    include_drafts: unreviewed drafts are excluded too, True includes them.
    Returns {cache_fetched_at, count, prs}; a pair's row lists its members.

    Flags: RE=re-review requested, MSG=awaiting my reply, CI!=failing CI,
    CFL=merge conflict, OLD=stale request, PEND!=you have an unsent review draft,
    PING=an archived-but-open PR whose author asked for a re-review informally
    (ping_at/ping_author/ping_snippet), PUSH=an archived-but-open PR whose head moved
    after your review (push_at/push_sha). Buckets S/M/L/XL = rough complexity.

    `companion` is the bundle's migration PR in odoo/upgrade, matched by head branch.
    It never shows up in the diff, so its absence means a data move has no upgrade script.
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
    if sort == "requested":
        sel.sort(key=lambda it: (not it.get("first_review_requested_at"), it.get("first_review_requested_at") or ""))
    elif sort != "dashboard":
        raise ValueError(f"sort must be 'dashboard' or 'requested', got {sort!r}")
    if not include_hidden:
        # Hiding only applies to the active queue; archived rows are never filtered.
        sel = [it for it in sel if it.get("is_archived") or it["id"] not in hidden_ids]
    if not include_drafts:
        sel = [it for it in sel if it.get("is_archived") or not it.get("is_draft")]
    prs = []
    for it in sel:
        row = tab.QUEUE.summarize(it)
        if it["id"] in hidden_ids:
            row["hidden"] = True
        prs.append(row)
    return {
        "cache_fetched_at": query.cache_fetched_at(cfg),
        "count": len(prs),
        "prs": prs,
    }


@mcp.tool()
def get_pr(ref: str, fresh: bool = False) -> dict:
    """Review detail for one PR: body, reviewers, ci_failures, ai_reviews (the cached
    first pass), task links, companion migration PR and per-file diff metadata. No
    comment bodies: human_comments and unresolved_threads count them, get_comments
    reads them, get_diff serves the diff text.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    An enterprise number resolves to its odoo+enterprise pair.

    `companion` is the bundle's migration PR in odoo/upgrade (repo, number, url,
    title, state, head_sha), matched by head branch. Its diff is in no repo this
    PR touches, so before flagging a data move as unmigrated, check it: null
    means there genuinely is no upgrade script.

    Each review-queue member carries `commits` (oldest first, at most 20 of `commit_count`) and
    `runbot`, its latest runbot batch's trigger builds with their sub-build tally, or {error}.

    A ref naming one of your Authored PRs instead returns its Branch set as get_mine does.
    Use fresh=True when activity from the last few minutes matters.
    """
    cfg = _get_cfg()
    view, row = tab.first(cfg, ref, tab.QUEUE, tab.MINE)
    stale = {}
    if fresh:
        stale = _fresh(row["id"] if view is tab.QUEUE else row["members"][0]["id"])
        view, row = tab.first(cfg, ref, tab.QUEUE, tab.MINE)
    out = {**view.detail(row), **stale}
    if view is tab.QUEUE:
        out["members"] = _review_members(cfg, out["members"])
    return out


def _review_members(cfg: config.Config, members: list[dict]) -> list[dict]:
    """Each Review queue member with its runbot batch, fetched when stale."""
    staleness = timedelta(minutes=cfg.thresholds.mine_staleness_minutes)
    conn = db.connect(cfg.db_path)
    try:
        out = []
        for m in members:
            row = db.get_cached_pr(conn, f"{m['repo']}#{m['number']}")
            out.append({**m, "runbot": runbot_cache.batch_runbot(conn, row["runbot_url"], staleness)})
        return out
    finally:
        conn.close()


@mcp.tool()
def get_comments(ref: str, include_bots: bool = False, unresolved_only: bool = False,
                 since: str | None = None) -> dict:
    """The Discussion of any cached PR, whichever tab holds it.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    A Review queue PR returns {id, discussion}, both halves of a pair merged. An Authored
    PR returns {key, prs: [{ref, discussion}]} for its set's PRs with a discussion left, a
    merged or closed Forward-port only when ref names it. A Tracked PR returns its summary, body and discussion.

    A discussion is a list of groups, newest first, {kind ('review' / 'issue' / 'orphan'),
    entry, threads}. threads hold {thread_id, path, state (RESOLVED / UNRESOLVED), comments}
    oldest first. Entries carry author, created_at, body, url, is_bot, is_pending (an
    unsent draft) and, for reviews, state.

    include_bots: keep robodoo, fw-bot and *[bot] entries. unresolved_only: only
    UNRESOLVED threads with the groups holding them. since: ISO time, only entries after it.
    """
    return tab.get_comments(_get_cfg(), ref, include_bots=include_bots,
                            unresolved_only=unresolved_only, since=since)


@mcp.tool()
def list_mine(band: str | None = None, include_dismissed: bool = False) -> dict:
    """List your Authored PRs as Branch sets, PRs sharing one head branch across repos:
    Needs you first (oldest Action item first), then Open by most recent activity, then Done.

    band: only sets in this band ('needs', 'open' or 'done'), every band by default.
    include_dismissed: dismissed members are excluded by default; True adds them
    back carrying dismissed_at.

    Each set carries key (the head branch), task, title and url (the primary's), band,
    actions [{member, kind, text, since}] (what waits on you: thread, ci, conflict,
    changes, reviewers, linked, fw), fyi labels (movement since the last look, 'idle Nd',
    'waiting on re-review', 'source merged', 'fw k/n merged'), acknowledged, runbot and
    members. Members, empty or false keys left out, carry state, ci (green / red / pending)
    with ci_failing and override, decision, r_plus, requested people and teams, conflict,
    head_sha, branch, new_comments, unresolved_threads and fw, the Forward-ports in target
    branch order with base and flag (conflict / red / null). Returns {cache_fetched_at,
    count, branch_sets}.
    """
    cfg = _get_cfg()
    sets = [tab.MINE.summarize(s) for s in tab.MINE.load(cfg, include_dismissed=include_dismissed)
            if band in (None, s["band"])]
    return {
        "cache_fetched_at": query.cache_fetched_at(cfg),
        "count": len(sets),
        "branch_sets": sets,
    }


@mcp.tool()
def get_mine(ref: str, discussion: bool = False, fresh: bool = False) -> dict:
    """The Branch set holding one of your Authored PRs, ref naming any member or Forward-port.

    Each member and Forward-port carries ref, url, state, draft, ci, ci_failing, override,
    staging, staging_url, conflict, base, head_sha, branch, review, decision, r_plus,
    requested people and teams, mergebot_unknown, new_comments (human, since your last
    look) and unresolved_threads. discussion=True returns every member with its body and
    Discussion tree (get_comments shape) and each Forward-port with its own.
    Use fresh=True when activity from the last few minutes matters.
    """
    cfg = _get_cfg()
    stale = _fresh(tab.MINE.find(cfg, ref)["members"][0]["id"]) if fresh else {}
    branch_set = tab.MINE.find(cfg, ref)
    out = query.mine_detail(cfg, branch_set) if discussion else tab.MINE.detail(branch_set)
    return {**out, **stale}


@mcp.tool()
def get_runbot(ref: str) -> dict:
    """Runbot status and failures for one of your Authored PRs, or for a bundle by name.

    ref accepts a PR ref as get_mine does, naming its whole Branch set, or an exact bundle
    name (several matches return `candidates` and their `count`).
    Returns {ref, requests, error, batches: [{batch_id, prs, triggers}]},
    a trigger being {name, build_id, url, verdict, children, previous, failures, error, prs}.
    A trigger `error` of "deferred" (request cap) clears on a later call. A test failure
    carries its runbot `tag` and `known_error`, and a failure {log: "gone"} still links its build.
    """
    return runbot_cache.get_runbot(_get_cfg(), ref)


@mcp.tool()
def hide_pr(ref: str, hidden: bool = True) -> dict:
    """Hide a PR from the pending queue (the dashboard's × button) until a push to any
    code half of its Branch set, or unhide it with hidden=False.

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    Returns {id, hidden, hidden_count}.
    """
    cfg = _get_cfg()
    item = tab.only(cfg, ref, "hide_pr")
    if not hidden:
        _apply_mark_ops(cfg, [{"kind": "hide", "op": "clear", "key": item["id"]}])
        return {"id": item["id"], "hidden": False, "hidden_count": _hidden_count(cfg)}
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
    _apply_mark_ops(cfg, [{
        "kind": "hide",
        "op": "set",
        "key": item["id"],
        "guard": live_set.heads_key,
        "at": derive.now_utc(),
    }])
    return {"id": item["id"], "hidden": True, "hidden_count": _hidden_count(cfg)}


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
    Review queue only: an Authored PR's diff is not cached, its miss names the local checkout.
    """
    item = checkout.diff_item(_get_cfg(), ref)
    return query.get_diff_text(
        item, files=files,
        changed_since_review_only=changed_since_review_only,
        max_chars=max_chars,
    )


@mcp.tool()
def set_ai_review(ref: str, summary: str, verdict: str,
                  concerns: list[dict] | None = None) -> dict:
    """Store a first-pass review for one PR in the slot the automatic pass writes to,
    replacing any row there, and re-render the dashboard.

    summary: 1-2 sentences on what the PR does. verdict: 'looks-good' | 'minor' | 'major'.
    concerns: most important first, each {severity: high|med|low, message, where (optional
    file path)}, at most 5 kept. On a pair, ref picks the half, so call once per half.
    Returns {id, head_sha, context_heads, verdict, concern_count, replaced, computed_at},
    plus replaced_source and replaced_at for the row overwritten.
    """
    cfg = _get_cfg()
    item = tab.only(cfg, ref, "set_ai_review")
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
    class MarksHandler(BaseHTTPRequestHandler):
        # The dashboard is opened as file:// (origin "null"), so every response
        # needs permissive CORS and a preflight answer.
        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

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

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path not in ("/marks", "/refresh"):
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                parsed = json.loads(raw or b"{}")
                items = parsed.get("ops" if path == "/marks" else "refs") or []
            except (ValueError, AttributeError):
                self._send_json(400, {"error": "invalid body"})
                return
            if path == "/marks":
                self._send_json(200, {"ok": True, "count": _apply_mark_ops(cfg, items)})
                return
            if not (items and isinstance(items, list) and all(isinstance(r, str) for r in items)):
                self._send_json(400, {"error": "refs must be a list of owner/repo#n"})
                return
            # The CLI holds the refresh lock and re-renders, so the page only has to reload.
            error = _refresh_prs(items)
            self._send_json(502 if error else 200, {"error": error} if error else {"ok": True})

    return MarksHandler


# The mark kinds the listener stores, each mapped to whether setting one needs a guard.
_MARK_KINDS = {"hide": False, "dismiss_tracked": False, "dismiss_mine": False, "ack": True}


def _apply_mark_ops(cfg: config.Config, ops: list) -> int:
    """Store set/clear mark ops, re-rendering when any applied, and return how many were."""
    applied = 0
    conn = db.connect(cfg.db_path)
    try:
        with db.transaction(conn):
            for op in ops:
                kind, key, verb = ((op or {}).get(f) for f in ("kind", "key", "op"))
                if kind not in _MARK_KINDS or not key or verb not in ("set", "clear") or (
                        verb == "set" and _MARK_KINDS[kind] and not op.get("guard")):
                    continue
                if verb == "set":
                    db.set_mark(conn, kind, key, op.get("guard"), op.get("at") or derive.now_utc())
                else:
                    db.clear_marks(conn, kind, [key])
                applied += 1
    finally:
        conn.close()
    if applied:
        _schedule_rerender(cfg)
    return applied


def _hidden_count(cfg: config.Config) -> int:
    conn = db.connect(cfg.db_path)
    try:
        return len(db.marks(conn, "hide"))
    finally:
        conn.close()


def start_marks_listener(cfg: config.Config) -> ThreadingHTTPServer | None:
    """Bind the localhost mark write-through listener. Returns the
    server, or None if the port is already taken (another MCP instance owns it -
    first one wins)."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", cfg.hidden_sync_port), _make_handler(cfg))
    except OSError as e:
        log.info("marks listener not started (port %d unavailable: %s)",
                 cfg.hidden_sync_port, e)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("marks listener on 127.0.0.1:%d", server.server_address[1])
    return server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        start_marks_listener(_get_cfg())
    except (FileNotFoundError, ValueError) as e:
        # No usable config yet: run the server anyway (tools surface the error),
        # matching the prior behaviour where config was only loaded on first use.
        log.warning("marks listener skipped: %s", e)
    mcp.run()
