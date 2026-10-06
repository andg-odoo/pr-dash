from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Literal, Protocol

log = logging.getLogger("pr_dash.github")

_PAGE_INFO = "pageInfo { hasNextPage endCursor }"


@dataclass(frozen=True)
class _Pages:
    """Node fields of the paged connections, shared with their follow-up pages."""
    reviews: str
    comments: str
    threads: str
    thread_comments: str
    files: str = ""


# One field set for the Discussion in every view, so a field is added in one place.
_PAGES = _Pages(
    reviews="id author { login } state submittedAt body url",
    comments="author { login } createdAt body databaseId url",
    threads="id isResolved path",
    thread_comments="author { login } createdAt body path databaseId url pullRequestReview { id }",
    files="path",
)


def _connection(name: str, args: str, fields: str) -> str:
    return f"{name}({args}) {{ totalCount {_PAGE_INFO} nodes {{ {fields} }} }}"


def _thread_fields(pages: _Pages, replies: int) -> str:
    return f"{pages.threads} " + _connection("comments", f"first: {replies}", pages.thread_comments)


def _connections(pages: _Pages, size: int, replies: int) -> str:
    return "\n  ".join([
        _connection("reviews", f"first: {size}", pages.reviews),
        _connection("comments", f"first: {size}", pages.comments),
        _connection("reviewThreads", f"first: {size}", _thread_fields(pages, replies)),
    ])


def _complete_pages(nodes: list[dict], pages: _Pages, *, chunk_size: int = 25) -> None:
    """Fetch in place the later pages of every paged connection, one aliased request per round."""
    def _overflowing(conns):
        return [c for c in conns if c[3] and (c[3].get("pageInfo") or {}).get("hasNextPage")]

    pending = _overflowing(
        [(n["id"], "PullRequest", name, n.get(name), fields) for n in nodes if n.get("id")
         for name, fields in (("reviews", pages.reviews), ("comments", pages.comments),
                              ("reviewThreads", _thread_fields(pages, 100)), ("files", pages.files))
         if fields]
        + [(t["id"], "PullRequestReviewThread", "comments", t.get("comments"), pages.thread_comments)
           for n in nodes for t in (n.get("reviewThreads") or {}).get("nodes") or []]
    )
    while pending:
        new_threads = []
        for start in range(0, len(pending), chunk_size):
            chunk = pending[start:start + chunk_size]
            parts = [
                f'c{i}: node(id: {json.dumps(owner)}) {{ ... on {typename} {{ ' + _connection(
                    name, f"first: 100, after: {json.dumps(conn['pageInfo']['endCursor'])}", fields,
                ) + " } }"
                for i, (owner, typename, name, conn, fields) in enumerate(chunk)
            ]
            data = _graphql("query {\n" + "\n".join(parts) + "\n}", {})
            for i, (_, _, name, conn, _) in enumerate(chunk):
                page = (data.get(f"c{i}") or {}).get(name) or {"nodes": [], "pageInfo": {}}
                conn["nodes"].extend(page["nodes"])
                conn["pageInfo"] = page["pageInfo"]
                if name == "reviewThreads":
                    new_threads.extend(page["nodes"])
        pending = _overflowing(pending) + _overflowing([
            (t["id"], "PullRequestReviewThread", "comments", t.get("comments"), pages.thread_comments)
            for t in new_threads
        ])


# The full PR field selection, shared by the review-request search and the queue nodes fetch.
PR_NODE_FRAGMENT = """
fragment PRFields on PullRequest {
  id
  url
  number
  title
  state
  isDraft
  createdAt
  updatedAt
  mergeable
  additions
  deletions
  changedFiles
  body
  baseRefName
  headRefName
  headRefOid
  author { login }
  repository { nameWithOwner }
  reviewRequests(first: 30) {
    nodes {
      requestedReviewer {
        __typename
        ... on User { login }
        ... on Team { slug }
      }
    }
  }
  latestReviews(first: 30) {
    nodes { author { login } state commit { oid } }
  }
  """ + _connections(_PAGES, 30, 30) + """
  commits(last: 1) {
    nodes {
      commit {
        statusCheckRollup {
          state
          contexts(first: 50) {
            nodes {
              __typename
              ... on StatusContext { context state targetUrl }
              ... on CheckRun { name conclusion detailsUrl }
            }
          }
        }
      }
    }
  }
  timelineItems(last: 30, itemTypes: [REVIEW_REQUESTED_EVENT, PULL_REQUEST_REVIEW]) {
    nodes {
      __typename
      ... on ReviewRequestedEvent {
        createdAt
        requestedReviewer {
          __typename
          ... on User { login }
          ... on Team { slug }
        }
      }
      ... on PullRequestReview {
        author { login }
        submittedAt
      }
    }
  }
  """ + _connection("files", "first: 100", _PAGES.files) + """
}
"""

