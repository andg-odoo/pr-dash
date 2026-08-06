from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

# Tolerate the separators/noise that appear between the keyword and the id in PR
# bodies: a hyphen, spaces, a tilde, or a markdown link opening (`[`, sometimes
# with a leading `#`), e.g. `task-6234716`, `task ~6234716`, `task-[6234716](url)`.
TASK_RE = re.compile(r"\b(task|opw)[-\s~\[#]*(\d{4,8})\b", re.IGNORECASE)

_DIFF_FILE_SPLIT = re.compile(r"(?m)^(?=diff --git )")
_DIFF_FILE_PATH = re.compile(r"diff --git a/.+? b/(.+)")

# Generated / translation files carry no review signal but eat the diff budget,
# in the cache and in the AI prompt alike. Mirrors the frontend NOISY_RE used to
# fold these files in the dashboard.
NOISE_RE = re.compile(
    r"(\.(po|pot|map|lock)$)|(\.min\.(js|css)$)"
    r"|((^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$)",
    re.IGNORECASE,
)

# Prefix of every line pr-dash writes into a diff it edited. Nothing git emits
# starts this way, so a stub can never be mistaken for content the PR changed -
# by a reader, by the model, or by a later compaction pass.
STUB_TAG = "[pr-dash]"
_STUB_LINE = re.compile(rf"(?m)^ {re.escape(STUB_TAG)} ")

# Per-file cap above which a file's body is stubbed out. Tied to the AI review
# budget rather than to taste: a file whose own diff is larger than everything
# the prompt can hold could never have been reviewed in context anyway, while
# the rest of the PR is losing its review to it.
MAX_FILE_CHARS = 50_000


@dataclass
class CompactedDiff:
    """A diff reduced to what is worth reading, plus what that cost.

    `dropped` are files removed outright, `stubbed` are files present in `text`
    as a pr-dash stub - both are what a caller needs to say the diff is partial
    instead of quietly presenting it as the whole change.
    """
    text: str
    dropped: list[str]
    stubbed: list[str]

    @property
    def partial(self) -> bool:
        return bool(self.dropped or self.stubbed)


def iter_diff_files(diff_text: str) -> Iterator[tuple[str | None, str]]:
    """Yield (path, chunk) for each file section in a combined `.diff`.

    path is the b-side path from the `diff --git` header, or None for a chunk
    with no recognizable header (e.g. a leading preamble). Shared by the diff
    compactor and the change-signature hasher so they can never disagree
    about file boundaries; the frontend has its own copy in app.js."""
    if not diff_text:
        return
    for chunk in _DIFF_FILE_SPLIT.split(diff_text):
        if not chunk.strip():
            continue
        m = _DIFF_FILE_PATH.match(chunk)
        yield (m.group(1).strip() if m else None), chunk


def _changed_lines(chunk: str) -> list[str]:
    """A file chunk's +/- content lines, without the `---`/`+++` file headers."""
    return [
        ln for ln in chunk.split("\n")
        if (ln.startswith("+") and not ln.startswith("+++"))
        or (ln.startswith("-") and not ln.startswith("---"))
    ]


def pr_file_url(repo: str, number: int, path: str) -> str:
    """Deep link to one file inside a PR's Files tab. GitHub anchors each file
    on the sha256 of its b-side path - verified against a rendered files page,
    renames included (the new path is what hashes)."""
    digest = hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()
    return f"https://github.com/{repo}/pull/{number}/files#diff-{digest}"


def _stub_chunk(path: str, chunk: str, repo: str, number: int) -> str:
    """Replace a file's body with a pr-dash annotation, keeping its `diff --git`
    header so the file still shows up in every file list.

    Shaped as an empty-range hunk with context lines rather than as bare text:
    diff2html renders a file section with no hunk as "File without changes",
    which would turn an omission into a false claim about the PR. Context lines
    also keep the stub out of the +/- accounting everything else does on a diff.
    """
    changed = _changed_lines(chunk)
    added = sum(1 for ln in changed if ln.startswith("+"))
    size = len(chunk.encode("utf-8", "replace"))
    header = chunk.split("\n", 1)[0]
    body = (
        f"@@ -0,0 +0,0 @@ {STUB_TAG} file contents omitted\n"
        f" {STUB_TAG} +{added}/-{len(changed) - added} lines, {size} bytes,"
        f" omitted by pr-dash - not by this PR\n"
    )
    if repo and number:
        body += f" {STUB_TAG} view it at {pr_file_url(repo, number, path)}\n"
    return f"{header}\n{body}"


