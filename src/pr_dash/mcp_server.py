from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from pr_dash import config, query

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


@mcp.tool()
def list_prs(status: str = "pending") -> dict:
    """List PRs as compact triage rows, in dashboard order (worst bucket / oldest
    request first).

    status: 'pending' (default, not yet reviewed) | 'archived' | 'all'.
    Returns {cache_fetched_at, count, prs}.

    Flags: RE=re-review requested, MSG=awaiting my reply, CI!=failing CI,
    CFL=merge conflict, OLD=stale request. Buckets S/M/L/XL = rough complexity.
    """
    cfg = _get_cfg()
    items = query.load_items(cfg)
    if status == "pending":
        sel = [it for it in items if not it.get("is_archived")]
    elif status == "archived":
        sel = [it for it in items if it.get("is_archived")]
    elif status == "all":
        sel = items
    else:
        raise ValueError(f"status must be 'pending', 'archived', or 'all', got {status!r}")
    return {
        "cache_fetched_at": query.cache_fetched_at(cfg),
        "count": len(sel),
        "prs": [query.summarize(it) for it in sel],
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
    """
    items = query.load_items(_get_cfg())
    return query.detail(query.resolve_item(items, ref))


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
    """Counts over the pending queue (by bucket, by flag, drafts) and the
    archived history (by review verdict, by PR state, last 30 days)."""
    items = query.load_items(_get_cfg())
    return query.stats(items)


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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    mcp.run()
