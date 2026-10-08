from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

from pr_dash import branch_set, runbot

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
    ci_state, _ = status_check_state(_head_commit(node).get("statusCheckRollup"))
    comment_block = node.get("comments") or {}
    review_block = node.get("reviews") or {}
    thread_block = node.get("reviewThreads") or {}
    comments = discussion_stream(node)
    # Movement signal: replies move no totalCount, but a new review, thread or comment does.
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
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "closed_at": node.get("closedAt"),
        "merged_at": node.get("mergedAt"),
        "fetched_at": fetched_at,
    }
    return row, comments


def _head_commit(node: dict) -> dict:
    return ((node.get("commits") or {}).get("nodes") or [{}])[0].get("commit", {})


def mine_row_from_node(node: dict, fetched_at: str) -> tuple[dict, list[dict]]:
    """Flatten an Authored PR node into (state columns, comments), the tracked ones plus more."""
    row, comments = tracked_row_from_node(node, fetched_at)
    requested = [
        r.get("requestedReviewer") or {}
        for r in (node.get("reviewRequests") or {}).get("nodes") or []
    ]
    events = []
    for e in (node.get("timelineItems") or {}).get("nodes") or []:
        reviewer = e.get("requestedReviewer") or {}
        if reviewer.get("login") or reviewer.get("slug"):
            events.append({
                "kind": "requested" if e["__typename"] == "ReviewRequestedEvent" else "removed",
                "reviewer": reviewer.get("login") or reviewer.get("slug"),
                "is_team": reviewer["__typename"] == "Team",
                "at": e.get("createdAt"),
            })
    row.update({
        "head_branch": node.get("headRefName") or "",
        "review_decision": node.get("reviewDecision"),
        "mergeable": node.get("mergeable"),
        "head_committed_at": _head_commit(node).get("committedDate"),
        "checks": [
            {"name": c["name"], "url": c["url"],
             "state": "failure" if c["failing"] else "pending" if c["pending"] else "success"}
            for c in _iter_checks(_head_commit(node).get("statusCheckRollup"))
        ],
        "requested_people": [r["login"] for r in requested if r.get("__typename") == "User"],
        "requested_teams": [r["slug"] for r in requested if r.get("__typename") == "Team"],
        "review_request_events": events,
    })
    return row, comments


def discussion_stream(node: dict) -> list[dict]:
    """Merge a PR node's conversation comments, reviews and inline threads, oldest first."""
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
    return comments


def group_discussion(stream: list[dict]) -> list[dict]:
    """Nest a Discussion stream into groups holding their threads, newest group first."""
    groups, threads = [], {}
    for c in stream:
        c = {**c, "is_bot": is_bot(c["author"]),
             "is_pending": c["kind"] == "review" and (c["state"] == "PENDING" or not c["created_at"])}
        if c["kind"] != "thread":
            groups.append({"kind": c["kind"], "entry": c, "threads": []})
            continue
        thread = threads.setdefault(c["thread_id"], {
            "thread_id": c["thread_id"], "path": c["path"], "state": c["state"],
            "parent_id": c["parent_id"], "comments": []})
        thread["comments"].append(c)
    # A bot review is hidden, so its threads stand alone as orphans.
    by_review = {g["entry"]["thread_id"]: g for g in groups
                 if g["kind"] == "review" and not g["entry"]["is_bot"]}
    for thread in threads.values():
        parent = by_review.get(thread.pop("parent_id"))
        if parent:
            parent["threads"].append(thread)
        else:
            groups.append({"kind": "orphan", "entry": None, "threads": [thread]})
    groups.sort(key=lambda g: (g["entry"] or g["threads"][0]["comments"][0])["created_at"] or "",
                reverse=True)
    for g in groups:
        g["threads"].sort(key=lambda t: t["comments"][0]["created_at"] or "")
    return groups


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


