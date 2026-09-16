from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger("pr_dash.github")

# The full PR field selection, as a named GraphQL fragment so the review-request
# search and the by-number sibling fetch (fetch_pr_nodes) share one definition
# and can never drift out of sync.
PR_NODE_FRAGMENT = """
fragment PRFields on PullRequest {
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
  reviews(first: 30) {
    nodes { id author { login } state submittedAt body url }
  }
  comments(first: 30) {
    nodes { author { login } createdAt body databaseId url }
  }
  reviewThreads(first: 30) {
    nodes {
      id
      isResolved
      comments(first: 30) {
        nodes { author { login } createdAt body path databaseId url }
      }
    }
  }
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
  files(first: 100) {
    pageInfo { hasNextPage endCursor }
    nodes { path }
  }
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
query($q: String!, $cursor: String) {
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
        reviews(first: 50) {
          nodes { author { login } submittedAt state }
        }
      }
    }
  }
  rateLimit { remaining cost resetAt }
}
"""

FILES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      files(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { path }
      }
    }
  }
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
)
MAX_ATTEMPTS = 3


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
        if allow_failure and result.returncode != 0 and not result.stdout.strip():
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


def _graphql(query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables})
    out = _gh(
        ["api", "graphql", "--input", "-"],
        input=payload,
    )
    data = json.loads(out)
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
    data = json.loads(
        _gh(["api", "graphql", "--input", "-"], input=payload, allow_failure=True)
    )
    if data.get("errors"):
        log.debug("partial GraphQL errors: %s", data["errors"])
    return data.get("data") or {}


def search_personal_review_requested(login: str) -> tuple[list[dict], RateLimit | None]:
    """Return PRs where `login` is personally requested as a reviewer.

    `user-review-requested:` is the server-side form of is_personally_requested:
    a direct User request, team requests excluded, and a PR requested from both
    the user and their teams still matches. `review-requested:` also returned the
    team ones, so the whole field fragment was fetched for 148 PRs to keep 13 -
    39s and 1.5MB a refresh, against 2.8s and 122KB. Callers still filter.
    """
    q = f"is:open is:pr user-review-requested:{login} archived:false"
    nodes: list[dict] = []
    cursor = None
    rl: RateLimit | None = None
    while True:
        data = _graphql(SEARCH_QUERY, {"q": q, "cursor": cursor})
        search = data["search"]
        nodes.extend(n for n in search["nodes"] if n)
        rate = data.get("rateLimit")
        if rate:
            rl = RateLimit(rate["remaining"], rate["cost"], rate["resetAt"])
        if not search["pageInfo"]["hasNextPage"]:
            break
        cursor = search["pageInfo"]["endCursor"]
    return nodes, rl


def search_reviewed_by(
    login: str, *, limit: int = 1000, since: str | None = None,
) -> tuple[list[dict], RateLimit | None]:
    """Return PRs where `login` has submitted at least one review.

    Used for backfilling historical review data into the cache for KPI counts.
    Caps at `limit` results. GitHub search itself caps at 1000, so for a
    long-tenured reviewer the results are sorted updated-newest-first to keep
    the most recent reviews when that ceiling is hit. `since` (a YYYY-MM-DD
    date) bounds the window via the PR's `updated:` field - the review's own
    date isn't directly queryable, but PR update time is a close proxy.
    """
    q = f"is:pr reviewed-by:{login}"
    if since:
        q += f" updated:>={since}"
    q += " sort:updated-desc"
    nodes: list[dict] = []
    cursor = None
    rl: RateLimit | None = None
    while len(nodes) < limit:
        data = _graphql(REVIEWED_BY_QUERY, {"q": q, "cursor": cursor})
        search = data["search"]
        for n in search["nodes"]:
            if not n:
                continue
            nodes.append(n)
            if len(nodes) >= limit:
                break
        rate = data.get("rateLimit")
        if rate:
            rl = RateLimit(rate["remaining"], rate["cost"], rate["resetAt"])
        if not search["pageInfo"]["hasNextPage"]:
            break
        cursor = search["pageInfo"]["endCursor"]
    return nodes, rl


