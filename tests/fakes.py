"""In-memory GitHub, AI reviewer and clock for tests, with the GraphQL node builders they share."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pr_dash import ai, github, mergebot

if TYPE_CHECKING:
    from collections.abc import Callable

T0 = "2026-07-01T00:00:00Z"

_EVENT_TYPES = {"requested": "ReviewRequestedEvent", "removed": "ReviewRequestRemovedEvent"}
_TOKEN = re.compile(r"\.\.\.|\$?\w+|[{}():]")


def _selection(text: str, start: str) -> dict:
    """Parse the GraphQL selection opening at `start` in `text`, as key -> (sub, types, cut).

    :return: per selected key, its own selection, the item types it keeps and the nodes it cuts
    """
    toks = _TOKEN.findall(text[text.index(start) + len(start):])
    sel: dict = {}

    def merge(into: dict, other: dict) -> None:
        for key, (sub, types, cut) in other.items():
            if key in into and sub:
                merge(into[key][0], sub)
            else:
                into[key] = (sub, types, cut)

    def parse(into: dict) -> None:
        while (tok := toks.pop(0)) != "}":
            if tok == "...":
                spread = toks.pop(0)
                if spread == "on":
                    toks[:2] = []
                    parse(into)
                else:
                    merge(into, _selection(text, f"fragment {spread} on PullRequest {{"))
                continue
            if toks[0] == ":":
                toks[:2] = []
            args = []
            if toks[0] == "(":
                while (arg := toks.pop(0)) != ")":
                    args.append(arg)
            sub: dict = {}
            if toks[0] == "{":
                toks.pop(0)
                parse(sub)
            size = next((int(v) for k, v in zip(args, args[2:]) if k in ("first", "last")
                         and v.isdecimal()), None)
            # A connection selecting pageInfo is paged through to the end by the real adapter.
            cut = (None if size is None or "nodes" not in sub or "pageInfo" in sub
                   else slice(-size, None) if "last" in args else slice(size))
            types = {t.title().replace("_", "") for t in args if t.isupper()}
            merge(into, {tok: (sub or None, types, cut)})

    parse(sel)
    return sel


def _project(value, shape: dict | None):
    """Keep of `value` only what `shape` selects, as GitHub answers it."""
    if isinstance(value, list):
        return [_project(v, shape) for v in value]
    if shape is None or not isinstance(value, dict):
        return value
    out = {}
    for key, (sub, types, cut) in shape.items():
        if key not in value:
            continue
        v = value[key]
        if isinstance(v, dict) and "nodes" in v and (types or cut):
            nodes = [n for n in v["nodes"] if not types or n.get("__typename") in types]
            v = {**v, "nodes": nodes[cut or slice(None)]}
        out[key] = _project(v, sub)
    return out


# Each view's node as the real adapter's query selects it.
SHAPES = {
    "queue": _selection(github.PR_NODE_FRAGMENT, "fragment PRFields on PullRequest {"),
    "tracked": _selection(github.TRACKED_NODE_FRAGMENT, "fragment TrackedFields on PullRequest {"),
    "mine": _selection(github.MINE_NODE_FRAGMENT, "fragment MineFields on PullRequest {"),
    "activity": _selection("{" + github._ARCHIVED_ACTIVITY_FIELDS + "}", "{"),
    "reviewed_by": _selection(github.REVIEWED_BY_QUERY, "... on PullRequest {"),
}
SHAPES["history"] = SHAPES["mine"]


@dataclass
class FakePR:
    """One PR as GitHub holds it, in the terms the derive parsers read.

    :param events: review request history, as {kind: requested|removed, reviewer, at}
    :param reviews: {author, state, at, commit} plus optional body and id
    :param comments: conversation comments, as {author, at, body}
    :param threads: {path, comments} plus optional id and resolved, a comment's review its id
    :param checks: CI context name -> state, the rollup state derived from them
    :param cross_refs: (repo, number, author) of the PRs cross-referencing this one
    """
    repo: str
    number: int
    author: str = "a"
    title: str = "T"
    body: str = "b"
    base: str = "18.0"
    head_branch: str = "feat"
    head_sha: str = "sha1"
    state: str = "OPEN"
    draft: bool = False
    created_at: str = T0
    updated_at: str = T0
    pushed_at: str = T0
    closed_at: str | None = None
    merged_at: str | None = None
    mergeable: str = "MERGEABLE"
    checks: dict[str, str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    patch: str | None = None
    requested: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    threads: list[dict] = field(default_factory=list)
    cross_refs: list[tuple[str, int, str]] = field(default_factory=list)
    subscribed: bool = False

    @property
    def id(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


def _user(login: str) -> dict:
    return {"__typename": "User", "login": login}


def _page(nodes: list[dict]) -> dict:
    return {"totalCount": len(nodes), "nodes": nodes}


def _rollup(checks: dict[str, str]) -> dict | None:
    if not checks:
        return None
    states = set(checks.values())
    state = ("FAILURE" if states & {"FAILURE", "ERROR"}
             else "PENDING" if "PENDING" in states else "SUCCESS")
    return {"state": state, "contexts": {"nodes": [
        {"__typename": "StatusContext", "context": name, "state": s, "targetUrl": None}
        for name, s in checks.items()
    ]}}


def pr_node(pr: FakePR, view: str = "queue", **over) -> dict:
    """The node GitHub returns for `pr` under `view`'s query, `over` replacing top-level keys."""
    reviews = [
        {"id": r.get("id", f"R{i}"), "author": {"login": r["author"]}, "state": r["state"],
         "submittedAt": r["at"], "body": r.get("body", ""), "url": f"{pr.url}#review-{i}",
         "commit": {"oid": r["commit"]}}
        for i, r in enumerate(pr.reviews)
    ]
    comments = [
        {"author": {"login": c["author"]}, "createdAt": c["at"], "body": c["body"],
         "databaseId": 100 + i, "url": f"{pr.url}#issuecomment-{100 + i}"}
        for i, c in enumerate(pr.comments)
    ]
    threads = [
        {"id": t.get("id", f"T{j}"), "isResolved": t.get("resolved", False), "path": t["path"],
         "comments": {"nodes": [
             {"author": {"login": c["author"]}, "createdAt": c["at"], "body": c["body"],
              "path": t["path"], "databaseId": 1000 * (j + 1) + k,
              "url": f"{pr.url}#discussion_r{1000 * (j + 1) + k}",
              "pullRequestReview": {"id": c["review"]} if c.get("review") else None}
             for k, c in enumerate(t["comments"])
         ]}}
        for j, t in enumerate(pr.threads)
    ]
    timeline = sorted(
        [{"__typename": _EVENT_TYPES[e["kind"]], "createdAt": e["at"],
          "requestedReviewer": _user(e["reviewer"])} for e in pr.events]
        + [{"__typename": "PullRequestReview", "author": r["author"],
            "submittedAt": r["submittedAt"]} for r in reviews],
        key=lambda e: e.get("createdAt") or e["submittedAt"],
    )
    node = {
        "id": f"PR_{pr.id}", "url": pr.url, "number": pr.number, "title": pr.title, "state": pr.state,
        "isDraft": pr.draft, "body": pr.body, "createdAt": pr.created_at,
        "updatedAt": pr.updated_at, "closedAt": pr.closed_at, "mergedAt": pr.merged_at,
        "mergeable": pr.mergeable, "reviewDecision": None, "additions": 1, "deletions": 0, "changedFiles": len(pr.files),
        "baseRefName": pr.base, "headRefName": pr.head_branch, "headRefOid": pr.head_sha,
        "author": {"login": pr.author}, "repository": {"nameWithOwner": pr.repo},
        "reviewRequests": {"nodes": [{"requestedReviewer": _user(r)} for r in pr.requested]},
        "latestReviews": {"nodes": list({r["author"]["login"]: r for r in reviews}.values())},
        "reviews": _page(reviews),
        "comments": _page(comments),
        "reviewThreads": _page(threads),
        "commits": {"nodes": [{"commit": {"oid": pr.head_sha, "committedDate": pr.pushed_at,
                                          "statusCheckRollup": _rollup(pr.checks)}}]},
        "timelineItems": {"nodes": timeline},
        "files": _page([{"path": path} for path in pr.files]),
        "crossReferences": {"nodes": [
            {"__typename": "CrossReferencedEvent", "source": {"number": n, "author": {"login": a}, "repository": {"nameWithOwner": r}}}
            for r, n, a in pr.cross_refs
        ]},
    }
    return _project({**node, **over}, SHAPES[view])