def compact_diff(
    diff_text: str,
    *,
    repo: str = "",
    number: int = 0,
    max_file_chars: int = MAX_FILE_CHARS,
    stub_noise: bool = False,
) -> CompactedDiff:
    """Shrink a combined `.diff` to the part worth reviewing.

    Two kinds of file lose their body: generated/translation noise, which has no
    review signal at any size, and any file whose chunk exceeds max_file_chars -
    one data file must not cost the rest of the PR its review. Noise is dropped
    outright, or reduced to a stub when `stub_noise` is set, which is what the
    cached copy wants: the dashboard lists every file the PR touches.

    The storage path and the review gate both come through here, so the size a
    PR is judged on is the size the model is handed. Re-running it on an already
    compacted diff is a no-op, which is what makes that guarantee hold.
    """
    kept: list[str] = []
    dropped: list[str] = []
    stubbed: list[str] = []
    for path, chunk in iter_diff_files(diff_text):
        noisy = bool(path and NOISE_RE.search(path))
        if noisy and not stub_noise:
            dropped.append(path)
            continue
        # A headerless chunk (a preamble at most) has no path to stub or link.
        if path and (noisy or len(chunk) > max_file_chars) and not _STUB_LINE.search(chunk):
            chunk = _stub_chunk(path, chunk, repo, number)
        if path and _STUB_LINE.search(chunk):
            stubbed.append(path)
        kept.append(chunk)
    return CompactedDiff("".join(kept), dropped, stubbed)


def file_change_signatures(diff_text: str) -> dict[str, str]:
    """Map each file path in a combined `.diff` to a hash of only its +/- lines
    (ignoring @@ headers and context). Two diffs of the same change hash equal
    even after a rebase shifts line numbers/context, so this is the basis for
    detecting which files actually changed since a prior review."""
    sigs: dict[str, str] = {}
    for path, chunk in iter_diff_files(diff_text):
        if path is None:
            continue
        changed = _changed_lines(chunk)
        if not changed:
            # A stub has no +/- lines, so every stubbed file would otherwise
            # hash alike and a re-pushed data file would read as "unchanged
            # since your review" - the one thing this signature exists to say
            # honestly. Its annotation carries the omitted line counts and byte
            # size, which is what moves when the file's content does.
            changed = [ln for ln in chunk.split("\n") if _STUB_LINE.match(ln)]
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