SEARCH_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 20, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ...PRFields
    }
  }
  rateLimit { remaining cost resetAt }
}
""" + PR_NODE_FRAGMENT

REVIEWED_BY_QUERY = """
query($q: String!, $login: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        url
        number
        title
        state
        createdAt
        updatedAt
        mergeable
        additions
        deletions
        changedFiles
        baseRefName
        headRefName
        headRefOid
        author { login }
        repository { nameWithOwner }
        reviews(last: 50, author: $login) {
          nodes { author { login } submittedAt state }
        }
      }
    }
  }
  rateLimit { remaining cost resetAt }
}
"""

class GithubError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# GitHub-side hiccups that abort an otherwise fine call, matched against the gh stderr.
RETRYABLE_ERRORS = (
    "http 502",
    "http 503",
    "http 504",
    "stream error",
    "connection reset",
    "unexpected eof",
    "i/o timeout",
    "tls handshake timeout",
    "error connecting to",
    "unexpected end of json input",
)
MAX_ATTEMPTS = 4


@dataclass
class RateLimit:
    remaining: int
    cost: int
    reset_at: str


def _gh(args: list[str], *, input: str | None = None, timeout: int = 60,
        allow_failure: bool = False) -> str:
    """Run `gh` and return stdout, retrying transient GitHub failures with backoff.

    `allow_failure` returns stdout on a nonzero exit as long as there *is*
    stdout: `gh api graphql` exits 1 whenever the response carries any `errors`,
    even a partial one where most aliases resolved fine. Callers that can use a
    partial response need the body, not the exception.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _gh_once(args, input=input, timeout=timeout, allow_failure=allow_failure)
        except GithubError as e:
            if attempt == MAX_ATTEMPTS or not e.retryable:
                raise
            delay = 2 ** (attempt - 1)
            log.warning("%s; retrying in %ss (attempt %s/%s)", e, delay, attempt + 1, MAX_ATTEMPTS)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _gh_once(args: list[str], *, input: str | None = None, timeout: int = 60,
             allow_failure: bool = False) -> str:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=timeout,
            check=not allow_failure, input=input,
        )
        # A gateway error comes back as an HTML body, which is no partial response to keep.
        if allow_failure and result.returncode != 0 and (
                not result.stdout.strip()
                or any(p in result.stderr.lower() for p in RETRYABLE_ERRORS)):
            raise subprocess.CalledProcessError(
                result.returncode, ["gh", *args], result.stdout, result.stderr,
            )
    except FileNotFoundError as e:
        raise GithubError("`gh` CLI not found on PATH. Install it from https://cli.github.com/") from e
    except subprocess.CalledProcessError as e:
        msg = e.stderr or e.stdout or "(no output)"
        if "authentication required" in msg.lower() or "not logged" in msg.lower():
            raise GithubError("`gh` is not authenticated. Run `gh auth login`.") from e
        low = msg.lower()
        raise GithubError(
            f"`gh {' '.join(args)}` failed: {msg.strip()}",
            retryable=any(p in low for p in RETRYABLE_ERRORS),
        ) from e
    except subprocess.TimeoutExpired as e:
        raise GithubError(
            f"`gh {' '.join(args)}` timed out after {timeout}s", retryable=True
        ) from e
    return result.stdout


def _loads(out: str) -> dict:
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise GithubError(f"`gh api graphql` returned non-JSON: {out[:200]!r}") from e


def _graphql(query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables})
    out = _gh(
        ["api", "graphql", "--input", "-"],
        input=payload,
    )
    data = _loads(out)
    if "errors" in data:
        raise GithubError(f"GraphQL errors: {data['errors']}")
    return data["data"]


def _graphql_partial(query: str, variables: dict) -> dict:
    """Like _graphql, but keeps whatever resolved when some aliases errored.

    Only for batched by-alias fetches over a user-curated ref list, where one
    stale entry (repo renamed, PR number that isn't a PR) must not sink the
    other 24 in the chunk. GraphQL still returns `data` with the failed aliases
    nulled, so the caller's "absent means skip" handling covers it.
    """
    payload = json.dumps({"query": query, "variables": variables})
    data = _loads(_gh(["api", "graphql", "--input", "-"], input=payload, allow_failure=True))
    if data.get("errors"):
        log.debug("partial GraphQL errors: %s", data["errors"])
    return data.get("data") or {}


