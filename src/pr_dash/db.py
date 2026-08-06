from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 21

# The bundle's migration PR (odoo/upgrade), keyed by the cached PR it belongs to.
#
# Deliberately *not* a row in `pr`: nobody is requested as a reviewer on an
# upgrade PR, so it has no review request, no review state and no place in the
# queue. Hanging it off `pr(id)` instead means it can never be picked up by
# db.list_prs (the queue, the sweep and every KPI count read that table), and the
# cascade takes it with the PR when the sweep deletes one.
#
# Both halves of a pair get their own row pointing at the same migration - the
# pair is assembled at render time from independent PR records, so a per-PR row
# is what makes the migration visible whichever half is being looked at.
COMPANION_SCHEMA_SQL = """
CREATE TABLE pr_companion (
  pr_id       TEXT PRIMARY KEY REFERENCES pr(id) ON DELETE CASCADE,
  repo        TEXT NOT NULL,
  number      INTEGER NOT NULL,
  url         TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  author      TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL DEFAULT 'OPEN',
  is_draft    INTEGER NOT NULL DEFAULT 0,
  head_branch TEXT NOT NULL,
  head_sha    TEXT NOT NULL DEFAULT '',
  fetched_at  TEXT NOT NULL
);
"""

# The tracked-PR tables, kept as a named constant so the fresh-database schema
# and the v15 migration create them from one definition and can't drift.
#
# Tracked PRs are deliberately *not* rows in `pr`: they are PRs nobody asked me
# to review, so they have no review-request timestamp, no review state, and must
# never be touched by the review-queue sweep. Separate tables keep the two
# lifecycles from interfering.
TRACKED_SCHEMA_SQL = """
CREATE TABLE tracked (
  id            TEXT PRIMARY KEY,
  repo          TEXT NOT NULL,
  number        INTEGER NOT NULL,
  url           TEXT NOT NULL,
  title         TEXT NOT NULL DEFAULT '',
  author        TEXT NOT NULL DEFAULT '',
  state         TEXT NOT NULL DEFAULT 'OPEN',
  is_draft      INTEGER NOT NULL DEFAULT 0,
  target_branch TEXT NOT NULL DEFAULT '',
  head_sha      TEXT NOT NULL DEFAULT '',
  body          TEXT,
  ci_state      TEXT,
  comment_count INTEGER NOT NULL DEFAULT 0,
  activity_count     INTEGER NOT NULL DEFAULT 0,
  review_count       INTEGER NOT NULL DEFAULT 0,
  thread_count       INTEGER NOT NULL DEFAULT 0,
  unresolved_threads INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT,
  updated_at    TEXT,
  closed_at     TEXT,
  merged_at     TEXT,
  source        TEXT NOT NULL DEFAULT 'notif',
  added_at      TEXT NOT NULL,
  dismissed_at  TEXT,
  fetched_at    TEXT
);

CREATE TABLE tracked_comment (
  pr_id      TEXT NOT NULL REFERENCES tracked(id) ON DELETE CASCADE,
  comment_id TEXT NOT NULL,
  kind       TEXT NOT NULL,
  thread_id  TEXT,
  parent_id  TEXT,
  author     TEXT,
  created_at TEXT,
  body       TEXT,
  path       TEXT,
  state      TEXT,
  url        TEXT,
  PRIMARY KEY (pr_id, comment_id)
);

CREATE TABLE tracked_seen (
  pr_id         TEXT PRIMARY KEY REFERENCES tracked(id) ON DELETE CASCADE,
  state          TEXT,
  head_sha       TEXT,
  comment_count  INTEGER,
  activity_count INTEGER,
  seen_at        TEXT NOT NULL
);

CREATE INDEX idx_tracked_comment_pr ON tracked_comment(pr_id);
"""