def thread_facts(stream: list[dict], login: str) -> list[dict]:
    """Each review thread's resolution, my participation and last reply, from a Discussion."""
    threads: dict[str, list[dict]] = {}
    for c in stream:
        if c["kind"] == "thread":
            threads.setdefault(c["thread_id"], []).append(c)
    return [{
        "thread_id": thread_id,
        "is_resolved": comments[0]["state"] == "RESOLVED",
        "i_participated": any(c["author"] == login for c in comments),
        "last_reply_at": comments[-1]["created_at"] or "",
        "last_reply_author": comments[-1]["author"] or "",
    } for thread_id, comments in threads.items()]


def unresolved_threads(stream: list[dict]) -> int:
    """How many review threads of a Discussion are unresolved."""
    return len({c["thread_id"] for c in stream if c["kind"] == "thread" and c["state"] == "UNRESOLVED"})


def awaiting_my_reply(facts: list[dict], login: str) -> bool:
    """True when an unresolved thread I took part in ends on someone else's comment."""
    return any(not t["is_resolved"] and t["i_participated"]
               and t["last_reply_author"] not in ("", login) for t in facts)


def has_pending_draft(node: dict, login: str) -> bool:
    """True when the node holds my unsent review, which GitHub shows only to its author."""
    return any(_login(r) == login and (r.get("state") == "PENDING" or r.get("submittedAt") is None)
               for r in (node.get("reviews") or {}).get("nodes") or [])


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
    """Yield each rollup check normalized to {name, url, failing, pending}, hiding the
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
                "pending": ctx.get("state") in ("PENDING", "EXPECTED"),
            }
        elif typename == "CheckRun":
            yield {
                "name": ctx.get("name") or "(check)",
                "url": ctx.get("detailsUrl"),
                "failing": (ctx.get("conclusion") or "") in _FAILING_CONCLUSIONS,
                # GitHub sets a check run's conclusion only once it has completed
                "pending": not ctx.get("conclusion"),
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


def now_utc(timespec: str = "seconds") -> str:
    return datetime.now(timezone.utc).isoformat(timespec=timespec)


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def days_since(iso_ts: str) -> int:
    return max(0, (datetime.now(timezone.utc) - parse_iso(iso_ts)).days)


# Odoo branch names carry the task id between dashes, e.g. master-l10n_ec-x-6396725-andg.
_BRANCH_TASK_RE = re.compile(r"-(\d{6,8})-")
_CI_WORST_FIRST = {"failure": "red", "pending": "pending", "success": "green"}


_BANDS = ("needs", "open", "done")


def branch_sets(
    members: list[dict], mergebot_states: dict[str, dict], *, streams: dict[str, list[dict]],
    login: str, acks: dict[str, str], seen: dict[str, dict], now: str,
) -> list[dict]:
    """Group Authored PRs into Branch sets, Needs you first, then Open, then Done.

    :param members: Authored PR rows, as db.list_mine returns them
    :param mergebot_states: pr id -> last stored Mergebot read, a mergebot.MergebotState as a dict
    :param streams: pr id -> its discussion stream oldest first, bots included
    :param login: the user's GitHub login
    :param acks: Branch set key -> the fingerprint it was Acknowledged at
    :param seen: pr id -> its mine_seen row from the last interactive look
    :param now: ISO time `idle Nd` counts to
    """
    forward_ports: dict[str, list[dict]] = {}
    for row in members:
        if row["source_id"]:
            forward_ports.setdefault(row["source_id"], []).append(row)
    sets = []
    for bs in branch_set.group(row for row in members if not row["source_id"]):
        key = bs.key[1]
        group = [(row, _mine_member(row, mergebot_states.get(row["id"]))) for row in bs.members]
        ids = {m["id"] for _, m in group}
        actions, fyi = [], []
        chains: list[tuple[dict, dict]] = []
        for row, m in group:
            m_actions, m_fyi = _member_attention(
                row, m, streams.get(m["id"], []), mergebot_states.get(m["id"]), login, ids,
                seen.get(m["id"]))
            actions += m_actions
            fyi += m_fyi
            chain = sorted(forward_ports.get(m["id"], []),
                           key=lambda r: branch_order(r["target_branch"]))
            m["fw"] = [_forward_port(r, mergebot_states.get(r["id"])) for r in chain]
            for r, f in zip(chain, m["fw"], strict=True):
                actions += _forward_port_attention(
                    r, f, streams.get(f["id"], []), mergebot_states.get(r["id"]), login)
                chains.append((r, f))
            fyi += _chain_labels(m)
        actions.sort(key=lambda a: parse_iso(a["since"]))
        done = all(m["state"] in ("MERGED", "CLOSED") for _, m in group) and all(
            f["state"] in ("MERGED", "CLOSED") for _, f in chains)
        updated = max((r["updated_at"] for r in [*(row for row, _ in group), *(r for r, _ in chains)]
                       if r["updated_at"]), default=None)
        if not done and updated and (idle := (parse_iso(now) - parse_iso(updated)).days) > 7:
            fyi.append(f"idle {idle}d")
        fingerprint = hashlib.sha1(json.dumps([
            sorted(row["head_sha"] for row, _ in group),
            sorted((a["member"], a["kind"], a["text"]) for a in actions),
            sorted((m["id"], m["ci"], m["ci_failing"]) for _, m in group),
            sorted((m["id"], row["activity_count"],
                    max((c["created_at"] or "" for c in streams.get(m["id"], [])), default=""))
                   for row, m in group),
            sorted((f["id"], r["head_sha"], f["ci"], f["ci_failing"], r["activity_count"],
                    max((c["created_at"] or "" for c in streams.get(f["id"], [])), default=""))
                   for r, f in chains),
        ]).encode()).hexdigest()[:16]
        acknowledged = acks.get(key) == fingerprint
        task = _BRANCH_TASK_RE.search(key)
        sets.append({
            "key": key,
            "task": task and task.group(1),
            "title": bs.primary["title"],
            "url": bs.primary["url"],
            "members": [m for _, m in group],
            "actions": actions,
            "action_lines": _action_lines(actions),
            "fyi": list(dict.fromkeys(fyi)),
            "acknowledged": acknowledged,
            "fingerprint": fingerprint,
            "band": "done" if done else "needs" if actions and not acknowledged else "open",
        })
    sets.sort(key=lambda s: max(m["updated_at"] or "" for m in s["members"]), reverse=True)
    sets.sort(key=lambda s: (
        _BANDS.index(s["band"]),
        parse_iso(s["actions"][0]["since"]).timestamp() if s["band"] == "needs" else 0,
    ))
    return sets


def _action_lines(actions: list[dict]) -> list[dict]:
    """Action items for the list view, one line per member and author for their waiting threads."""
    groups: dict[object, list[dict]] = {}
    for i, a in enumerate(actions):
        groups.setdefault((a["member"], a["author"]) if a["kind"] == "thread" else i, []).append(a)
    lines = []
    for group in groups.values():
        first = group[0]
        if len(group) == 1:
            lines.append(first)
            continue
        files = len({a["path"] for a in group if a["path"]})
        where = f" on {files} file{'s' * (files > 1)}" if files else ""
        lines.append({**first, "text": f"{first['author']} is waiting in {len(group)} threads{where}"})
    return lines


def _member_attention(
    row: dict, m: dict, stream: list[dict], mergebot: dict | None, login: str,
    set_ids: set[str], prev: dict | None,
) -> tuple[list[dict], list[str]]:
    """(Action items, FYI labels) of one member, which owes nothing while draft or resolved."""
    actions: list[dict] = []
    fyi: list[str] = []
    head_at = row["head_committed_at"] or row["fetched_at"]

    def act(kind: str, text: str, since: str, **extra) -> None:
        actions.append({"member": m["ref"], "kind": kind, "text": text, "since": since, **extra})

    # A row added but not fetched yet carries no state to judge.
    if m["state"] == "OPEN" and not m["draft"] and row["fetched_at"]:
        threads = {c["thread_id"]: c for c in stream if c["kind"] == "thread"}
        for c in threads.values():
            if c["state"] == "UNRESOLVED" and c["author"] != login:
                where = f" on {c['path']}" if c["path"] else ""
                act("thread", f"{c['author']} is waiting in a thread{where}", c["created_at"],
                    author=c["author"], path=c["path"])
        if m["ci"] == "red":
            act("ci", "CI red: " + ", ".join(m["ci_failing"]), head_at)
        if m["conflict"]:
            act("conflict", "merge conflict", head_at)
        verdicts = {
            c["author"]: c for c in stream
            if c["kind"] == "review" and c["author"] != login
            and c["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
        }
        changes = [c for c in verdicts.values() if c["state"] == "CHANGES_REQUESTED"]
        if changes:
            latest = max(changes, key=lambda c: parse_iso(c["created_at"]))
            pushed = row["head_committed_at"]
            if not pushed or parse_iso(latest["created_at"]) > parse_iso(pushed):
                by = ", ".join(sorted(c["author"] for c in changes))
                act("changes", f"changes requested by {by}", latest["created_at"])
            else:
                fyi.append("waiting on re-review")
        # GitHub drops a person's request once they review, so a review since the push counts.
        reviewed = any(
            (c["kind"] == "thread" or c["state"] in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"))
            and c["author"] != login and not is_bot(c["author"])
            and (not row["head_committed_at"]
                 or parse_iso(c["created_at"]) > parse_iso(row["head_committed_at"]))
            for c in stream
        )
        if not m["requested_people"] and not reviewed:
            removed = [e["at"] for e in row["review_request_events"]
                       if e["kind"] == "removed" and not e["is_team"]]
            text = "only teams requested" if m["requested_teams"] else "no reviewer requested"
            act("reviewers", text, max(removed, default=row["created_at"]))
        for pr in mergebot["linked"] if mergebot else []:
            # Its own member raises its own items, and a missing r+ waits on a reviewer.
            if (pr["ready"] is False and f"{pr['repo']}#{pr['number']}" not in set_ids
                    and set(pr["blockers"]) != {"missing r+"}):
                blockers = ", ".join(pr["blockers"]) or "not ready"
                act("linked", f"linked {pr['repo']}#{pr['number']}: {blockers}", head_at)

        for kind, text, since in _bot_attention(row, m, stream, mergebot, login):
            act(kind, text, since)

    if prev is None or not prev["fetched_at"]:
        return actions, fyi
    mark = parse_iso(prev["fetched_at"])
    for c in stream:
        if c["author"] == login or parse_iso(c["created_at"]) <= mark:
            continue
        if c["author"] in _BOT_LOGINS:
            kind, text = _bot_comment(c, stream, login)
            if kind == "fyi" and text:
                fyi.append(text)
        elif is_bot(c["author"]):
            continue
        elif c["kind"] == "review" and c["state"] == "APPROVED":
            fyi.append("approved · r+ missing" if m["r_plus"] is False else "approved")
        else:
            fyi.append("new reply")
    if m["r_plus"] and not prev["r_plus"]:
        fyi.append("r+")
    for e in row["review_request_events"]:
        if e["is_team"] or parse_iso(e["at"]) <= mark:
            continue
        if e["kind"] == "requested":
            fyi.append(f"reviewer added: {e['reviewer']}")
        elif m["requested_people"]:
            fyi.append(f"reviewer removed: {e['reviewer']}")
    return actions, fyi


def _forward_port(row: dict, mergebot: dict | None) -> dict:
    """One Forward-port of a member, flagged when it is open and conflicting or red."""
    m = _mine_member(row, mergebot)
    flag = None
    if m["state"] == "OPEN":
        flag = "conflict" if m["conflict"] else "red" if m["ci"] == "red" else None
    keep = ("id", "ref", "num", "repo", "url", "state", "ci", "ci_failing", "override",
            "conflict", "mergebot_unknown")
    return {**{k: m[k] for k in keep}, "base": row["target_branch"], "flag": flag}


def _forward_port_attention(
    row: dict, f: dict, stream: list[dict], mergebot: dict | None, login: str,
) -> list[dict]:
    """Action items of one open Forward-port: conflict, red CI, a bot failure or a human waiting."""
    if f["state"] != "OPEN":
        return []
    head_at = row["head_committed_at"] or row["fetched_at"]
    items = []
    if f["conflict"]:
        items.append(("fw", "merge conflict", head_at))
    if f["ci"] == "red":
        items.append(("fw", "CI red: " + ", ".join(f["ci_failing"]), head_at))
    humans = [c for c in stream if not is_bot(c["author"])]
    if humans and humans[-1]["author"] != login:
        items.append(("fw", f"{humans[-1]['author']} commented", humans[-1]["created_at"]))
    for kind, text, since in _bot_attention(row, f, stream, mergebot, login):
        items.append(("command" if kind == "command" else "fw", text, since))
    return [{"member": f["ref"], "kind": kind, "text": f"forward-port to {f['base']}: {text}",
             "since": since} for kind, text, since in items]


# Bot phrases, see odoo/runbot runbot_merge/data/runbot_merge.pull_requests.feedback.template.csv
_BOT_ACTION_RE = re.compile(
    r"staging failed|failed on this reviewed PR|has failed CI|unable to stage|how to merge it"
    r"|cherrypicking of pull request \S+ failed|is in conflict|did not succeed"
    r"|can't be used as a forward port target",
    re.IGNORECASE,
)
_BOT_REJECTION_RE = re.compile(
    r"I'm afraid I can't do that|you can't review\+|already reviewed"
    r"|I can only do this on unmodified forward-port PRs",
    re.IGNORECASE,
)
_BOT_FYI = [
    (re.compile(p, re.IGNORECASE), label) for p, label in (
        ("Pull request status dashboard", None),
        (r"linked pull request\(s\) .* not ready", None),
        ("Currently available commands", None),
        ("of the forward-port chain", None),
        ("Merge method set to", "merge method set"),
        ("Forward-porting to '([^']+)'", "forward-porting to {}"),
        ("Starting forward-port", "forward-port started"),
        ("Disabled forward-porting", "forward-porting disabled"),
        ("forward-port PRs awaiting action", "forward-ports awaiting action"),
        ("has become a normal PR", "forward-port detached"),
    )
]
_BOT_ADDRESS_RE = re.compile(r"^(?:I'm sorry, )?(?:@[\w-]+[\s.:,]*)+")
_MENTION_RE = re.compile(r"@([\w-]+)")


def _bot_sentence(body: str | None) -> str:
    """The first sentence of a bot comment, without its leading @mentions or runbot link."""
    line = _BOT_ADDRESS_RE.sub("", (body or "").strip().partition("\n")[0])
    line = re.split(r"\.(?:\s|$)| \(view more", line)[0].rstrip(":")
    return re.sub(r"\b([0-9a-f]{7})[0-9a-f]{33}\b", r"\1", " ".join(line.split()))


def _bot_comment(c: dict, stream: list[dict], login: str) -> tuple[str, str | None]:
    """Classify a robodoo or fw-bot comment as ("bot" | "command" | "fyi", text or None)."""
    body = c["body"] or ""
    if _BOT_REJECTION_RE.search(body):
        commander = (_MENTION_RE.findall(body) or [""])[0]
        mention = f"@{c['author']}".lower()
        command = next((
            e for e in reversed(stream[:stream.index(c)])
            if e["author"] == commander and mention in (e["body"] or "").lower()
        ), None)
        if commander == login and command:
            cmd = next(ln for ln in command["body"].splitlines() if mention in ln.lower())
            return "command", f"{c['author']} rejected \"{cmd.strip()}\": {_bot_sentence(body)}"
        return "fyi", f"{c['author']} rejected {commander}'s command"
    if _BOT_ACTION_RE.search(body):
        return "bot", f"{c['author']}: {_bot_sentence(body)}"
    for pattern, label in _BOT_FYI:
        if found := pattern.search(body):
            return "fyi", label and label.format(*found.groups())
    return "fyi", f"{c['author']} commented"


def _bot_attention(
    row: dict, m: dict, stream: list[dict], mergebot: dict | None, login: str,
) -> list[tuple[str, str, str]]:
    """(kind, text, since) of each bot failure that no push, command or page has settled."""
    managed = mergebot is not None and mergebot["state"] not in ("unmanaged", "unknown")
    checks = {c["name"] for c in row["checks"]} | {
        c["name"] for c in (mergebot["checks"] if managed else [])}
    out: dict[tuple[str, str], str] = {}
    for i, c in enumerate(stream):
        if c["author"] not in _BOT_LOGINS:
            continue
        kind, text = _bot_comment(c, stream, login)
        if kind == "fyi" or (managed and mergebot["state"] == "staged"):
            continue
        later = stream[i + 1:]
        if kind == "command":
            mention = f"@{c['author']}".lower()
            if not any(e["author"] == login and mention in (e["body"] or "").lower()
                       for e in later):
                out.setdefault((kind, text), c["created_at"])
            continue
        if row["head_committed_at"] and parse_iso(row["head_committed_at"]) > parse_iso(
                c["created_at"]):
            continue
        phrase = _BOT_ACTION_RE.search(c["body"]).group(0).lower()
        failed_check = re.search(r"'([^']+)' failed on this reviewed PR", c["body"])
        # The page and the GitHub checks answer CI, conflicts and readiness, so they win.
        settled = {
            "staging failed": managed and mergebot["state"] != "error",
            "failed on this reviewed pr": failed_check and failed_check.group(1) in checks,
            "has failed ci": m["ci"] is not None,
            "unable to stage": m["conflict"],
            "how to merge it": managed and mergebot["merge_method"],
            "can't be used as a forward port target": any(
                e["author"] in _BOT_LOGINS and "Forward-porting to" in (e["body"] or "")
                for e in later),
        }.get(phrase, phrase.startswith("cherrypicking") and m["conflict"])
        if not settled:
            out.setdefault((kind, text), c["created_at"])
    return [(kind, text, since) for (kind, text), since in out.items()]


def _chain_labels(m: dict) -> list[str]:
    """The in-between labels of a member's Chain: its Source Merged and how many ports followed."""
    merged = sum(f["state"] == "MERGED" for f in m["fw"])
    labels = []
    if m["state"] == "MERGED" and any(f["state"] == "OPEN" for f in m["fw"]):
        labels.append("source merged")
    if merged:
        labels.append(f"fw {merged}/{len(m['fw'])} merged")
    return labels