def fetch_reviewed_prs(
    refs: list[tuple[str, int, str]], login: str, *, chunk_size: int = 25,
) -> set[str]:
    """Given (repo, number, pr_id) triples, return the pr_ids that `login` has
    submitted at least one review on.

    Used at sweep time to verify delete-candidates: a PR that left the active
    request set because it was just approved/changes-requested has a stale
    cached `previously_reviewed=0` (it was last fetched before the review), so
    we re-check it here before deciding to delete vs. archive. Batched via
    GraphQL field aliases to keep this to a few requests.
    """
    reviewed: set[str] = set()
    for start in range(0, len(refs), chunk_size):
        chunk = refs[start:start + chunk_size]
        parts = []
        for i, (repo, number, _) in enumerate(chunk):
            owner, name = repo.split("/", 1)
            parts.append(
                f'p{i}: repository(owner: "{owner}", name: "{name}") {{ '
                f'pullRequest(number: {number}) {{ '
                f'reviews(first: 50) {{ nodes {{ author {{ login }} }} }} }} }}'
            )
        query = "query {\n" + "\n".join(parts) + "\n}"
        data = _graphql(query, {})
        for i, (_, _, pr_id) in enumerate(chunk):
            pr = (data.get(f"p{i}") or {}).get("pullRequest") or {}
            nodes = (pr.get("reviews") or {}).get("nodes") or []
            if any((n.get("author") or {}).get("login") == login for n in nodes):
                reviewed.add(pr_id)
    return reviewed


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


def fetch_archived_activity(
    refs: list[tuple[str, int, str]], *, chunk_size: int = 25,
) -> dict[str, dict]:
    """Given (repo, number, pr_id) triples, return pr_id -> a node with the
    current state plus the recent comment/review/request activity needed to
    detect an informal re-review ping (see derive.detect_review_ping) and a push
    landed after my review (see derive.detect_push_since_review).

    One batched request over the whole set (GraphQL field aliases). Used to
    re-check archived-but-still-OPEN PRs: they left the `review-requested:`
    search, so their state goes stale the moment robodoo closes them, and any
    "please re-review" ping from the author is invisible to all tooling.

    `updatedAt` / `headRefOid` are the staleness signals the caller uses to
    decide which of these rows need a full re-fetch: a push moves the head, and
    any review or comment (mine included) moves updatedAt.
    """
    out: dict[str, dict] = {}
    for start in range(0, len(refs), chunk_size):
        chunk = refs[start:start + chunk_size]
        parts = []
        for i, (repo, number, _) in enumerate(chunk):
            owner, name = repo.split("/", 1)
            parts.append(
                f'p{i}: repository(owner: "{owner}", name: "{name}") {{ '
                f'pullRequest(number: {number}) {{{_ARCHIVED_ACTIVITY_FIELDS}}} }}'
            )
        query = "query {\n" + "\n".join(parts) + "\n}"
        data = _graphql(query, {})
        for i, (_, _, pr_id) in enumerate(chunk):
            pr = (data.get(f"p{i}") or {}).get("pullRequest")
            if pr:
                out[pr_id] = pr
    return out


def fetch_head_sha(repo: str, number: int) -> str | None:
    """Live head sha of one PR. Hides store the head_sha they were made at so a
    later push auto-unhides; an archived row's cached sha can be long stale, so
    a hide taken from the cache would expire against the live sha immediately."""
    owner, name = repo.split("/", 1)
    data = _graphql(
        'query($owner: String!, $name: String!, $number: Int!) { '
        'repository(owner: $owner, name: $name) { '
        'pullRequest(number: $number) { headRefOid } } }',
        {"owner": owner, "name": name, "number": number},
    )
    pr = (data.get("repository") or {}).get("pullRequest") or {}
    return pr.get("headRefOid")


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


