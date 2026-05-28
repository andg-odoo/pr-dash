from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

TASK_RE = re.compile(r"\b(task|opw)[-\s]?(\d{4,8})\b", re.IGNORECASE)

_DIFF_FILE_SPLIT = re.compile(r"(?m)^(?=diff --git )")
_DIFF_FILE_PATH = re.compile(r"diff --git a/.+? b/(.+)")


def iter_diff_files(diff_text: str) -> Iterator[tuple[str | None, str]]:
    """Yield (path, chunk) for each file section in a combined `.diff`.

    path is the b-side path from the `diff --git` header, or None for a chunk
    with no recognizable header (e.g. a leading preamble). Shared by the AI
    noise-stripper and the change-signature hasher so they can never disagree
    about file boundaries; the frontend has its own copy in app.js."""
    if not diff_text:
        return
    for chunk in _DIFF_FILE_SPLIT.split(diff_text):
        if not chunk.strip():
            continue
        m = _DIFF_FILE_PATH.match(chunk)
        yield (m.group(1).strip() if m else None), chunk


def file_change_signatures(diff_text: str) -> dict[str, str]:
    """Map each file path in a combined `.diff` to a hash of only its +/- lines
    (ignoring @@ headers and context). Two diffs of the same change hash equal
    even after a rebase shifts line numbers/context, so this is the basis for
    detecting which files actually changed since a prior review."""
    sigs: dict[str, str] = {}
    for path, chunk in iter_diff_files(diff_text):
        if path is None:
            continue
        changed = [
            ln for ln in chunk.split("\n")
            if (ln.startswith("+") and not ln.startswith("+++"))
            or (ln.startswith("-") and not ln.startswith("---"))
        ]
        sigs[path] = hashlib.sha1(
            "\n".join(changed).encode("utf-8", "replace")
        ).hexdigest()[:16]
    return sigs


def thread_signature(threads: list[dict]) -> str:
    """Stable signature of a PR's thread activity. Changes when a thread is
    added or gets a new reply, so it detects discussion movement since a prior view."""
    parts = sorted(f"{t['thread_id']}:{t.get('last_reply_at', '')}" for t in threads)
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def since_last_look_tags(
    prev: tuple[str | None, str | None, str] | None,
    head_sha: str, ci_state: str | None, thread_sig: str, *, first_run: bool,
) -> list[str]:
    """What changed since the PR was last rendered. `prev` is the previously
    seen (head_sha, ci_state, thread_sig) or None. On the first run ever
    (`first_run`) nothing is flagged - there's no baseline to compare against."""
    if first_run:
        return []
    if prev is None:
        return ["new"]
    p_head, p_ci, p_sig = prev
    tags = []
    if p_head != head_sha:
        tags.append("pushed")
    if (p_ci or "") != (ci_state or ""):
        tags.append("ci")
    if p_sig != thread_sig:
        tags.append("reply")
    return tags


def path_to_module(repo: str, path: str) -> str | None:
    if repo == "odoo/enterprise":
        return path.split("/", 1)[0] if "/" in path else None
    if repo == "odoo/odoo":
        if path.startswith("addons/"):
            parts = path.split("/", 2)
            return parts[1] if len(parts) >= 2 else None
        if path.startswith("odoo/addons/"):
            parts = path.split("/", 3)
            return parts[2] if len(parts) >= 3 else None
        if path.startswith("odoo/"):
            return "_core"
        return "_root"
    return None