def branch_order(branch: str) -> tuple:
    """Sort key putting Odoo branches in release order, 19.0 < saas-19.1 < 20.0 < master."""
    if branch == "master":
        return (math.inf,)
    return tuple(int(n) for n in re.findall(r"\d+", branch))


# fw-bot writes one line per ancestor, odoo#291981 names odoo#291857 then odoo#290657.
_FORWARD_PORT_OF_RE = re.compile(r"^Forward-Port-Of: ([\w.-]+/[\w.-]+#\d+)\s*$", re.MULTILINE)


def forward_port_ancestors(body: str | None) -> list[str]:
    """The `owner/repo#n` ids a Forward-port body names as its ancestors."""
    return _FORWARD_PORT_OF_RE.findall(body or "")


def forward_port_candidates(node: dict) -> list[str]:
    """Ids of the bot-authored PRs cross-referencing `node`, its Forward-ports among them."""
    out = []
    for e in (node.get("crossReferences") or {}).get("nodes") or []:
        source = e.get("source") or {}
        if source.get("number") and is_bot(_login(source)):
            out.append(f"{source['repository']['nameWithOwner']}#{source['number']}")
    return out


def runbot_triggers(row: dict, snapshots: dict[str, dict], mergebot: dict | None) -> list[dict]:
    """A Mine row's failing runbot checks with their snapshot, named by the check."""
    before = {c["name"]: c["state"] for c in row["previous_checks"]}
    states = check_states(row, mergebot)
    out = []
    for c in row["checks"]:
        ids = runbot.parse_check_url(c["url"] or "")
        if not ids or states.get(c["name"]) != "failure":
            continue
        snap = snapshots.get(f"build:{ids[1]}")
        # A red check the refresh has not read yet still shows, as GitHub reports it.
        trigger = (snap["triggers"][0] if snap and snap["triggers"]
                   else {"verdict": "red", "children": None, "failures": []})
        red = trigger["verdict"] == "red"
        out.append({**trigger, "name": c["name"], "batch_id": ids[0], "build_id": ids[1],
                    "url": runbot.build_url(ids[1]),
                    "error": snap["error"] if snap else "not fetched yet",
                    "fetched_at": snap and snap["fetched_at"],
                    "stale": bool(snap and snap["error"] and snap["triggers"]),
                    "previous": {"failure": "red", "success": "green"}.get(before.get(c["name"]))
                    if red else None})
    return out