def tracked_row_from_node(node: dict, fetched_at: str) -> tuple[dict, list[dict]]:
    """Flatten a slim tracked-PR node into (state columns, comments).

    Discussion is merged from all three places GitHub keeps it - conversation
    comments, review submissions, and inline review threads - into one
    time-ordered stream. On an Odoo PR almost nothing lives in `comments`: the
    approvals and the actual argument are reviews and threads, so fetching only
    conversation comments makes a busy PR look silent.
    """
    ci_state, _ = status_check_state(
        ((node.get("commits") or {}).get("nodes") or [{}])[0]
        .get("commit", {}).get("statusCheckRollup"),
    )
    comment_block = node.get("comments") or {}
    review_block = node.get("reviews") or {}
    thread_block = node.get("reviewThreads") or {}

    def _entry(c, kind, when, *, path=None, state=None,
               thread_id=None, parent_id=None):
        return {
            "comment_id": c.get("url") or f"{kind}:{when}:{_login(c)}",
            "kind": kind,
            "thread_id": thread_id,
            "parent_id": parent_id,
            "author": _login(c),
            "created_at": when,
            "body": c.get("body"),
            "path": path,
            "state": state,
            "url": c.get("url"),
        }

    comments = [
        _entry(c, "issue", c.get("createdAt"))
        for c in (comment_block.get("nodes") or [])
    ]
    for r in review_block.get("nodes") or []:
        state = r.get("state")
        # A bare COMMENTED review with no body is just the envelope GitHub wraps
        # around inline thread comments - the comments themselves come through
        # reviewThreads, so keeping the envelope would double every one of them.
        # A bodiless APPROVED / CHANGES_REQUESTED is the opposite: the verdict is
        # the whole message, and dropping it loses the LGTM.
        if state == "COMMENTED" and not (r.get("body") or "").strip():
            continue
        comments.append(_entry(r, "review", r.get("submittedAt"), state=state,
                               thread_id=r.get("id")))
    for t in thread_block.get("nodes") or []:
        path = t.get("path")
        state = "RESOLVED" if t.get("isResolved") else "UNRESOLVED"
        thread_nodes = (t.get("comments") or {}).get("nodes") or []
        # A thread hangs off the review that opened it - its *first* comment's
        # review. Later replies belong to whatever review the replier happened
        # to be submitting, so keying on those would scatter one thread across
        # several parents.
        parent = ((thread_nodes[0] if thread_nodes else {}).get("pullRequestReview")
                  or {}).get("id")
        for c in thread_nodes:
            comments.append(_entry(c, "thread", c.get("createdAt"), path=path,
                                   state=state, thread_id=t.get("id"),
                                   parent_id=parent))
    comments.sort(key=lambda c: c["created_at"] or "")

    unresolved = sum(
        1 for t in (thread_block.get("nodes") or []) if not t.get("isResolved")
    )
    # Movement signal. Thread *replies* don't move any totalCount, but a new
    # review, a new thread, or a new conversation comment does - which covers
    # every way a watched PR visibly progresses.
    activity_count = (
        (comment_block.get("totalCount") or 0)
        + (review_block.get("totalCount") or 0)
        + (thread_block.get("totalCount") or 0)
    )
    row = {
        "title": node.get("title") or "",
        "author": _login(node) or "",
        "state": node.get("state") or "OPEN",
        "is_draft": int(bool(node.get("isDraft"))),
        "target_branch": node.get("baseRefName") or "",
        "head_sha": node.get("headRefOid") or "",
        "body": node.get("body"),
        "ci_state": ci_state,
        "comment_count": comment_block.get("totalCount") or 0,
        "activity_count": activity_count,
        "review_count": review_block.get("totalCount") or 0,
        "thread_count": thread_block.get("totalCount") or 0,
        "unresolved_threads": unresolved,
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "closed_at": node.get("closedAt"),
        "merged_at": node.get("mergedAt"),
        "fetched_at": fetched_at,
    }
    return row, comments


def tracked_since_last_look(
    prev: tuple[str | None, str | None, int | None] | None,
    state: str, head_sha: str | None, comment_count: int, *, first_run: bool,
) -> list[str]:
    """What moved on a tracked PR since it was last rendered.

    Deliberately coarser than the review-queue equivalent: for a PR you only
    watch, "it merged" and "someone said something" are the events worth a
    badge - CI churn and every force-push are not what you subscribed for.
    A state flip is reported even on the first run, since landing in the list
    already merged is the whole point of tracking it.
    """
    resolved = state in ("MERGED", "CLOSED")
    if prev is None:
        return ["resolved"] if resolved else ([] if first_run else ["new"])
    p_state, p_head, p_comments = prev
    tags = []
    if (p_state or "OPEN") != state:
        tags.append("resolved" if resolved else "reopened")
    elif resolved:
        # Still merged/closed and already flagged once - keep the badge so the
        # row stays visibly done until it is dismissed.
        tags.append("resolved")
    if p_head and head_sha and p_head != head_sha:
        tags.append("pushed")
    if p_comments is not None and comment_count > p_comments:
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


_BOT_LOGINS = {"robodoo", "fw-bot"}

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_MD_NOISE_RE = re.compile(r"[`*_>#~]")


def is_bot(login: str | None) -> bool:
    """True for automation authors (robodoo, fw-bot, GitHub App `*[bot]`), so
    callers can filter their noise from human discussion."""
    login = (login or "").lower()
    return login in _BOT_LOGINS or login.endswith("[bot]")


