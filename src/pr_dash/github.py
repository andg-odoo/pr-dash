from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

SEARCH_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 20, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        url
        number
        title
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
        reviewThreads(first: 30) {
          nodes {
            id
            isResolved
            comments(first: 30) {
              nodes { author { login } createdAt }
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
    }
  }
  rateLimit { remaining cost resetAt }
}
"""

REVIEWED_BY_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        url
        number
        title
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
    pass


@dataclass
class RateLimit:
    remaining: int
    cost: int
    reset_at: str


def _gh(args: list[str], *, input: str | None = None, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=timeout, check=True, input=input,
        )
    except FileNotFoundError as e:
        raise GithubError("`gh` CLI not found on PATH. Install it from https://cli.github.com/") from e
    except subprocess.CalledProcessError as e:
        msg = e.stderr or e.stdout or "(no output)"
        if "authentication required" in msg.lower() or "not logged" in msg.lower():
            raise GithubError("`gh` is not authenticated. Run `gh auth login`.") from e
        raise GithubError(f"`gh {' '.join(args)}` failed: {msg.strip()}") from e
    except subprocess.TimeoutExpired as e:
        raise GithubError(f"`gh {' '.join(args)}` timed out after {timeout}s") from e
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


def search_personal_review_requested(login: str) -> tuple[list[dict], RateLimit | None]:
    """Return PRs where `login` is requested as a reviewer (personally or via team).

    Caller is responsible for filtering team-only requests.
    """
    q = f"is:open is:pr review-requested:{login} archived:false"
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