SCHEMA_SQL = """
CREATE TABLE pr (
  id                   TEXT PRIMARY KEY,
  repo                 TEXT NOT NULL,
  number               INTEGER NOT NULL,
  title                TEXT NOT NULL,
  url                  TEXT NOT NULL,
  author               TEXT NOT NULL,
  is_draft             INTEGER NOT NULL DEFAULT 0,
  target_branch        TEXT NOT NULL,
  head_branch          TEXT NOT NULL,
  head_sha             TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  review_requested_at  TEXT NOT NULL,
  previously_reviewed  INTEGER NOT NULL,
  mergeable            TEXT,
  ci_state             TEXT,
  ci_failures          TEXT,
  runbot_url           TEXT,
  additions            INTEGER NOT NULL,
  deletions            INTEGER NOT NULL,
  changed_files        INTEGER NOT NULL,
  unresolved_threads   INTEGER NOT NULL DEFAULT 0,
  awaiting_my_reply    INTEGER NOT NULL DEFAULT 0,
  linked_task          TEXT,
  linked_task_kind     TEXT,
  body                 TEXT,
  archived_at          TEXT,
  state                TEXT NOT NULL DEFAULT 'OPEN',
  my_pending_review    INTEGER NOT NULL DEFAULT 0,
  ping_at              TEXT,
  ping_author          TEXT,
  ping_snippet         TEXT,
  push_at              TEXT,
  push_sha             TEXT,
  fetched_at           TEXT NOT NULL
);

CREATE TABLE pr_module (
  pr_id  TEXT NOT NULL REFERENCES pr(id) ON DELETE CASCADE,
  module TEXT NOT NULL,
  PRIMARY KEY (pr_id, module)
);

CREATE TABLE pr_reviewer (
  pr_id TEXT NOT NULL REFERENCES pr(id) ON DELETE CASCADE,
  kind  TEXT NOT NULL,
  name  TEXT NOT NULL,
  state TEXT NOT NULL,
  PRIMARY KEY (pr_id, kind, name)
);

CREATE TABLE pr_thread (
  pr_id             TEXT NOT NULL REFERENCES pr(id) ON DELETE CASCADE,
  thread_id         TEXT NOT NULL,
  is_resolved       INTEGER NOT NULL,
  i_participated    INTEGER NOT NULL,
  last_reply_at     TEXT NOT NULL,
  last_reply_author TEXT NOT NULL,
  PRIMARY KEY (pr_id, thread_id)
);

CREATE TABLE pr_comment (
  pr_id      TEXT NOT NULL REFERENCES pr(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  thread_id  TEXT,
  comment_id TEXT NOT NULL,
  author     TEXT,
  created_at TEXT,
  body       TEXT,
  path       TEXT,
  state      TEXT,
  url        TEXT,
  PRIMARY KEY (pr_id, kind, comment_id)
);

CREATE TABLE complexity (
  head_sha    TEXT PRIMARY KEY,
  bucket      TEXT NOT NULL,
  score       REAL NOT NULL,
  method      TEXT NOT NULL,
  notes       TEXT,
  computed_at TEXT NOT NULL
);

CREATE TABLE pr_diff (
  head_sha   TEXT PRIMARY KEY,
  patch_text TEXT,
  truncated  INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT NOT NULL
);

CREATE TABLE ai_review (
  head_sha           TEXT PRIMARY KEY,
  sibling_head_sha   TEXT NOT NULL DEFAULT '',
  companion_head_sha TEXT NOT NULL DEFAULT '',
  summary            TEXT NOT NULL,
  concerns           TEXT NOT NULL DEFAULT '[]',
  verdict            TEXT NOT NULL,
  source             TEXT NOT NULL DEFAULT 'auto',
  computed_at        TEXT NOT NULL
);

CREATE TABLE review_snapshot (
  pr_id        TEXT PRIMARY KEY REFERENCES pr(id) ON DELETE CASCADE,
  reviewed_sha TEXT NOT NULL,
  signatures   TEXT NOT NULL,
  snapped_at   TEXT NOT NULL
);

CREATE TABLE seen (
  pr_id      TEXT PRIMARY KEY REFERENCES pr(id) ON DELETE CASCADE,
  head_sha   TEXT,
  ci_state   TEXT,
  thread_sig TEXT,
  seen_at    TEXT NOT NULL
);

CREATE INDEX idx_pr_module_pr ON pr_module(pr_id);
CREATE INDEX idx_pr_reviewer_pr ON pr_reviewer(pr_id);
CREATE INDEX idx_pr_thread_pr ON pr_thread(pr_id);
CREATE INDEX idx_pr_comment_pr ON pr_comment(pr_id);
""" + TRACKED_SCHEMA_SQL + COMPANION_SCHEMA_SQL


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # Wait (rather than immediately raising "database is locked") when another
    # pr-dash run holds the write lock - e.g. a cron refresh overlapping a manual one.
    conn.execute("PRAGMA busy_timeout = 5000")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == SCHEMA_VERSION:
        return
    if current == 0:
        conn.executescript(SCHEMA_SQL)
    if current < 2:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
        if "linked_task_kind" not in cols:
            conn.execute("ALTER TABLE pr ADD COLUMN linked_task_kind TEXT")
    if current < 3:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
        if "archived_at" not in cols:
            conn.execute("ALTER TABLE pr ADD COLUMN archived_at TEXT")
    if current < 4:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "ai_review" not in tables:
            conn.execute(
                "CREATE TABLE ai_review ("
                "  head_sha    TEXT PRIMARY KEY,"
                "  summary     TEXT NOT NULL,"
                "  concerns    TEXT NOT NULL DEFAULT '[]',"
                "  verdict     TEXT NOT NULL,"
                "  computed_at TEXT NOT NULL"
                ")"
            )
    if current < 5:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_review)").fetchall()}
        if "sibling_head_sha" not in cols:
            conn.execute(
                "ALTER TABLE ai_review ADD COLUMN sibling_head_sha TEXT NOT NULL DEFAULT ''"
            )
        # Old rows have sibling_head_sha='', which won't match a paired lookup -
        # so paired PRs naturally re-review while still-single PRs keep their cache hit.
    if current < 6:
        # Diffs are now fetched as `.diff` (one clean combined diff) instead of
        # `.patch` (mbox), so the frontend can reliably split them per file.
        # Drop cached patches so they re-fetch in the new format on next refresh.
        conn.execute("DELETE FROM pr_diff")
    if current < 7:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "review_snapshot" not in tables:
            conn.execute(
                "CREATE TABLE review_snapshot ("
                "  pr_id        TEXT PRIMARY KEY REFERENCES pr(id) ON DELETE CASCADE,"
                "  reviewed_sha TEXT NOT NULL,"
                "  signatures   TEXT NOT NULL,"
                "  snapped_at   TEXT NOT NULL"
                ")"
            )
    if current < 8:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "seen" not in tables:
            conn.execute(
                "CREATE TABLE seen ("
                "  pr_id      TEXT PRIMARY KEY REFERENCES pr(id) ON DELETE CASCADE,"
                "  head_sha   TEXT,"
                "  ci_state   TEXT,"
                "  thread_sig TEXT,"
                "  seen_at    TEXT NOT NULL"
                ")"
            )
    if current < 9:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
        if "ci_failures" not in cols:
            conn.execute("ALTER TABLE pr ADD COLUMN ci_failures TEXT")
    if current < 10:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
        if "is_draft" not in cols:
            conn.execute("ALTER TABLE pr ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0")
    if current < 11:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
        if "state" not in cols:
            # Pre-existing archived rows default to OPEN; the refresh re-checks
            # the ones that matter (archived siblings of active pairs).
            conn.execute("ALTER TABLE pr ADD COLUMN state TEXT NOT NULL DEFAULT 'OPEN'")
    if current < 12:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
        if "my_pending_review" not in cols:
            conn.execute(
                "ALTER TABLE pr ADD COLUMN my_pending_review INTEGER NOT NULL DEFAULT 0"
            )
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "pr_comment" not in tables:
            conn.execute(
                "CREATE TABLE pr_comment ("
                "  pr_id      TEXT NOT NULL REFERENCES pr(id) ON DELETE CASCADE,"
                "  kind       TEXT NOT NULL,"
                "  thread_id  TEXT,"
                "  comment_id TEXT NOT NULL,"
                "  author     TEXT,"
                "  created_at TEXT,"
                "  body       TEXT,"
                "  path       TEXT,"
                "  state      TEXT,"
                "  url        TEXT,"
                "  PRIMARY KEY (pr_id, kind, comment_id)"
                ")"
            )
            conn.execute("CREATE INDEX idx_pr_comment_pr ON pr_comment(pr_id)")
    if current < 13:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
        for col in ("ping_at", "ping_author", "ping_snippet"):
            if col not in cols:
                conn.execute(f"ALTER TABLE pr ADD COLUMN {col} TEXT")
    if current < 14:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
        for col in ("push_at", "push_sha"):
            if col not in cols:
                conn.execute(f"ALTER TABLE pr ADD COLUMN {col} TEXT")
    if current < 15:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "tracked" not in tables:
            conn.executescript(TRACKED_SCHEMA_SQL)
    if current < 16:
        # v15 shipped with issue-comment counts only, which made a PR whose
        # whole discussion lives in review threads look silent.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tracked)").fetchall()}
        for col in ("activity_count", "unresolved_threads"):
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE tracked ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
                )
        seen_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(tracked_seen)").fetchall()
        }
        if "activity_count" not in seen_cols:
            conn.execute("ALTER TABLE tracked_seen ADD COLUMN activity_count INTEGER")
        # Force a re-fetch of every tracked row so the new counts get populated
        # rather than sitting at 0 until each PR happens to go stale.
        conn.execute("UPDATE tracked SET fetched_at = NULL")
    if current < 17:
        # Thread comments now nest under the review that raised them, which
        # needs the review id (parent_id) and the thread they belong to.
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(tracked_comment)").fetchall()
        }
        for col in ("thread_id", "parent_id"):
            if col not in cols:
                conn.execute(f"ALTER TABLE tracked_comment ADD COLUMN {col} TEXT")
        conn.execute("UPDATE tracked SET fetched_at = NULL")
    if current < 18:
        # A single summed activity_count reads as a meaningless "91 discussion";
        # the row shows the parts, so they have to be stored separately.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tracked)").fetchall()}
        for col in ("review_count", "thread_count"):
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE tracked ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
                )
        conn.execute("UPDATE tracked SET fetched_at = NULL")
    if current < 19:
        # An over-threshold diff is now compacted instead of dropped, but
        # _store_patch skips a head sha it already has a row for - so every PR
        # dropped under the old rule would keep its empty row and never get a
        # cached diff or an AI first pass. Clear them to re-fetch once; the ones
        # compaction genuinely cannot save just land back here empty.
        conn.execute("DELETE FROM pr_diff WHERE patch_text IS NULL")
    if current < 20:
        # Hand-written reviews have to outlive the automatic pass; everything
        # already on disk came from the model.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_review)").fetchall()}
        if "source" not in cols:
            conn.execute(
                "ALTER TABLE ai_review ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'"
            )
    if current < 21:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "pr_companion" not in tables:
            conn.executescript(COMPANION_SCHEMA_SQL)
        # Whether a companion migration was in the prompt changes the answer -
        # it is the difference between flagging a missing migration and not - so
        # it joins the review's cache key. Existing rows default to '', which
        # still matches every PR that has no companion (the overwhelming
        # majority) and correctly misses the ones that just gained one.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_review)").fetchall()}
        if "companion_head_sha" not in cols:
            conn.execute(
                "ALTER TABLE ai_review "
                "ADD COLUMN companion_head_sha TEXT NOT NULL DEFAULT ''"
            )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _upsert(conn: sqlite3.Connection, table: str, row: dict,
            conflict_keys: list[str]) -> None:
    """INSERT ... ON CONFLICT(conflict_keys) DO UPDATE, setting every non-key
    column from the inserted row. Shared by all the upsert_* helpers so a new
    column only needs to be added to the row dict, not to a hand-written SET."""
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    conflict = ", ".join(conflict_keys)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in conflict_keys)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {updates}",
        row,
    )