def comment_snippet(body: str | None, limit: int = 100) -> str:
    """One-line plain-text preview of a comment: markdown links reduced to their
    text, common markdown markers dropped, whitespace/newlines collapsed, then
    truncated with an ellipsis. For the dashboard's unresolved-thread lines."""
    if not body:
        return ""
    text = _MD_LINK_RE.sub(r"\1", body)
    text = _MD_NOISE_RE.sub("", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def derive_comments(node: dict, my_login: str) -> tuple[list[dict], bool]:
    """Flatten a PR node's review threads, review submissions and conversation
    comments into pr_comment rows, and report whether the viewer has an unsent
    (PENDING) review draft.

    A PENDING review is only ever returned for the viewer's own token, which is
    exactly the invisible-draft case we want to surface loudly.
    """
    out: list[dict] = []

    for t in (node.get("reviewThreads") or {}).get("nodes") or []:
        thread_id = t.get("id")
        for c in (t.get("comments") or {}).get("nodes") or []:
            cid = c.get("databaseId")
            if cid is None:
                continue
            out.append({
                "kind": "thread",
                "thread_id": thread_id,
                "comment_id": str(cid),
                "author": (c.get("author") or {}).get("login"),
                "created_at": c.get("createdAt"),
                "body": c.get("body"),
                "path": c.get("path"),
                "state": None,
                "url": c.get("url"),
            })

    my_pending = False
    for r in (node.get("reviews") or {}).get("nodes") or []:
        rid = r.get("id")
        if rid is None:
            continue
        state = r.get("state")
        author = (r.get("author") or {}).get("login")
        pending = state == "PENDING" or r.get("submittedAt") is None
        if pending and author == my_login:
            my_pending = True
        out.append({
            "kind": "review",
            "thread_id": None,
            "comment_id": str(rid),
            "author": author,
            "created_at": r.get("submittedAt"),
            "body": r.get("body"),
            "path": None,
            "state": state,
            "url": r.get("url"),
        })

    for c in (node.get("comments") or {}).get("nodes") or []:
        cid = c.get("databaseId")
        if cid is None:
            continue
        out.append({
            "kind": "issue",
            "thread_id": None,
            "comment_id": str(cid),
            "author": (c.get("author") or {}).get("login"),
            "created_at": c.get("createdAt"),
            "body": c.get("body"),
            "path": None,
            "state": None,
            "url": c.get("url"),
        })

    return out, my_pending


def _login(node: dict | None) -> str | None:
    return (node.get("author") or {}).get("login") if node else None


def detect_review_ping(node: dict, my_login: str) -> dict | None:
    """Detect an informal "please re-review" ping on an archived, still-open PR.

    Returns {ping_at, ping_author, ping_body} for the triggering comment, or None.

    A ping is a human (non-bot) conversation comment or review-thread reply by
    someone other than me and other than an active reviewer, posted after my last
    review activity (my last submitted review or my last comment). It is *not*
    raised (cleared) when, after that ping, any of these happened: a formal
    re-review request to me (already surfaced as RE), or another reviewer's
    activity - a submitted review by someone else, or a reply from someone who
    has a submitted review (the PR moved to a final reviewer doing their job). A
    later reply of my own is subsumed: it lifts my-last-activity past the ping.

    `node` is a fetch_archived_activity node: comments / reviewThreads.comments /
    reviews / timelineItems(REVIEW_REQUESTED_EVENT).
    """
    comments = (node.get("comments") or {}).get("nodes") or []
    thread_comments: list[dict] = []
    for t in (node.get("reviewThreads") or {}).get("nodes") or []:
        thread_comments.extend((t.get("comments") or {}).get("nodes") or [])
    all_comments = comments + thread_comments
    reviews = (node.get("reviews") or {}).get("nodes") or []
    events = (node.get("timelineItems") or {}).get("nodes") or []

    def created(c: dict) -> str:
        return c.get("createdAt") or ""

    # My last review activity: latest of my submitted reviews and my comments.
    my_times = [
        r["submittedAt"] for r in reviews
        if _login(r) == my_login and r.get("submittedAt")
    ]
    my_times += [created(c) for c in all_comments if _login(c) == my_login and created(c)]
    if not my_times:
        return None
    my_last = max(my_times)

    # Established reviewers: anyone but me or the PR author with a submitted
    # review. The author is never one: replying to an inline review comment
    # wraps the reply in a COMMENTED review object, and authors are the primary
    # ping source - their wrappers must neither exclude them nor clear a ping.
    pr_author = _login(node)
    other_reviewers = {
        _login(r) for r in reviews
        if r.get("submittedAt") and _login(r)
        and _login(r) not in (my_login, pr_author)
    }

    candidates = [
        c for c in all_comments
        if created(c) > my_last
        and _login(c) and _login(c) != my_login
        and _login(c) not in other_reviewers
        and not is_bot(_login(c))
    ]
    if not candidates:
        return None
    ping = max(candidates, key=created)
    ping_at = created(ping)

    # Formal re-request to me after the ping -> already visible as RE.
    if any(
        (ev.get("requestedReviewer") or {}).get("login") == my_login
        and (ev.get("createdAt") or "") > ping_at
        for ev in events
    ):
        return None
    # Another reviewer submitted a review after the ping.
    if any(
        _login(r) not in (my_login, pr_author)
        and r.get("submittedAt") and r["submittedAt"] > ping_at
        for r in reviews
    ):
        return None
    # An established reviewer replied after the ping.
    if any(_login(c) in other_reviewers and created(c) > ping_at for c in all_comments):
        return None

    return {"ping_at": ping_at, "ping_author": _login(ping), "ping_body": ping.get("body")}


def detect_push_since_review(node: dict, my_login: str) -> dict | None:
    """Detect a push landed after my last review on an archived, still-open PR.

    Returns {push_at, push_sha} for the current head commit, or None.

    No heuristic is involved and none is wanted: the author either pushed on top
    of the commit I reviewed or they did not. The anchor is the commit oid my
    latest submitted review was attached to, compared against the live head - so
    this stays true even when the PR has since moved on to a final reviewer whose
    questions prompted the push. That is precisely the case worth surfacing
    without dragging the PR back into the queue: my work on it is done, but the
    row on disk no longer describes what is on GitHub.

    Deliberately anchored on the review commit rather than review_snapshot: that
    table is only written when the reviewed sha *is* the head at fetch time and a
    patch is cached, which never holds for a PR that left the request set the
    moment I reviewed it.

    `node` is a fetch_archived_activity node: reviews.commit / commits / headRefOid.
    """
    reviews = (node.get("reviews") or {}).get("nodes") or []
    mine = [
        r for r in reviews
        if _login(r) == my_login and r.get("submittedAt")
        and (r.get("commit") or {}).get("oid")
    ]
    if not mine:
        return None
    reviewed_sha = max(mine, key=lambda r: r["submittedAt"])["commit"]["oid"]

    head_commits = (node.get("commits") or {}).get("nodes") or []
    head = (head_commits[-1].get("commit") or {}) if head_commits else {}
    head_sha = head.get("oid") or node.get("headRefOid")
    if not head_sha or head_sha == reviewed_sha:
        return None
    return {"push_at": head.get("committedDate") or "", "push_sha": head_sha}


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


# The third repo a robodoo bundle can carry. A change that moves data between
# modules ships its upgrade script here, on the same head branch as the addons
# halves - so it is a *companion* to the change, never a half of it.
# config.CompanionConfig.repo defaults to this and is what the fetch looks in.
COMPANION_REPO = "odoo/upgrade"


def _repo_of(pr: dict) -> str:
    """The repo a PR belongs to, read off its `owner/repo#number` id so this also
    works on the trimmed dicts callers assemble, not just on full cached rows."""
    return str(pr["id"]).rpartition("#")[0]


def detect_pairs(prs: list[dict]) -> dict[str, str]:
    """Map pr id -> paired pr id. Pair = same (author, head_branch) across exactly 2 PRs.

    Companion members are dropped before that count is taken. robodoo groups a
    bundle by head branch name across all three repos, so a data move whose
    migration lives in odoo/upgrade makes the group three - and "exactly 2" then
    found no pair at all, silently un-pairing the odoo<->enterprise halves this
    exists to find. The migration is attached to each half separately, by
    cli._refresh_companions.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for pr in prs:
        if _repo_of(pr) == COMPANION_REPO:
            continue
        groups.setdefault((pr["author"], pr["head_branch"]), []).append(pr)
    pairs: dict[str, str] = {}
    for group in groups.values():
        if len(group) == 2:
            a, b = group
            pairs[a["id"]] = b["id"]
            pairs[b["id"]] = a["id"]
    return pairs