def branch_hit(pr: FakePR) -> dict:
    """The entry the head branch search returns for `pr`."""
    return {"number": pr.number, "title": pr.title, "state": pr.state, "draft": pr.draft,
            "url": pr.url, "head_branch": pr.head_branch, "head_sha": pr.head_sha,
            "author": pr.author}


class FakeClock:
    """The current UTC time, moving only when a test advances it."""

    def __init__(self, now: datetime = datetime(2026, 7, 1, tzinfo=UTC)):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class FakeGitHub:
    """`github.GitHub` answered from seeded PRs, logging each call as (method, *args)."""

    def __init__(self, clock: FakeClock | None = None):
        self.clock = clock or FakeClock()
        self.prs: dict[str, FakePR] = {}
        self.calls: list[tuple] = []
        self._failing: dict[str, Callable[..., bool]] = {}

    def add(self, repo: str, number: int, **fields) -> FakePR:
        pr = self.prs[f"{repo}#{number}"] = FakePR(repo, number, **fields)
        return pr

    def fail(self, method: str, when: Callable[..., bool] | None = None) -> None:
        """Make `method` raise GithubError, on all calls or the ones whose args satisfy `when`."""
        self._failing[method] = when or (lambda *args: True)

    def _touch(self, pr_id: str) -> FakePR:
        pr = self.prs[pr_id]
        pr.updated_at = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        return pr

    def push(self, pr_id: str, sha: str) -> None:
        pr = self._touch(pr_id)
        pr.head_sha, pr.pushed_at = sha, pr.updated_at

    def request(self, pr_id: str, login: str) -> None:
        pr = self._touch(pr_id)
        pr.requested.append(login)
        pr.events.append({"kind": "requested", "reviewer": login, "at": pr.updated_at})

    def unrequest(self, pr_id: str, login: str) -> None:
        pr = self._touch(pr_id)
        pr.requested.remove(login)
        pr.events.append({"kind": "removed", "reviewer": login, "at": pr.updated_at})

    def review(self, pr_id: str, login: str, state: str = "APPROVED", body: str = "") -> None:
        pr = self._touch(pr_id)
        # Submitting a review fulfils the request, as on GitHub.
        if login in pr.requested:
            pr.requested.remove(login)
        pr.reviews.append({"author": login, "state": state, "at": pr.updated_at,
                           "commit": pr.head_sha, "body": body})

    def comment(self, pr_id: str, login: str, body: str) -> None:
        pr = self._touch(pr_id)
        pr.comments.append({"author": login, "at": pr.updated_at, "body": body})

    def close(self, pr_id: str, state: str = "CLOSED") -> None:
        pr = self._touch(pr_id)
        pr.state, pr.closed_at = state, pr.updated_at
        if state == "MERGED":
            pr.merged_at = pr.updated_at

    def _call(self, method: str, *args) -> None:
        self.calls.append((method, *args))
        when = self._failing.get(method)
        if when and when(*args):
            raise github.GithubError(f"{method}: HTTP 502", retryable=True)

    def _found(self, refs: list[tuple[str, int]]) -> list[FakePR]:
        return [pr for repo, number in refs if (pr := self.prs.get(f"{repo}#{number}"))]

    def review_requested(self, login):
        self._call("review_requested", login)
        return [pr_node(pr) for pr in self.prs.values()
                if pr.state == "OPEN" and login in pr.requested], None

    def reviewed_by(self, login, *, limit=1000, since=None):
        self._call("reviewed_by", login, limit, since)
        hits = sorted((pr for pr in self.prs.values()
                       if any(r["author"] == login for r in pr.reviews)
                       and (not since or pr.updated_at >= since)),
                      key=lambda pr: pr.updated_at, reverse=True)
        # The query asks only for `login`'s reviews.
        return [pr_node(replace(pr, reviews=[r for r in pr.reviews if r["author"] == login]),
                        "reviewed_by") for pr in hits[:limit]], None

    def reviewed_among(self, refs, login):
        self._call("reviewed_among", refs, login)
        return {pr.id for pr in self._found(refs) if any(r["author"] == login for r in pr.reviews)}

    def archived_activity(self, refs):
        self._call("archived_activity", refs)
        return {pr.id: pr_node(pr, "activity") for pr in self._found(refs)}

    def head_sha(self, repo, number):
        self._call("head_sha", repo, number)
        return next((pr.head_sha for pr in self._found([(repo, number)])), None)

    def open_prs_by_head_branch(self, repo, branches):
        self._call("open_prs_by_head_branch", repo, branches)
        # Oldest first, so the newest of several open PRs on one branch wins, as sorted on GitHub.
        return {pr.head_branch: branch_hit(pr)
                for pr in sorted(self.prs.values(), key=lambda pr: pr.created_at)
                if pr.repo == repo and pr.state == "OPEN" and pr.head_branch in branches}

    def pr_states(self, repo, numbers):
        self._call("pr_states", repo, numbers)
        return {pr.number: pr.state for pr in self._found([(repo, n) for n in numbers])}

    def nodes(self, refs, view):
        self._call("nodes", refs, view)
        return {pr.id: pr_node(pr, view) for pr in self._found(refs)}

    def authored_open(self, login):
        self._call("authored_open", login)
        return [(pr.repo, pr.number) for pr in self.prs.values()
                if pr.author == login and pr.state == "OPEN"]

    def authored_closed(self, login):
        self._call("authored_closed", login)
        return [(pr.repo, pr.number) for pr in self.prs.values()
                if pr.author == login and pr.state != "OPEN"]

    def manual_subscriptions(self):
        self._call("manual_subscriptions")
        return [{"id": pr.id, "repo": pr.repo, "number": pr.number, "url": pr.url,
                 "title": pr.title, "updated_at": pr.updated_at}
                for pr in self.prs.values() if pr.subscribed]

    def patch(self, repo, number):
        self._call("patch", repo, number)
        return next((pr.patch for pr in self._found([(repo, number)])), None)