def search_open_prs_by_head_branch(
    repo: str, branches: list[str], *, chunk_size: int = 25,
) -> dict[str, dict]:
    """Given head branch names, return branch -> that branch's open PR in `repo`.

    Answers "does this bundle have a companion migration" for a handful of
    branches. Listing every open PR in the repo instead answered it for all of
    them at once, but its cost tracks the repo rather than the question:
    odoo/upgrade runs ~750 open PRs, 8 REST pages, 5.2s a refresh. Aliasing one
    `search` per branch into a single GraphQL request asks only what is being
    asked - 13 branches in 1.6s, for a rate-limit cost of 1.

    `head:` is a search filter, not an exact match, so a hit whose headRefName
    merely resembles the branch has to be dropped here. `sort:created-desc`
    keeps the newest of several exact hits, which is what the REST listing did
    when two open PRs shared a head branch - a bundle robodoo could not resolve
    either.

    Strict rather than partial (unlike the by-ref fetchers): a missing branch
    here means "this bundle has no migration", which is an answer the AI pass
    acts on, so a half-resolved response has to raise rather than read as a
    batch of negatives.
    """
    out: dict[str, dict] = {}
    for start in range(0, len(branches), chunk_size):
        chunk = branches[start:start + chunk_size]
        parts = []
        for i, branch in enumerate(chunk):
            q = json.dumps(f"repo:{repo} is:pr is:open sort:created-desc head:{branch}")
            parts.append(
                f'b{i}: search(query: {q}, type: ISSUE, first: 5) {{ '
                f'nodes {{{_BRANCH_SEARCH_FIELDS}}} }}'
            )
        query = "query {\n" + "\n".join(parts) + "\n}"
        data = _graphql(query, {})
        for i, branch in enumerate(chunk):
            nodes = (data.get(f"b{i}") or {}).get("nodes") or []
            for node in nodes:
                if not node or node.get("headRefName") != branch:
                    continue
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
                break
    return out


def fetch_pr_states(repo: str, numbers: list[int], *,
                    chunk_size: int = 25) -> dict[int, str]:
    """Given PR numbers in one repo, return number -> OPEN / CLOSED / MERGED.

    For companions the branch search above no longer returns. An upgrade PR has
    its own review flow and is regularly merged ahead of the addons halves it
    migrates, so treating "no longer open" as "no migration" would resurrect the
    exact false positive the companion exists to kill.

    Batched via GraphQL field aliases and tolerant of a partial response: a
    number that no longer resolves is simply absent from the result.
    """
    owner, name = repo.split("/", 1)
    out: dict[int, str] = {}
    for start in range(0, len(numbers), chunk_size):
        chunk = numbers[start:start + chunk_size]
        parts = [
            f'p{i}: repository(owner: "{owner}", name: "{name}") {{ '
            f'pullRequest(number: {n}) {{ state }} }}'
            for i, n in enumerate(chunk)
        ]
        data = _graphql_partial("query {\n" + "\n".join(parts) + "\n}", {})
        for i, n in enumerate(chunk):
            state = ((data.get(f"p{i}") or {}).get("pullRequest") or {}).get("state")
            if state:
                out[n] = state
    return out


def fetch_pr_nodes(
    refs: list[tuple[str, int, str]], *, chunk_size: int = 10,
) -> list[dict]:
    """Fetch full PR nodes by (repo, number, pr_id), shaped exactly like the
    review-request search nodes (same PRFields fragment), so they can flow
    through the normal _node_to_rows path.

    Used to prime open pair-siblings the user already reviewed: those left the
    review-requested search, so their threads/reviews/comments would otherwise
    never be cached. Batched via GraphQL field aliases (typically 0-3 at a time).
    """
    nodes: list[dict] = []
    for start in range(0, len(refs), chunk_size):
        chunk = refs[start:start + chunk_size]
        parts = []
        for i, (repo, number, _) in enumerate(chunk):
            owner, name = repo.split("/", 1)
            parts.append(
                f'p{i}: repository(owner: "{owner}", name: "{name}") {{ '
                f'pullRequest(number: {number}) {{ ...PRFields }} }}'
            )
        query = "query {\n" + "\n".join(parts) + "\n}\n" + PR_NODE_FRAGMENT
        data = _graphql(query, {})
        for i in range(len(chunk)):
            pr = (data.get(f"p{i}") or {}).get("pullRequest")
            if pr:
                nodes.append(pr)
    return nodes