def modules_for(repo: str, paths: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for p in paths:
        m = path_to_module(repo, p)
        if m:
            seen.setdefault(m, None)
    return list(seen.keys())


def installable_modules(modules: list[str]) -> list[str]:
    return [m for m in modules if not m.startswith("_")]


def parse_linked_task(body: str | None) -> tuple[str, str] | None:
    """Return (kind, id) for the first task/opw reference, or None.

    kind is lowercase: 'task' or 'opw'.
    """
    if not body:
        return None
    m = TASK_RE.search(body)
    if not m:
        return None
    return m.group(1).lower(), m.group(2)


@dataclass
class ThreadDerivation:
    threads: list[dict]
    unresolved: int
    awaiting_my_reply: bool


def derive_threads(thread_nodes: list[dict], my_login: str) -> ThreadDerivation:
    out: list[dict] = []
    unresolved = 0
    awaiting = False
    for t in thread_nodes:
        comments = (t.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        i_participated = any((c.get("author") or {}).get("login") == my_login for c in comments)
        last = comments[-1]
        last_login = (last.get("author") or {}).get("login") or ""
        last_at = last.get("createdAt") or ""
        is_resolved = bool(t.get("isResolved"))
        if not is_resolved:
            unresolved += 1
            if i_participated and last_login and last_login != my_login:
                awaiting = True
        out.append({
            "thread_id": t["id"],
            "is_resolved": int(is_resolved),
            "i_participated": int(i_participated),
            "last_reply_at": last_at,
            "last_reply_author": last_login,
        })
    return ThreadDerivation(threads=out, unresolved=unresolved, awaiting_my_reply=awaiting)


def derive_reviewers(latest_reviews: list[dict], review_requests: list[dict]) -> list[dict]:
    # Start with latest reviews (whose authors are users)
    out: dict[tuple[str, str], dict] = {}
    for r in latest_reviews:
        login = (r.get("author") or {}).get("login")
        state = r.get("state")
        if login and state:
            out[("user", login)] = {"kind": "user", "name": login, "state": state}
    # Add pending requests not already accounted for
    for r in review_requests:
        rr = r.get("requestedReviewer") or {}
        typename = rr.get("__typename")
        if typename == "User":
            name = rr.get("login")
            kind = "user"
        elif typename == "Team":
            name = rr.get("slug")
            kind = "team"
        else:
            continue
        if not name:
            continue
        if (kind, name) not in out:
            out[(kind, name)] = {"kind": kind, "name": name, "state": "PENDING"}
    return list(out.values())


def latest_review_requested_at(timeline: list[dict], my_login: str, fallback: str) -> str:
    """Find most recent ReviewRequestedEvent where I was the target."""
    latest = ""
    for ev in timeline:
        if ev.get("__typename") != "ReviewRequestedEvent":
            continue
        rr = ev.get("requestedReviewer") or {}
        if rr.get("__typename") == "User" and rr.get("login") == my_login:
            ts = ev.get("createdAt") or ""
            if ts > latest:
                latest = ts
    return latest or fallback


def previously_reviewed(timeline: list[dict], my_login: str) -> bool:
    for ev in timeline:
        if ev.get("__typename") != "PullRequestReview":
            continue
        if (ev.get("author") or {}).get("login") == my_login:
            return True
    return False


_FAILING_STATES = {"FAILURE", "ERROR"}
_FAILING_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "CANCELLED"}


def _iter_checks(status_check_rollup: dict | None) -> Iterator[dict]:
    """Yield each rollup check normalized to {name, url, failing}, hiding the
    StatusContext vs CheckRun field-name differences (context/targetUrl/state
    vs name/detailsUrl/conclusion)."""
    if not status_check_rollup:
        return
    for ctx in (status_check_rollup.get("contexts") or {}).get("nodes") or []:
        typename = ctx.get("__typename")
        if typename == "StatusContext":
            yield {
                "name": ctx.get("context") or "(check)",
                "url": ctx.get("targetUrl"),
                "failing": (ctx.get("state") or "") in _FAILING_STATES,
            }
        elif typename == "CheckRun":
            yield {
                "name": ctx.get("name") or "(check)",
                "url": ctx.get("detailsUrl"),
                "failing": (ctx.get("conclusion") or "") in _FAILING_CONCLUSIONS,
            }


def status_check_state(status_check_rollup: dict | None) -> tuple[str | None, str | None]:
    """Return (overall_state, runbot_url) from a statusCheckRollup."""
    if not status_check_rollup:
        return None, None
    checks = list(_iter_checks(status_check_rollup))
    # Prefer the main 'ci/runbot' context; fall back to any runbot.odoo.com URL.
    runbot = next(
        (c["url"] for c in checks
         if c["name"] == "ci/runbot" and "runbot.odoo.com" in (c["url"] or "")),
        None,
    ) or next(
        (c["url"] for c in checks if "runbot.odoo.com" in (c["url"] or "")),
        None,
    )
    return status_check_rollup.get("state"), runbot


def failing_checks(status_check_rollup: dict | None) -> list[dict]:
    """Extract the individual failing checks (name + url) from a rollup, so a
    reviewer can see *which* check is red rather than just an overall FAILURE."""
    return [
        {"name": c["name"], "url": c["url"]}
        for c in _iter_checks(status_check_rollup) if c["failing"]
    ]


def is_personally_requested(review_requests: list[dict], my_login: str) -> bool:
    for r in review_requests:
        rr = r.get("requestedReviewer") or {}
        if rr.get("__typename") == "User" and rr.get("login") == my_login:
            return True
    return False


def heuristic_score(
    *,
    additions: int,
    deletions: int,
    changed_files: int,
    modules: list[str],
    unresolved_threads: int,
    previously_reviewed: bool,
) -> float:
    size = additions + deletions
    files = max(changed_files, 1)
    mods = len(modules)
    score = (
        math.log2(size + 2)
        * math.sqrt(files)
        * (1 + 0.4 * mods)
        * (1 + 0.2 * unresolved_threads)
        * (0.5 if previously_reviewed else 1.0)
    )
    return round(score, 2)


def bucket_for(score: float, M: float, L: float, XL: float) -> str:
    if score < M:
        return "S"
    if score < L:
        return "M"
    if score < XL:
        return "L"
    return "XL"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def days_since(iso_ts: str) -> int:
    return max(0, (datetime.now(timezone.utc) - parse_iso(iso_ts)).days)


def detect_pairs(prs: list[dict]) -> dict[str, str]:
    """Map pr id -> paired pr id. Pair = same (author, head_branch) across exactly 2 PRs."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for pr in prs:
        groups.setdefault((pr["author"], pr["head_branch"]), []).append(pr)
    pairs: dict[str, str] = {}
    for group in groups.values():
        if len(group) == 2:
            a, b = group
            pairs[a["id"]] = b["id"]
            pairs[b["id"]] = a["id"]
    return pairs