def get_cached_pr(conn: sqlite3.Connection, pr_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pr WHERE id = ?", (pr_id,)).fetchone()


def upsert_pr(conn: sqlite3.Connection, row: dict) -> None:
    _upsert(conn, "pr", row, ["id"])


def replace_modules(conn: sqlite3.Connection, pr_id: str, modules: list[str]) -> None:
    conn.execute("DELETE FROM pr_module WHERE pr_id = ?", (pr_id,))
    conn.executemany(
        "INSERT INTO pr_module (pr_id, module) VALUES (?, ?)",
        [(pr_id, m) for m in modules],
    )


def replace_reviewers(conn: sqlite3.Connection, pr_id: str, reviewers: list[dict]) -> None:
    conn.execute("DELETE FROM pr_reviewer WHERE pr_id = ?", (pr_id,))
    conn.executemany(
        "INSERT INTO pr_reviewer (pr_id, kind, name, state) VALUES (?, ?, ?, ?)",
        [(pr_id, r["kind"], r["name"], r["state"]) for r in reviewers],
    )


def set_pr_state(conn: sqlite3.Connection, pr_id: str, state: str) -> None:
    conn.execute("UPDATE pr SET state = ? WHERE id = ?", (state, pr_id))


def set_ping(conn: sqlite3.Connection, pr_id: str, ping_at: str | None,
             ping_author: str | None, ping_snippet: str | None) -> None:
    """Record (or clear, with all-None) an informal re-review ping on a PR."""
    conn.execute(
        "UPDATE pr SET ping_at = ?, ping_author = ?, ping_snippet = ? WHERE id = ?",
        (ping_at, ping_author, ping_snippet, pr_id),
    )


def set_push(conn: sqlite3.Connection, pr_id: str, push_at: str | None,
             push_sha: str | None) -> None:
    """Record (or clear, with all-None) a push landed after my last review.

    Written by the archived reconcile only, so it never competes with the normal
    refresh path: while a PR is still in the request set a new push is the thing
    being reviewed, not an after-the-fact event.
    """
    conn.execute(
        "UPDATE pr SET push_at = ?, push_sha = ? WHERE id = ?",
        (push_at, push_sha, pr_id),
    )


def set_my_review_state(conn: sqlite3.Connection, pr_id: str, login: str, state: str) -> None:
    """Upsert only my own reviewer row, leaving other reviewers untouched.

    Used by backfill to record the review decision (APPROVED / CHANGES_REQUESTED
    / COMMENTED) on PRs that left the request queue, which the search path never
    captures - without it those rows render as my_review_state=PENDING.
    """
    conn.execute(
        "INSERT INTO pr_reviewer (pr_id, kind, name, state) VALUES (?, 'user', ?, ?) "
        "ON CONFLICT(pr_id, kind, name) DO UPDATE SET state = excluded.state",
        (pr_id, login, state),
    )


def replace_threads(conn: sqlite3.Connection, pr_id: str, threads: list[dict]) -> None:
    conn.execute("DELETE FROM pr_thread WHERE pr_id = ?", (pr_id,))
    conn.executemany(
        "INSERT INTO pr_thread (pr_id, thread_id, is_resolved, i_participated, "
        "last_reply_at, last_reply_author) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                pr_id,
                t["thread_id"],
                t["is_resolved"],
                t["i_participated"],
                t["last_reply_at"],
                t["last_reply_author"],
            )
            for t in threads
        ],
    )


