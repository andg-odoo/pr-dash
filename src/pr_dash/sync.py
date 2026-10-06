"""Bring the cache up to date from GitHub, the Mergebot and the AI reviewer."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pr_dash import ai, branch_set, db, derive, github, hidden, mergebot

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("pr_dash")

# Waits after the first failed AI pass at a review context, then after the second.
_AI_RETRY_BACKOFF_HOURS = (1, 4)


@dataclass
class RefreshReport:
    """How many rows each refresh phase changed."""
    primed: int = 0
    archived: int = 0
    deleted: int = 0
    closed: int = 0
    pinged: int = 0
    pushed: int = 0
    refreshed: int = 0
    companions: int = 0


def _node_id(node: dict) -> str:
    return f"{node['repository']['nameWithOwner']}#{node['number']}"


def _refs(rows) -> list[tuple[str, int]]:
    return [(r["repo"], r["number"]) for r in rows]


class Sync:
    """Every operation that brings the cache up to date, each phase announced to `on_phase`."""

    def __init__(self, conn, cfg, gh: github.GitHub, *,
                 read_mergebot: Callable[[str, int], mergebot.MergebotState] = mergebot.fetch,
                 reviewer: Callable[..., list[ai.ReviewOutcome]] = ai.review_batch,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 on_phase: Callable[[str], None] | None = None):
        self.conn, self.cfg, self.gh = conn, cfg, gh
        self.read_mergebot, self.reviewer, self.clock = read_mergebot, reviewer, clock
        self._phase = on_phase or (lambda text: None)

    def _stamp(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    def refresh_queue(self, *, force: bool, cron: bool) -> RefreshReport:
        """Fetch the Review queue, settle the rows that left it, then run the AI pass."""
        conn, cfg, login, report = self.conn, self.cfg, self.cfg.github_login, RefreshReport()
        self._phase("Searching for review requests...")
        nodes, rate = self.gh.review_requested(login)
        if rate:
            log.debug("GraphQL rate limit: %d remaining (cost %d)", rate.remaining, rate.cost)
        personally = [n for n in nodes if derive.is_personally_requested(
            (n.get("reviewRequests") or {}).get("nodes") or [], login,
        )]
        self._phase(f"Processing {len(personally)} PRs...")

        kept_ids: set[str] = set()
        staleness_cutoff = self.clock() - timedelta(minutes=cfg.thresholds.staleness_minutes)
        for i, node in enumerate(personally, 1):
            pr_id = _node_id(node)
            kept_ids.add(pr_id)
            self._phase(f"[{i}/{len(personally)}] {pr_id}")
            cached = db.get_cached_pr(conn, pr_id)
            head_sha = node["headRefOid"]
            if not (
                force or cached is None
                or cached["head_sha"] != head_sha
                or cached["updated_at"] != node["updatedAt"]
                or cached["archived_at"] is not None
                or derive.parse_iso(cached["fetched_at"]) < staleness_cutoff
                # A diff the cache lost, say to a schema migration, is refetched on a fresh PR.
                or db.get_diff(conn, head_sha) is None
            ):
                continue
            self._store_node(node, force=force)

            # Snapshot the reviewed diff's files only when that review is on the cached head.
            my_review = next(
                (r for r in (node.get("latestReviews") or {}).get("nodes") or []
                 if (r.get("author") or {}).get("login") == login),
                None,
            )
            reviewed_sha = (my_review.get("commit") or {}).get("oid") if my_review else None
            if reviewed_sha and reviewed_sha == head_sha:
                snap = db.get_review_snapshot(conn, pr_id)
                diff_row = db.get_diff(conn, head_sha)
                if (not snap or snap["reviewed_sha"] != reviewed_sha) and (
                        diff_row and diff_row["patch_text"]):
                    db.upsert_review_snapshot(
                        conn, pr_id, reviewed_sha,
                        json.dumps(derive.file_change_signatures(diff_row["patch_text"])),
                        self._stamp(),
                    )

        # Reviewed open halves of an active set leave the search, so they are fetched and kept.
        self._phase("Priming reviewed Branch set halves...")
        halves = [
            row for s in branch_set.group(dict(r) for r in db.list_prs(conn))
            if any(m["id"] in kept_ids for m in s.members)
            for row in s.members if row["id"] not in kept_ids and row.get("state") == "OPEN"
        ]
        report.primed = self._refresh_pr_rows(halves, force=force, kept_ids=kept_ids)

        self._phase("Reconciling PRs that left the queue...")
        # A failed reconcile leaves unverified rows, so the sweep only archives this run.
        reconciled = self._reconcile_reviewed(kept_ids)
        report.archived, report.deleted = db.sweep(
            conn, kept_ids, self._stamp(), delete=reconciled,
        )

        self._phase("Re-checking archived rows...")
        self._reconcile_archived_states(kept_ids, report)

        # Before the AI pass, so it is told whether a migration exists.
        self._phase("Matching migration PRs...")
        companion_repo = self._refresh_companions(kept_ids, report)

        if cfg.ai.enabled and cfg.ai.review_enabled:
            requests, context = self._build_review_queue(
                kept_ids, companion_repo=companion_repo,
                limit=cfg.ai.cron_max_reviews if cron else None,
            )
            if requests:
                self._phase(f"AI sanity-check {len(requests)} small PRs...")
                outcomes = self.reviewer(
                    requests, timeout=cfg.ai.timeout_seconds,
                    model=cfg.ai.model, max_diff_chars=cfg.ai.review_max_diff_chars,
                )
                self._store_reviews(outcomes, context)
        log.debug("queue refresh: %s", report)
        return report

    def _store_node(self, node: dict, *, force: bool, cached=None) -> None:
        """Persist a queue node, its diff and complexity, keeping `cached`'s review state."""
        conn, cfg = self.conn, self.cfg
        pr_row, modules, reviewers, threads, comments = self._node_to_rows(node)
        if cached:
            # The timeline window can drop my review, and refreshing content never unarchives.
            pr_row["previously_reviewed"] = (
                cached["previously_reviewed"] or pr_row["previously_reviewed"]
            )
            pr_row["archived_at"] = cached["archived_at"]
        pr_id = pr_row["id"]
        with db.transaction(conn):
            db.upsert_pr(conn, pr_row)
            db.replace_modules(conn, pr_id, modules)
            db.replace_reviewers(conn, pr_id, reviewers)
            db.replace_threads(conn, pr_id, threads)
            db.replace_comments(conn, pr_id, comments)
        self._store_patch(pr_row, force=force)
        score = derive.heuristic_score(
            additions=pr_row["additions"],
            deletions=pr_row["deletions"],
            changed_files=pr_row["changed_files"],
            modules=modules,
            unresolved_threads=pr_row["unresolved_threads"],
            previously_reviewed=bool(pr_row["previously_reviewed"]),
        )
        db.upsert_complexity(conn, {
            "head_sha": pr_row["head_sha"],
            "bucket": derive.bucket_for(score, cfg.buckets.M, cfg.buckets.L, cfg.buckets.XL),
            "score": score,
            "method": "heuristic",
            "notes": None,
            "computed_at": self._stamp(),
        })

    def _store_reviews(self, outcomes: list, review_context: dict) -> None:
        """Persist each outcome: the review itself, or the failure that stands in for it."""
        now = self._stamp()
        for outcome in outcomes:
            context_heads, companion_sha = review_context.get(outcome.head_sha, ("", ""))
            if outcome.result is not None:
                rev = outcome.result
                with db.transaction(self.conn):
                    db.upsert_ai_review(
                        self.conn, outcome.head_sha, context_heads,
                        rev.summary, json.dumps(rev.concerns),
                        rev.verdict, now, companion_head_sha=companion_sha,
                    )
                    db.clear_ai_attempt(self.conn, outcome.head_sha, context_heads, companion_sha)
            elif outcome.reason != "cli-missing":
                # An absent CLI is the batch's problem, not this PR's or its budget's.
                db.record_ai_attempt(self.conn, outcome.head_sha, context_heads, companion_sha,
                                     outcome.reason, now)

    def _store_patch(self, pr_row: dict, *, force: bool) -> None:
        """Cache pr_row's head diff, compacted over the size limits, none over the file limit."""
        conn, limits = self.conn, self.cfg.thresholds
        head_sha = pr_row["head_sha"]
        if db.get_diff(conn, head_sha) is not None and not force:
            return
        if pr_row["changed_files"] > limits.diff_max_files:
            db.upsert_diff(conn, head_sha, None, True, self._stamp())
            return
        patch = self.gh.patch(pr_row["repo"], pr_row["number"])
        truncated = False
        if patch is not None and (
            patch.count("\n") > limits.diff_max_lines or len(patch) > limits.diff_max_bytes
        ):
            # Noise is stubbed rather than dropped, so the file list still shows every file.
            compacted = derive.compact_diff(
                patch, repo=pr_row["repo"], number=pr_row["number"], stub_noise=True,
            )
            patch, truncated = compacted.text, compacted.partial
            if len(patch) > limits.diff_max_bytes:
                patch, truncated = None, True
        db.upsert_diff(conn, head_sha, patch, truncated, self._stamp())

    def _refresh_pr_rows(self, targets: list[dict], *, force: bool,
                         kept_ids: set[str] | None = None) -> int:
        """Re-persist cached rows the search cannot return, keeping their archived state.

        :return: the number of rows re-persisted
        """
        if not targets:
            return 0
        by_id = {r["id"]: r for r in targets}
        try:
            nodes = self.gh.nodes(_refs(targets), "queue")
        except github.GithubError as e:
            log.warning("row refresh failed (%s); keeping cached data", e)
            return 0

        refreshed = 0
        for node in nodes.values():
            pr_id = _node_id(node)
            cached = by_id.get(pr_id)
            # Kept even when not re-persisted, so the sweep never archives a reviewed half.
            if kept_ids is not None:
                kept_ids.add(pr_id)
            # A row with no comment rows yet predates their caching, so it is primed once.
            if not (force or cached is None or not db.has_comments(self.conn, pr_id)
                    or cached["head_sha"] != node["headRefOid"]
                    or cached["updated_at"] != node["updatedAt"]):
                continue
            self._store_node(node, force=force, cached=cached)
            refreshed += 1
        return refreshed

    def _reconcile_reviewed(self, kept_ids: set[str]) -> bool:
        """Mark the fallen-out rows I reviewed, False when GitHub could not be asked."""
        candidates = db.delete_candidates(self.conn, kept_ids)
        if not candidates:
            return True
        try:
            reviewed = self.gh.reviewed_among(_refs(candidates), self.cfg.github_login)
        except github.GithubError as e:
            log.warning("review reconciliation failed (%s); deferring sweep deletes", e)
            return False
        if reviewed:
            db.mark_reviewed(self.conn, reviewed)
        return True

    def _reconcile_archived_states(self, kept_ids: set[str], report: RefreshReport) -> None:
        """Refresh archived open rows: state, re-review ping, push since my review and content."""
        conn, login = self.conn, self.cfg.github_login
        prs = [dict(r) for r in db.list_prs(conn)]
        stale = [
            p for p in prs
            if p["id"] not in kept_ids and p["archived_at"] and p["state"] == "OPEN"
        ]
        if not stale:
            return
        try:
            activity = self.gh.archived_activity(_refs(stale))
        except github.GithubError as e:
            log.warning("archived activity check failed (%s); keeping cached data", e)
            return

        # Hiding dismisses pings until a push, judged on the live sha since archived rows lag.
        def _live_sha(p: dict) -> str:
            return (activity.get(p["id"]) or {}).get("headRefOid") or p["head_sha"]

        live_sets = branch_set.group({**p, "head_sha": _live_sha(p)} for p in prs)
        set_of = {m["id"]: s for s in live_sets for m in s.members}
        hidden_ids = set(hidden.prune(hidden.load(self.cfg), [
            {"id": p["id"], "heads_key": set_of[p["id"]].heads_key} for p in stale
        ]))

        outdated: list[dict] = []
        with db.transaction(conn):
            for p in stale:
                node = activity.get(p["id"])
                if node is None:
                    continue
                new_state = node.get("state") or "OPEN"
                if new_state != p["state"]:
                    db.set_pr_state(conn, p["id"], new_state)
                    report.closed += new_state != "OPEN"
                ping = push = None
                if new_state == "OPEN" and p["id"] not in hidden_ids:
                    ping = derive.detect_review_ping(node, login)
                    push = derive.detect_push_since_review(node, login)
                if ping:
                    db.set_ping(conn, p["id"], ping["ping_at"], ping["ping_author"],
                                derive.comment_snippet(ping.get("ping_body")))
                    report.pinged += 1
                elif p["ping_at"]:
                    db.set_ping(conn, p["id"], None, None, None)
                if push:
                    db.set_push(conn, p["id"], push["push_at"], push["push_sha"])
                    report.pushed += 1
                elif p["push_at"]:
                    db.set_push(conn, p["id"], None, None)
                # Nothing else refreshes an archived row, so a moved head or update refetches it.
                if new_state == "OPEN" and (
                    (node.get("headRefOid") or p["head_sha"]) != p["head_sha"]
                    or (node.get("updatedAt") or p["updated_at"]) != p["updated_at"]
                ):
                    outdated.append(p)
        report.refreshed = self._refresh_pr_rows(outdated, force=False)

    def _refresh_companions(self, kept_ids: set[str], report: RefreshReport) -> str:
        """Attach each Branch set's migration PR by head branch, returning the repo searched."""
        cfg, conn = self.cfg, self.conn
        if not (cfg.companion.enabled and cfg.companion.repo):
            return ""
        repo = cfg.companion.repo
        searched = {
            pr["head_branch"] for pr in db.list_prs(conn)
            if pr["id"] in kept_ids and pr["repo"] != repo and pr["head_branch"]
        }
        # The upgrade repo is private, so no access must cost the Companions, not the refresh.
        try:
            by_branch = self.gh.open_prs_by_head_branch(repo, sorted(searched))
        except github.GithubError as e:
            log.debug("companion lookup skipped (%s): %s", repo, e)
            return ""

        now = self._stamp()
        stored = db.list_companions(conn)
        matched: dict[str, dict] = {}
        dropped: list[str] = []
        # By number, so both halves of a pair agree on what became of their migration.
        vanished: set[int] = set()
        for pr in db.list_prs(conn):
            pr_id, branch = pr["id"], pr["head_branch"]
            if pr["repo"] == repo:
                continue
            prev = stored.get(pr_id)
            if branch not in searched:
                # Unasked, so absence proves nothing and only the stored row can be re-checked.
                if prev is not None and prev["state"] == "OPEN":
                    vanished.add(prev["number"])
                continue
            entry = by_branch.get(branch)
            if entry:
                matched[pr_id] = entry
            elif prev is None:
                continue
            elif prev["head_branch"] != branch:
                # Force-pushed onto another branch, so the attached migration is another change's.
                dropped.append(pr_id)
            elif prev["state"] == "OPEN":
                # Gone from the search but often merely merged ahead of its bundle, so re-check it.
                vanished.add(prev["number"])

        states: dict[int, str] = {}
        if vanished:
            try:
                states = self.gh.pr_states(repo, sorted(vanished))
            except github.GithubError as e:
                log.debug("companion state re-check skipped: %s", e)

        with db.transaction(conn):
            for pr_id in dropped:
                db.delete_companion(conn, pr_id)
            for pr_id, entry in matched.items():
                db.upsert_companion(conn, pr_id, {
                    "repo": repo,
                    "number": entry["number"],
                    "url": entry["url"],
                    "title": entry.get("title") or "",
                    "author": entry.get("author") or "",
                    "state": (entry.get("state") or "open").upper(),
                    "is_draft": int(bool(entry.get("draft"))),
                    "head_branch": entry["head_branch"],
                    "head_sha": entry.get("head_sha") or "",
                    "fetched_at": now,
                })
            for number, state in states.items():
                db.set_companion_state(conn, repo, number, state)

        # Only the active queue's migrations are prompt context, so only theirs need a diff.
        for pr_id, entry in matched.items():
            if pr_id not in kept_ids:
                continue
            self._store_patch({
                "repo": repo,
                "number": entry["number"],
                "head_sha": entry.get("head_sha") or "",
                # Unknown for a migration script, so only the byte guards apply.
                "changed_files": 0,
            }, force=False)
        report.companions = len(matched)
        return repo

    def _ai_attempt_exhausted(self, head_sha: str, context_heads: str,
                              companion_head_sha: str, now: datetime) -> bool:
        """Whether this review context has failed too often, or too recently."""
        row = db.get_ai_attempt(self.conn, head_sha, context_heads, companion_head_sha)
        if row is None:
            return False
        if row["attempts"] >= self.cfg.ai.max_attempts:
            return True
        hours = _AI_RETRY_BACKOFF_HOURS[min(row["attempts"], len(_AI_RETRY_BACKOFF_HOURS)) - 1]
        return derive.parse_iso(row["last_attempt_at"]) + timedelta(hours=hours) > now

    def _build_review_queue(
        self, kept_ids: set[str], *, companion_repo: str = "", limit: int | None = None,
    ) -> tuple[list[ai.ReviewRequest], dict[str, tuple[str, str]]]:
        """Queue the settled PRs whose compacted diff fits the prompt, cheapest first.

        :param companion_repo: the repo searched for migrations, "" when none was looked for
        :return: the requests, and head_sha -> (context_heads, companion_head_sha) to key results
        """
        conn, now = self.conn, self.clock()
        max_diff_chars = self.cfg.ai.review_max_diff_chars
        pr_rows = {pr_id: db.get_cached_pr(conn, pr_id) for pr_id in kept_ids}
        pr_rows = {k: v for k, v in pr_rows.items() if v is not None}
        set_of = {
            m["id"]: s
            for s in branch_set.group(pr_rows.values(), db.list_companion_rows(conn, pr_rows))
            for m in s.halves
        }
        modules_by_pr = db.list_modules(conn)

        def patch_text(sha: str) -> str:
            row = db.get_diff(conn, sha)
            return (row["patch_text"] if row else None) or ""

        def diff_cached(half) -> bool:
            return bool(patch_text(half["head_sha"]))

        # (size, head_sha, request), sorted so a capped run takes the cheapest, in a fixed order.
        sized: list[tuple[int, str, ai.ReviewRequest]] = []
        review_context: dict[str, tuple[str, str]] = {}

        for pr_id, pr_row in pr_rows.items():
            head_sha = pr_row["head_sha"]
            diff_text = patch_text(head_sha)
            if not diff_text:
                continue
            # The raw text travels on, so the prompt can tell the model what compaction omitted.
            compacted = derive.compact_diff(
                diff_text, repo=pr_row["repo"], number=pr_row["number"],
            )
            if not compacted.text or len(compacted.text) > max_diff_chars:
                continue

            bset = set_of[pr_id]
            context_heads = bset.context_heads(pr_row, diff_cached)

            # Unlike a code half, the Companion is context on existence alone, cached diff or not.
            companion_row = bset.companion
            companion_head_sha = companion_row["head_sha"] if companion_row else ""
            companion = None
            if companion_row:
                companion = ai.Companion(
                    repo=companion_row["repo"],
                    number=companion_row["number"],
                    title=companion_row["title"],
                    state=companion_row["state"],
                    diff=patch_text(companion_row["head_sha"]),
                )

            if db.has_manual_ai_review(conn, head_sha):
                continue
            if db.get_ai_review(
                conn, head_sha, context_heads, companion_head_sha,
            ) is not None:
                continue
            if self._ai_attempt_exhausted(head_sha, context_heads, companion_head_sha, now):
                continue

            sized.append((len(compacted.text), head_sha, ai.ReviewRequest(
                head_sha=head_sha,
                title=pr_row["title"],
                body=pr_row["body"] or "",
                modules=modules_by_pr.get(pr_id, []),
                branch=pr_row["target_branch"],
                diff=diff_text,
                repo=pr_row["repo"],
                number=pr_row["number"],
                context=[
                    ai.ContextHalf(h["repo"], h["number"], h["title"], patch_text(h["head_sha"]))
                    for h in bset.context(pr_row, diff_cached)
                ],
                companion=companion,
                companion_repo=companion_repo,
            )))
            review_context[head_sha] = (context_heads, companion_head_sha)

        sized.sort(key=lambda item: (item[0], item[1]))
        if limit is not None:
            sized = sized[:limit]
        requests = [r for _, _, r in sized]
        return requests, {k: review_context[k] for _, k, _ in sized}

    def _node_to_rows(
        self, node: dict,
    ) -> tuple[dict, list[str], list[dict], list[dict], list[dict]]:
        login = self.cfg.github_login
        repo = node["repository"]["nameWithOwner"]
        paths = [f["path"] for f in (node.get("files") or {}).get("nodes", [])]

        modules = derive.modules_for(repo, paths)

        timeline = (node.get("timelineItems") or {}).get("nodes") or []
        review_requested_at = derive.latest_review_requested_at(
            timeline, login, fallback=node["updatedAt"],
        )
        prev_reviewed = derive.previously_reviewed(timeline, login)

        thread_nodes = (node.get("reviewThreads") or {}).get("nodes") or []
        th = derive.derive_threads(thread_nodes, login)
        comments, my_pending_review = derive.derive_comments(node, login)

        latest_reviews = (node.get("latestReviews") or {}).get("nodes") or []
        review_requests = (node.get("reviewRequests") or {}).get("nodes") or []
        reviewers = derive.derive_reviewers(latest_reviews, review_requests)

        commits = (node.get("commits") or {}).get("nodes") or []
        rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
        ci_state, runbot_url = derive.status_check_state(rollup)
        ci_failures = derive.failing_checks(rollup)

        parsed_task = derive.parse_linked_task(node.get("body"))
        task_kind, task_id = parsed_task if parsed_task else (None, None)

        pr_row = {
            "id": _node_id(node),
            "repo": repo,
            "number": node["number"],
            "title": node["title"],
            "url": node["url"],
            "author": (node.get("author") or {}).get("login") or "(unknown)",
            "is_draft": int(bool(node.get("isDraft"))),
            "target_branch": node["baseRefName"],
            "head_branch": node["headRefName"],
            "head_sha": node["headRefOid"],
            "created_at": node["createdAt"],
            "updated_at": node["updatedAt"],
            "review_requested_at": review_requested_at,
            "previously_reviewed": int(prev_reviewed),
            "mergeable": node.get("mergeable"),
            "ci_state": ci_state,
            "ci_failures": json.dumps(ci_failures) if ci_failures else None,
            "runbot_url": runbot_url,
            "additions": node["additions"],
            "deletions": node["deletions"],
            "changed_files": node["changedFiles"],
            "unresolved_threads": th.unresolved,
            "awaiting_my_reply": int(th.awaiting_my_reply),
            "linked_task": task_id,
            "linked_task_kind": task_kind,
            "body": node.get("body"),
            "archived_at": None,
            "state": node.get("state") or "OPEN",
            "my_pending_review": int(my_pending_review),
            "fetched_at": self._stamp(),
        }
        return pr_row, modules, reviewers, th.threads, comments