# Tracked PRs are read-only watch targets, not review work: no diff, no files,
# no review threads, no reviewer states. Just enough to answer "did it move, and
# what was said" - which keeps the batched fetch cheap even for a long list.
TRACKED_NODE_FRAGMENT = """
fragment TrackedFields on PullRequest {
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
  comments(last: 10) {
    totalCount
    nodes { author { login } createdAt body url }
  }
  reviews(last: 10) {
    totalCount
    nodes { id author { login } state submittedAt body url }
  }
  reviewThreads(last: 15) {
    totalCount
    nodes {
      id
      isResolved
      path
      comments(last: 5) {
        nodes { author { login } createdAt body url pullRequestReview { id } }
      }
    }
  }
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


def list_manual_subscriptions() -> list[dict]:
    """Return the PR threads the user subscribed to *themselves*.

    GitHub exposes no endpoint for the /notifications/subscriptions page, so the
    notification list is the only handle on it - and its `reason` field is what
    separates a deliberate Subscribe ("manual") from the auto-subscription that
    a review request or a mention creates. `all=true` includes already-read
    threads, without which only unread ones would ever seed.

    This is inherently activity-bounded: GitHub prunes old notifications, so a
    subscribed-but-quiet PR eventually stops appearing here. That is exactly why
    the caller stores the result in a sticky table instead of mirroring it.

    Each entry is {repo, number, url, title, updated_at}.
    """
    out: list[dict] = []
    seen: set[str] = set()
    raw = _gh([
        "api", "/notifications?all=true&per_page=100", "--paginate",
        "--jq", ".[] | select(.reason == \"manual\") "
                "| select(.subject.type == \"PullRequest\") "
                "| {url: .subject.url, title: .subject.title, "
                "updated_at: .updated_at, repo: .repository.full_name}",
    ], timeout=120)
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        # subject.url is the REST pulls URL: .../repos/{owner}/{repo}/pulls/{n}
        api_url = entry.get("url") or ""
        _, sep, tail = api_url.partition("/repos/")
        if not sep or "/pulls/" not in tail:
            continue
        repo, _, number_s = tail.partition("/pulls/")
        try:
            number = int(number_s)
        except ValueError:
            continue
        pr_id = f"{repo}#{number}"
        if pr_id in seen:
            continue
        seen.add(pr_id)
        out.append({
            "id": pr_id,
            "repo": repo,
            "number": number,
            "url": f"https://github.com/{repo}/pull/{number}",
            "title": entry.get("title") or "",
            "updated_at": entry.get("updated_at"),
        })
    return out


def fetch_tracked_nodes(
    refs: list[tuple[str, int]], *, chunk_size: int = 50,
) -> dict[str, dict]:
    """Given (repo, number) pairs, return pr_id -> a slim PR node.

    Batched via GraphQL field aliases. A ref that no longer resolves (deleted
    repo, or a number that was never a PR) is simply absent from the result
    rather than raising, so one bad entry can't sink the whole refresh.

    The chunk is wide because each one is a round trip and the node is slim: 43
    tracked PRs took 10.5s in fives and 6.4s in one go.
    """
    out: dict[str, dict] = {}
    for start in range(0, len(refs), chunk_size):
        chunk = refs[start:start + chunk_size]
        parts = []
        for i, (repo, number) in enumerate(chunk):
            owner, name = repo.split("/", 1)
            parts.append(
                f'p{i}: repository(owner: "{owner}", name: "{name}") {{ '
                f'pullRequest(number: {number}) {{ ...TrackedFields }} }}'
            )
        query = "query {\n" + "\n".join(parts) + "\n}\n" + TRACKED_NODE_FRAGMENT
        data = _graphql_partial(query, {})
        for i, (repo, number) in enumerate(chunk):
            pr = (data.get(f"p{i}") or {}).get("pullRequest")
            if pr:
                out[f"{repo}#{number}"] = pr
    return out


def fetch_remaining_files(repo: str, number: int, after_cursor: str) -> list[str]:
    """Paginate files() beyond the first 100 included in the search query."""
    owner, name = repo.split("/", 1)
    paths: list[str] = []
    cursor = after_cursor
    while cursor:
        data = _graphql(FILES_QUERY, {"owner": owner, "name": name, "number": number, "cursor": cursor})
        files = data["repository"]["pullRequest"]["files"]
        paths.extend(f["path"] for f in files["nodes"])
        if not files["pageInfo"]["hasNextPage"]:
            break
        cursor = files["pageInfo"]["endCursor"]
    return paths


def fetch_patch(repo: str, number: int) -> str | None:
    """Fetch the combined unified diff via gh REST. Returns None on failure.

    Uses `.diff` (one `diff --git` per file, net change) rather than `.patch`
    (mbox/format-patch with per-commit duplication and commit-message preamble)
    so the frontend can split it reliably into per-file sections.

    Returns the full diff; the caller is responsible for size-based truncation
    so it can flag a truncated diff rather than store a corrupt half-file one.
    """
    try:
        return _gh(
            [
                "api",
                f"repos/{repo}/pulls/{number}",
                "-H", "Accept: application/vnd.github.diff",
            ],
            timeout=60,
        )
    except GithubError:
        return None