_COMPANION_COLS = ["repo", "number", "url", "title", "author", "state",
                   "is_draft", "head_branch", "head_sha", "fetched_at"]


def upsert_companion(conn: sqlite3.Connection, pr_id: str, row: dict) -> None:
    """Attach (or re-attach) a PR's companion migration PR."""
    _upsert(conn, "pr_companion",
            {"pr_id": pr_id, **{c: row[c] for c in _COMPANION_COLS}}, ["pr_id"])


def delete_companion(conn: sqlite3.Connection, pr_id: str) -> None:
    conn.execute("DELETE FROM pr_companion WHERE pr_id = ?", (pr_id,))


def set_companion_state(conn: sqlite3.Connection, repo: str, number: int,
                        state: str) -> None:
    """Record a companion's current state wherever it is attached. Keyed on the
    companion itself, not on a pr_id: both halves of a pair point at the same
    migration and must not disagree about whether it merged."""
    conn.execute(
        "UPDATE pr_companion SET state = ? WHERE repo = ? AND number = ?",
        (state, repo, number),
    )


def get_companion(conn: sqlite3.Connection, pr_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pr_companion WHERE pr_id = ?", (pr_id,),
    ).fetchone()


def list_companions(conn: sqlite3.Connection) -> dict[str, dict]:
    return {
        r["pr_id"]: dict(r)
        for r in conn.execute("SELECT * FROM pr_companion").fetchall()
    }