def runbot_batches(rows: list[dict], pages: dict[str, dict],
                   snapshots: dict[str, dict]) -> list[dict]:
    """The runbot batches of open `rows`, each build once with the PRs reporting it."""
    batches: dict[int, dict] = {}
    for row in rows:
        if row["state"] != "OPEN":
            continue
        for entry in runbot_triggers(row, snapshots, pages.get(row["id"])):
            batch_id = entry.pop("batch_id")
            batch = batches.setdefault(batch_id, {"batch_id": batch_id, "prs": [], "triggers": {}})
            batch["prs"] += [row["id"]] if row["id"] not in batch["prs"] else []
            batch["triggers"].setdefault(entry["build_id"], {**entry, "prs": []})["prs"].append(
                row["id"])
    return [{**b, "triggers": list(b["triggers"].values())} for b in batches.values()]


def check_states(row: dict, mergebot: dict | None) -> dict[str, str]:
    """Each check's state as the Mergebot page reads it, where the PR has one."""
    managed = mergebot is not None and mergebot["state"] not in ("unmanaged", "unknown")
    page_checks = mergebot["checks"] if managed else []
    listed = {c["name"] for c in page_checks}
    # The page lists every runbot check it requires, so `ci/runbot (light)` is optional.
    checks = {
        c["name"]: c["state"] for c in row["checks"]
        if not (managed and c["name"] not in listed and "runbot.odoo.com" in (c["url"] or ""))
    }
    for c in page_checks:
        if c["overridden"] or c["status"] == "ok":
            checks[c["name"]] = "success"
        elif c["status"] == "fail":
            checks[c["name"]] = "failure"
        elif c["status"] is not None:
            checks[c["name"]] = "pending"
    return checks