def _search_pages(query: str, variables: dict):
    """Yield the response of each page of `query`'s `search` connection, following its cursor."""
    cursor = None
    while True:
        data = _graphql(query, {**variables, "cursor": cursor})
        yield data
        page = data["search"]["pageInfo"]
        if not page["hasNextPage"]:
            return
        cursor = page["endCursor"]


def _search_nodes(query: str, variables: dict, limit: int | None = None,
                  ) -> tuple[list[dict], RateLimit | None]:
    """Collect the PR nodes of every page of a `search`, up to `limit`, with the last rate limit."""
    nodes: list[dict] = []
    rl: RateLimit | None = None
    for data in _search_pages(query, variables):
        nodes.extend(n for n in data["search"]["nodes"] if n)
        if rate := data.get("rateLimit"):
            rl = RateLimit(rate["remaining"], rate["cost"], rate["resetAt"])
        if limit is not None and len(nodes) >= limit:
            break
    return nodes[:limit], rl


def _pull_requests(refs: list[tuple[str, int]], fields: str, *, chunk_size: int = 25,
                   partial: bool = False, fragment: str = "") -> dict[str, dict]:
    """Map each `(repo, number)` that resolves to its `pullRequest { fields }`, batched by alias.

    :param partial: keep what resolved when some aliases errored, for user-curated ref lists
    """
    out: dict[str, dict] = {}
    for start in range(0, len(refs), chunk_size):
        chunk = refs[start:start + chunk_size]
        parts = []
        for i, (repo, number) in enumerate(chunk):
            owner, name = repo.split("/", 1)
            parts.append(f'p{i}: repository(owner: "{owner}", name: "{name}") {{ '
                         f'pullRequest(number: {number}) {{ {fields} }} }}')
        query = "query {\n" + "\n".join(parts) + "\n}\n" + fragment
        data = (_graphql_partial if partial else _graphql)(query, {})
        for i, (repo, number) in enumerate(chunk):
            pr = (data.get(f"p{i}") or {}).get("pullRequest")
            if pr:
                out[f"{repo}#{number}"] = pr
    return out


_ARCHIVED_ACTIVITY_FIELDS = """
    state
    headRefOid
    updatedAt
    author { login }
    comments(last: 10) {
      nodes { author { login } createdAt body }
    }
    reviewThreads(last: 20) {
      nodes {
        comments(last: 5) { nodes { author { login } createdAt body } }
      }
    }
    reviews(last: 20) {
      nodes { author { login } state submittedAt commit { oid } }
    }
    commits(last: 1) {
      nodes { commit { oid committedDate } }
    }
    timelineItems(last: 20, itemTypes: [REVIEW_REQUESTED_EVENT]) {
      nodes {
        ... on ReviewRequestedEvent {
          createdAt
          requestedReviewer { ... on User { login } }
        }
      }
    }
"""


_BRANCH_SEARCH_FIELDS = """
      ... on PullRequest {
        number
        title
        state
        isDraft
        url
        headRefName
        headRefOid
        author { login }
      }
"""


# Tracked PRs are read-only watch targets, not review work: no diff, no files,
# no review threads, no reviewer states. Just enough to answer "did it move, and
# what was said" - which keeps the batched fetch cheap even for a long list.
TRACKED_NODE_FRAGMENT = """
fragment TrackedFields on PullRequest {
  id
  url
  number
  title
  state
  isDraft
  body
  createdAt
  updatedAt
  closedAt
  mergedAt
  headRefOid
  baseRefName
  author { login }
  repository { nameWithOwner }
  """ + _connections(_PAGES, 15, 5) + """
  commits(last: 1) {
    nodes {
      commit {
        statusCheckRollup {
          state
          contexts(first: 50) {
            nodes {
              __typename
              ... on StatusContext { context state targetUrl }
              ... on CheckRun { name conclusion detailsUrl }
            }
          }
        }
      }
    }
  }
}
"""


_REVIEWER = "requestedReviewer { __typename ... on User { login } ... on Team { slug } }"

