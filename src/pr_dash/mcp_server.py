from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from pr_dash import config, db, derive, github, hidden, query

# stderr only: stdout is the MCP protocol channel, so a single stray print or
# rich.Console write there corrupts the stream. Everything human-facing goes to
# stderr; the one process that legitimately writes to stdout (a `pr-dash
# refresh`) is a subprocess with its output captured, never inherited.
log = logging.getLogger("pr_dash.mcp")

mcp = FastMCP("pr-dash")

_CONFIG_PATH: Path | None = None
_cfg: config.Config | None = None


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
    commands, task links, and per-file diff metadata (no diff text - use
    get_diff for that).

    ref accepts: '12345', 'odoo#12345', 'odoo/odoo#12345', or a github PR URL.
    An enterprise number resolves to its odoo+enterprise pair.

    Flags: RE=re-review requested, MSG=awaiting my reply, CI!=failing CI,
    CFL=merge conflict, OLD=stale request. Buckets S/M/L/XL = rough complexity.
    my_pending_review is true when you have an unsent review draft (get_comments
    shows its body). Output stays slim - no comment bodies here.
    """
    items = query.load_items(_get_cfg())
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
    """
    cfg = _get_cfg()
    item = query.resolve_item(query.load_items(cfg), ref)
    return query.get_comments(cfg, item)


@mcp.tool()
def hide_pr(ref: str) -> dict:
    """Hide a PR from the pending queue (same as the dashboard's × button). It
    stays hidden until a head commit changes - a push to either half of a pair
    auto-unhides it.

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
            live = github.fetch_head_sha(
                member.get("repo") or "", member.get("number") or 0,
            )
        except (github.GithubError, ValueError):
            pass
        members.append({**member, "head_sha": live or member.get("head_sha")})
    mapping = hidden.apply_ops(cfg, [{
        "op": "hide",
        "pr_id": item["id"],
        "head_sha": hidden.item_sha({**item, "members": members}),
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
            if path not in ("/hidden", "/tracked"):
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
            if path == "/tracked":
                self._send_json(200, {"ok": True, "count": _apply_tracked_ops(cfg, ops)})
                return
            mapping = hidden.apply_ops(cfg, ops)
            self._send_json(200, {"ok": True, "count": len(mapping)})

    return HiddenSyncHandler


def _apply_tracked_ops(cfg: config.Config, ops: list) -> int:
    """Write dashboard dismiss/restore ops through to the tracked table.

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
                db.dismiss_tracked(conn, pr_id, when)
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