class FakeMergebot:
    """`mergebot.fetch` stand-in answering each seeded page state, "unknown" for the rest."""

    def __init__(self):
        self.pages: dict[str, str] = {}
        self.reads: list[str] = []

    def __call__(self, repo: str, number: int) -> mergebot.MergebotState:
        self.reads.append(f"{repo}#{number}")
        return mergebot.MergebotState(self.pages.get(f"{repo}#{number}", "unknown"))


class FakeReviewer:
    """`ai.review_batch` stand-in recording requests, answering looks-good unless set to fail."""

    def __init__(self):
        self.requests: list[ai.ReviewRequest] = []
        self.failures: dict[str, str] = {}

    def __call__(self, reqs, **kwargs) -> list[ai.ReviewOutcome]:
        self.requests.extend(reqs)
        return [
            ai.ReviewOutcome(r.head_sha, None, self.failures[r.head_sha])
            if r.head_sha in self.failures
            else ai.ReviewOutcome(r.head_sha, ai.ReviewResult(r.head_sha, "fake review", [],
                                                              "looks-good"))
            for r in reqs
        ]


def insert_pr(conn, pr_id: str, *, author="a", head_branch="feat", state="OPEN",
              archived_at=None, previously_reviewed=1, head_sha="sha1", updated=T0) -> None:
    """Write a bare Review queue row straight into the cache."""
    repo, number = pr_id.split("#")
    conn.execute(
        "INSERT INTO pr (id, repo, number, title, url, author, target_branch, "
        "head_branch, head_sha, created_at, updated_at, review_requested_at, "
        "previously_reviewed, additions, deletions, changed_files, state, "
        "archived_at, fetched_at) VALUES (?,?,?,'','',?,?,?,?,?,?,?,?,0,0,0,?,?,?)",
        (pr_id, repo, int(number), author, "18.0", head_branch, head_sha,
         T0, updated, T0, previously_reviewed, state, archived_at, T0),
    )