def upsert_complexity(conn: sqlite3.Connection, row: dict) -> None:
    _upsert(conn, "complexity", row, ["head_sha"])


def get_complexity(conn: sqlite3.Connection, head_sha: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM complexity WHERE head_sha = ?", (head_sha,)).fetchone()


def upsert_diff(conn: sqlite3.Connection, head_sha: str, patch_text: str | None,
                truncated: bool, fetched_at: str) -> None:
    _upsert(conn, "pr_diff", {
        "head_sha": head_sha,
        "patch_text": patch_text,
        "truncated": int(truncated),
        "fetched_at": fetched_at,
    }, ["head_sha"])


def get_diff(conn: sqlite3.Connection, head_sha: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pr_diff WHERE head_sha = ?", (head_sha,)).fetchone()


def get_review_snapshot(conn: sqlite3.Connection, pr_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM review_snapshot WHERE pr_id = ?", (pr_id,)
    ).fetchone()


def upsert_review_snapshot(conn: sqlite3.Connection, pr_id: str, reviewed_sha: str,
                           signatures_json: str, snapped_at: str) -> None:
    _upsert(conn, "review_snapshot", {
        "pr_id": pr_id,
        "reviewed_sha": reviewed_sha,
        "signatures": signatures_json,
        "snapped_at": snapped_at,
    }, ["pr_id"])


def list_seen(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["pr_id"]: r for r in conn.execute("SELECT * FROM seen").fetchall()}


def upsert_seen(conn: sqlite3.Connection, pr_id: str, head_sha: str | None,
                ci_state: str | None, thread_sig: str, seen_at: str) -> None:
    _upsert(conn, "seen", {
        "pr_id": pr_id,
        "head_sha": head_sha,
        "ci_state": ci_state,
        "thread_sig": thread_sig,
        "seen_at": seen_at,
    }, ["pr_id"])


def upsert_ai_review(conn: sqlite3.Connection, head_sha: str, sibling_head_sha: str,
                     summary: str, concerns_json: str, verdict: str,
                     computed_at: str, source: str = "auto",
                     companion_head_sha: str = "") -> None:
    _upsert(conn, "ai_review", {
        "head_sha": head_sha,
        "sibling_head_sha": sibling_head_sha,
        "companion_head_sha": companion_head_sha,
        "summary": summary,
        "concerns": concerns_json,
        "verdict": verdict,
        "source": source,
        "computed_at": computed_at,
    }, ["head_sha"])


def has_manual_ai_review(conn: sqlite3.Connection, head_sha: str) -> bool:
    """Whether this sha carries a hand-written review, whatever pair context it
    was written against. The automatic pass keys its cache lookup on that
    context, so a manual review of a PR whose pair state later moved would read
    as a miss and be silently overwritten by a model pass - the one thing a
    manual backfill must not do."""
    row = conn.execute(
        "SELECT 1 FROM ai_review WHERE head_sha = ? AND source = 'manual'",
        (head_sha,),
    ).fetchone()
    return row is not None


def get_ai_review(conn: sqlite3.Connection, head_sha: str,
                  sibling_head_sha: str = "",
                  companion_head_sha: str = "") -> sqlite3.Row | None:
    """Return cached review only when the surrounding context matches.

    Paired PRs reviewed before pairing don't hit cache - they re-review with
    context. A PR that gained (or lost) its companion migration misses for the
    same reason: the prompt told the model a migration exists, and that is
    precisely what decides whether a missing one is worth flagging.
    """
    return conn.execute(
        "SELECT * FROM ai_review WHERE head_sha = ? AND sibling_head_sha = ? "
        "AND companion_head_sha = ?",
        (head_sha, sibling_head_sha, companion_head_sha),
    ).fetchone()


def get_ai_review_any(conn: sqlite3.Connection, head_sha: str) -> sqlite3.Row | None:
    """Fetch a review by head_sha without filtering on sibling context.
    Used by the renderer, which must validate the pair context itself."""
    return conn.execute(
        "SELECT * FROM ai_review WHERE head_sha = ?", (head_sha,),
    ).fetchone()


def delete_candidates(conn: sqlite3.Connection, keep_ids: set[str]) -> list[sqlite3.Row]:
    """PRs that fell out of the active set and would be deleted by a sweep:
    not in `keep_ids`, not yet archived, cached previously_reviewed=0.
    Returned so the caller can re-verify them against GitHub before deletion."""
    rows = conn.execute(
        "SELECT id, repo, number FROM pr "
        "WHERE archived_at IS NULL AND previously_reviewed = 0"
    ).fetchall()
    return [r for r in rows if r["id"] not in keep_ids]


def mark_reviewed(conn: sqlite3.Connection, pr_ids: set[str]) -> None:
    if not pr_ids:
        return
    placeholders = ", ".join("?" for _ in pr_ids)
    conn.execute(
        f"UPDATE pr SET previously_reviewed = 1 WHERE id IN ({placeholders})",
        tuple(pr_ids),
    )


def sweep(conn: sqlite3.Connection, keep_ids: set[str], now: str,
          *, delete: bool = True) -> tuple[int, int]:
    """Archive previously-reviewed PRs that fell out of the result set;
    delete those that were never reviewed. Returns (archived, deleted).

    When `delete` is False (e.g. review reconciliation failed this run), only
    archiving runs, so an unverified PR is never lost to a transient error.
    """
    if not keep_ids:
        cur_a = conn.execute(
            "UPDATE pr SET archived_at = ? "
            "WHERE archived_at IS NULL AND previously_reviewed = 1",
            (now,),
        )
        archived = cur_a.rowcount
        if not delete:
            return archived, 0
        cur_d = conn.execute("DELETE FROM pr WHERE previously_reviewed = 0")
        return archived, cur_d.rowcount
    placeholders = ", ".join("?" for _ in keep_ids)
    keep_tuple = tuple(keep_ids)
    cur_a = conn.execute(
        f"UPDATE pr SET archived_at = ? "
        f"WHERE id NOT IN ({placeholders}) "
        f"AND archived_at IS NULL AND previously_reviewed = 1",
        (now, *keep_tuple),
    )
    archived = cur_a.rowcount
    if not delete:
        return archived, 0
    cur_d = conn.execute(
        f"DELETE FROM pr WHERE id NOT IN ({placeholders}) AND previously_reviewed = 0",
        keep_tuple,
    )
    return archived, cur_d.rowcount


def max_fetched_at(conn: sqlite3.Connection) -> str | None:
    """Newest `fetched_at` across all cached PRs - a proxy for cache freshness.
    None when the cache is empty."""
    row = conn.execute("SELECT MAX(fetched_at) AS m FROM pr").fetchone()
    return row["m"] if row else None


def list_prs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pr ORDER BY review_requested_at DESC").fetchall()


def list_modules(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute("SELECT pr_id, module FROM pr_module ORDER BY pr_id, module").fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["pr_id"], []).append(r["module"])
    return out


def list_reviewers(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT pr_id, kind, name, state FROM pr_reviewer ORDER BY pr_id, name"
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["pr_id"], []).append(
            {"kind": r["kind"], "name": r["name"], "state": r["state"]}
        )
    return out


def list_threads(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT pr_id, thread_id, is_resolved, i_participated, last_reply_at, last_reply_author "
        "FROM pr_thread ORDER BY pr_id, last_reply_at DESC"
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["pr_id"], []).append(dict(r))
    return out


_COMMENT_COLS = ["kind", "thread_id", "comment_id", "author",
                 "created_at", "body", "path", "state", "url"]


def replace_comments(conn: sqlite3.Connection, pr_id: str, comments: list[dict]) -> None:
    conn.execute("DELETE FROM pr_comment WHERE pr_id = ?", (pr_id,))
    conn.executemany(
        f"INSERT INTO pr_comment (pr_id, {', '.join(_COMMENT_COLS)}) "
        f"VALUES (?, {', '.join('?' for _ in _COMMENT_COLS)})",
        [(pr_id, *(c.get(col) for col in _COMMENT_COLS)) for c in comments],
    )


def list_comments(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT pr_id, kind, thread_id, comment_id, author, created_at, body, path, state, url "
        "FROM pr_comment ORDER BY pr_id, created_at"
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["pr_id"], []).append(dict(r))
    return out


def has_comments(conn: sqlite3.Connection, pr_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pr_comment WHERE pr_id = ? LIMIT 1", (pr_id,)
    ).fetchone()
    return row is not None


TRACKED_STATE_COLS = [
    "title", "author", "state", "is_draft", "target_branch", "head_sha", "body",
    "ci_state", "comment_count", "created_at", "updated_at", "closed_at",
    "merged_at", "fetched_at", "activity_count", "unresolved_threads",
    "review_count", "thread_count",
]


def add_tracked(conn: sqlite3.Connection, pr_id: str, repo: str, number: int,
                url: str, source: str, added_at: str) -> bool:
    """Start tracking a PR. Returns True if it was newly added.

    Tracking is sticky: an already-tracked row keeps its original `added_at` and
    `source`, so re-seeding from notifications never rewrites the provenance of
    one you added by hand.

    Only an explicit `source='manual'` add revives a dismissed row. The
    notification seed must not: a merged PR keeps its `reason=manual` thread for
    as long as GitHub retains it, so an un-dismissing seed would resurrect every
    PR the moment after you cleared it.
    """
    existing = conn.execute(
        "SELECT dismissed_at FROM tracked WHERE id = ?", (pr_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO tracked (id, repo, number, url, source, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pr_id, repo, number, url, source, added_at),
        )
        return True
    if existing["dismissed_at"] is not None and source == "manual":
        conn.execute("UPDATE tracked SET dismissed_at = NULL WHERE id = ?", (pr_id,))
    return False


def remove_tracked(conn: sqlite3.Connection, pr_id: str) -> bool:
    """Stop tracking a PR entirely (cascades its comments and seen baseline)."""
    return conn.execute("DELETE FROM tracked WHERE id = ?", (pr_id,)).rowcount > 0


def dismiss_tracked(conn: sqlite3.Connection, pr_id: str, when: str | None) -> None:
    """Set (or clear, with when=None) the dismissal stamp on a tracked PR.

    Dismissing keeps the row - it only drops out of the dashboard list - so a
    PR you dismissed stays deduped against the next notification re-seed.
    """
    conn.execute("UPDATE tracked SET dismissed_at = ? WHERE id = ?", (when, pr_id))


def update_tracked_state(conn: sqlite3.Connection, pr_id: str, row: dict) -> None:
    """Write the freshly-fetched GitHub state onto a tracked row, leaving the
    tracking metadata (source, added_at, dismissed_at) untouched."""
    sets = ", ".join(f"{c} = :{c}" for c in TRACKED_STATE_COLS if c in row)
    if not sets:
        return
    conn.execute(f"UPDATE tracked SET {sets} WHERE id = :id", {**row, "id": pr_id})


def get_tracked(conn: sqlite3.Connection, pr_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tracked WHERE id = ?", (pr_id,)).fetchone()


def list_tracked(conn: sqlite3.Connection, *,
                 include_dismissed: bool = False) -> list[sqlite3.Row]:
    where = "" if include_dismissed else " WHERE dismissed_at IS NULL"
    return conn.execute(
        f"SELECT * FROM tracked{where} ORDER BY updated_at DESC, number DESC"
    ).fetchall()


_TRACKED_COMMENT_COLS = ["comment_id", "kind", "thread_id", "parent_id", "author",
                         "created_at", "body", "path", "state", "url"]


def replace_tracked_comments(conn: sqlite3.Connection, pr_id: str,
                             comments: list[dict]) -> None:
    conn.execute("DELETE FROM tracked_comment WHERE pr_id = ?", (pr_id,))
    conn.executemany(
        f"INSERT INTO tracked_comment (pr_id, {', '.join(_TRACKED_COMMENT_COLS)}) "
        f"VALUES (?, {', '.join('?' for _ in _TRACKED_COMMENT_COLS)})",
        [(pr_id, *(c.get(col) for col in _TRACKED_COMMENT_COLS)) for c in comments],
    )


def list_tracked_comments(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute(
        f"SELECT pr_id, {', '.join(_TRACKED_COMMENT_COLS)} "
        "FROM tracked_comment ORDER BY pr_id, created_at"
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["pr_id"], []).append(dict(r))
    return out


def list_tracked_seen(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["pr_id"]: r for r in conn.execute("SELECT * FROM tracked_seen").fetchall()}


def upsert_tracked_seen(conn: sqlite3.Connection, pr_id: str, state: str | None,
                        head_sha: str | None, activity_count: int | None,
                        seen_at: str) -> None:
    _upsert(conn, "tracked_seen", {
        "pr_id": pr_id,
        "state": state,
        "head_sha": head_sha,
        "activity_count": activity_count,
        "seen_at": seen_at,
    }, ["pr_id"])


def comments_for(conn: sqlite3.Connection, pr_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT pr_id, kind, thread_id, comment_id, author, created_at, body, path, state, url "
        "FROM pr_comment WHERE pr_id = ? ORDER BY created_at",
        (pr_id,),
    ).fetchall()
    return [dict(r) for r in rows]