def _mine_member(row: dict, mergebot: dict | None) -> dict:
    """One Branch set member, its readiness read from the Mergebot page where there is one."""
    managed = mergebot is not None and mergebot["state"] not in ("unmanaged", "unknown")
    page_checks = mergebot["checks"] if managed else []
    checks = check_states(row, mergebot)
    state = row["state"]
    if state == "CLOSED" and managed and mergebot["state"] == "merged":
        state = "MERGED"
    return {
        "id": row["id"],
        "repo": row["repo"],
        "num": row["number"],
        "ref": f"{row['repo'].split('/')[-1]}#{row['number']}",
        "title": row["title"],
        "url": row["url"],
        "state": state,
        "draft": bool(row["is_draft"]),
        "ci": next((w for s, w in _CI_WORST_FIRST.items() if s in checks.values()), None),
        "ci_failing": [name for name, s in checks.items() if s == "failure"],
        "override": [
            {"check": c["name"], "by": c["overridden_by"]} for c in page_checks if c["overridden"]
        ],
        "decision": row["review_decision"],
        "r_plus": mergebot["r_plus"] if managed else None,
        "requested_people": row["requested_people"],
        "requested_teams": row["requested_teams"],
        "conflict": row["mergeable"] == "CONFLICTING",
        "updated_at": row["updated_at"],
        "mergebot_unknown": mergebot is None or mergebot["state"] == "unknown",
        "dismissed_at": row["dismissed_at"],
    }