# Authored PRs need what an author acts on, on top of the tracked fields.
MINE_NODE_FRAGMENT = f"""
fragment MineFields on PullRequest {{
  ...TrackedFields
  headRefName
  reviewDecision
  mergeable
  commits(last: 1) {{ nodes {{ commit {{ committedDate }} }} }}
  reviewRequests(first: 30) {{ nodes {{ {_REVIEWER} }} }}
  timelineItems(last: 30, itemTypes: [REVIEW_REQUESTED_EVENT, REVIEW_REQUEST_REMOVED_EVENT]) {{
    nodes {{
      __typename
      ... on ReviewRequestedEvent {{ createdAt {_REVIEWER} }}
      ... on ReviewRequestRemovedEvent {{ createdAt {_REVIEWER} }}
    }}
  }}
  crossReferences: timelineItems(last: 50, itemTypes: [CROSS_REFERENCED_EVENT]) {{
    nodes {{
      ... on CrossReferencedEvent {{
        source {{ ... on PullRequest {{ number author {{ login }} repository {{ nameWithOwner }} }} }}
      }}
    }}
  }}
}}
""" + TRACKED_NODE_FRAGMENT

AUTHORED_SEARCH_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number repository { nameWithOwner } } }
  }
}
"""


def _search_authored(q: str) -> tuple[list[tuple[str, int]], int]:
    """Return (repo, number) of every PR search `q` matches, and the total GitHub counted."""
    refs: list[tuple[str, int]] = []
    for data in _search_pages(AUTHORED_SEARCH_QUERY, {"q": q}):
        search = data["search"]
        refs.extend((n["repository"]["nameWithOwner"], n["number"]) for n in search["nodes"] if n)
    return refs, search["issueCount"]


View = Literal["queue", "tracked", "mine", "history"]


class GitHub(Protocol):
    """Every question the dashboard asks GitHub, each named for what its answer is for."""

    def review_requested(self, login: str) -> tuple[list[dict], RateLimit | None]: ...

    def reviewed_by(self, login: str, *, limit: int = 1000,
                    since: str | None = None) -> tuple[list[dict], RateLimit | None]: ...

    def reviewed_among(self, refs: list[tuple[str, int]], login: str) -> set[str]: ...

    def archived_activity(self, refs: list[tuple[str, int]]) -> dict[str, dict]: ...

    def head_sha(self, repo: str, number: int) -> str | None: ...

    def open_prs_by_head_branch(self, repo: str, branches: list[str]) -> dict[str, dict]: ...

    def pr_states(self, repo: str, numbers: list[int]) -> dict[int, str]: ...

    def nodes(self, refs: list[tuple[str, int]], view: View) -> dict[str, dict]: ...

    def authored_open(self, login: str) -> list[tuple[str, int]]: ...

    def authored_closed(self, login: str) -> list[tuple[str, int]]: ...

    def manual_subscriptions(self) -> list[dict]: ...

    def patch(self, repo: str, number: int) -> str | None: ...


class GhGitHub:
    """`GitHub` over the `gh` CLI."""

    def review_requested(self, login):
        # user-review-requested: drops team requests server-side, 2.8s a refresh instead of 39s.
        nodes, rl = _search_nodes(
            SEARCH_QUERY, {"q": f"is:open is:pr user-review-requested:{login} archived:false"})
        _complete_pages(nodes, _PAGES)
        return nodes, rl

    def reviewed_by(self, login, *, limit=1000, since=None):
        # Search caps at 1000, so newest-updated first keeps the recent reviews past that.
        q = f"is:pr reviewed-by:{login}" + (f" updated:>={since}" if since else "")
        return _search_nodes(REVIEWED_BY_QUERY, {"q": f"{q} sort:updated-desc", "login": login},
                             limit)

    def reviewed_among(self, refs, login):
        prs = _pull_requests(refs, f'reviews(first: 1, author: "{login}") '
                                   "{ nodes { author { login } } }")
        return {pr_id for pr_id, pr in prs.items()
                if any((n.get("author") or {}).get("login") == login
                       for n in (pr.get("reviews") or {}).get("nodes") or [])}

    def archived_activity(self, refs):
        return _pull_requests(refs, _ARCHIVED_ACTIVITY_FIELDS)

    def head_sha(self, repo, number):
        owner, name = repo.split("/", 1)
        data = _graphql(
            'query($owner: String!, $name: String!, $number: Int!) { '
            'repository(owner: $owner, name: $name) { '
            'pullRequest(number: $number) { headRefOid } } }',
            {"owner": owner, "name": name, "number": number},
        )
        return ((data.get("repository") or {}).get("pullRequest") or {}).get("headRefOid")

    def open_prs_by_head_branch(self, repo, branches, chunk_size=25):
        # Strict, as a branch missing from a half-resolved response would read as no migration.
        out: dict[str, dict] = {}
        for start in range(0, len(branches), chunk_size):
            chunk = branches[start:start + chunk_size]
            parts = []
            for i, branch in enumerate(chunk):
                q = json.dumps(f"repo:{repo} is:pr is:open sort:created-desc head:{branch}")
                parts.append(
                    f'b{i}: search(query: {q}, type: ISSUE, first: 5) {{ '
                    f'nodes {{{_BRANCH_SEARCH_FIELDS}}} }}',
                )
            data = _graphql("query {\n" + "\n".join(parts) + "\n}", {})
            for i, branch in enumerate(chunk):
                # `head:` is a filter, so a hit on a branch merely resembling this one is dropped.
                node = next((n for n in (data.get(f"b{i}") or {}).get("nodes") or []
                             if n and n.get("headRefName") == branch), None)
                if node:
                    out[branch] = {
                        "number": node["number"],
                        "title": node.get("title") or "",
                        "state": node.get("state") or "",
                        "draft": bool(node.get("isDraft")),
                        "url": node.get("url") or "",
                        "head_branch": branch,
                        "head_sha": node.get("headRefOid") or "",
                        "author": (node.get("author") or {}).get("login") or "",
                    }
        return out

    def pr_states(self, repo, numbers):
        prs = _pull_requests([(repo, n) for n in numbers], "state", partial=True)
        return {int(pr_id.rpartition("#")[2]): pr["state"]
                for pr_id, pr in prs.items() if pr.get("state")}

    def nodes(self, refs, view):
        if view == "queue":
            nodes = _pull_requests(refs, "...PRFields", chunk_size=10, fragment=PR_NODE_FRAGMENT)
            _complete_pages(list(nodes.values()), _PAGES)
            return nodes
        fragment, spread = ((TRACKED_NODE_FRAGMENT, "...TrackedFields") if view == "tracked"
                            else (MINE_NODE_FRAGMENT, "...MineFields"))
        # A closed PR carries its whole discussion, and 50 of them overran GitHub's 10 s limit.
        nodes = _pull_requests(refs, spread, chunk_size=10 if view == "history" else 50,
                               partial=True, fragment=fragment)
        # Both fragments spread TrackedFields.
        _complete_pages(list(nodes.values()), _PAGES)
        return nodes

    def authored_open(self, login):
        return _search_authored(f"is:open is:pr author:{login} archived:false")[0]

    def authored_closed(self, login):
        refs, total = _search_authored(f"is:closed is:pr author:{login}")
        if len(refs) < total:
            # Search serves at most 1000 results, a longer history needs the query split by date.
            raise GithubError(f"the search returned {len(refs)} of {total} closed PRs")
        return refs

    def manual_subscriptions(self):
        # Only the notification reason tells a manual Subscribe apart, and `all` adds read threads.
        raw = _gh([
            "api", "/notifications?all=true&per_page=100", "--paginate",
            "--jq", (".[] | select(.reason == \"manual\") "
                     "| select(.subject.type == \"PullRequest\") "
                     "| {url: .subject.url, title: .subject.title, "
                     "updated_at: .updated_at, repo: .repository.full_name}"),
        ], timeout=120)
        out: dict[str, dict] = {}
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            # subject.url is the REST pulls URL: .../repos/{owner}/{repo}/pulls/{n}
            _, sep, tail = (entry.get("url") or "").partition("/repos/")
            repo, _, number_s = tail.partition("/pulls/")
            if not sep or not number_s.isdecimal():
                continue
            number = int(number_s)
            out.setdefault(f"{repo}#{number}", {
                "id": f"{repo}#{number}",
                "repo": repo,
                "number": number,
                "url": f"https://github.com/{repo}/pull/{number}",
                "title": entry.get("title") or "",
                "updated_at": entry.get("updated_at"),
            })
        return list(out.values())

    def patch(self, repo, number):
        # `.diff` gives one section per file and net change, the caller truncates.
        try:
            return _gh(["api", f"repos/{repo}/pulls/{number}",
                        "-H", "Accept: application/vnd.github.diff"], timeout=60)
        except GithubError:
            return None
