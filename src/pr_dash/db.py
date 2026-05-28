from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 6

SCHEMA_SQL = """
CREATE TABLE pr (
  id                   TEXT PRIMARY KEY,
  repo                 TEXT NOT NULL,
  number               INTEGER NOT NULL,
  title                TEXT NOT NULL,
  url                  TEXT NOT NULL,
  author               TEXT NOT NULL,
  target_branch        TEXT NOT NULL,
  head_branch          TEXT NOT NULL,
  head_sha             TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  review_requested_at  TEXT NOT NULL,
  previously_reviewed  INTEGER NOT NULL,
  mergeable            TEXT,
  ci_state             TEXT,
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
  head_sha         TEXT PRIMARY KEY,
  sibling_head_sha TEXT NOT NULL DEFAULT '',
  summary          TEXT NOT NULL,
  concerns         TEXT NOT NULL DEFAULT '[]',
  verdict          TEXT NOT NULL,
  computed_at      TEXT NOT NULL
);

CREATE INDEX idx_pr_module_pr ON pr_module(pr_id);
CREATE INDEX idx_pr_reviewer_pr ON pr_reviewer(pr_id);
CREATE INDEX idx_pr_thread_pr ON pr_thread(pr_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
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


def get_cached_pr(conn: sqlite3.Connection, pr_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pr WHERE id = ?", (pr_id,)).fetchone()


def upsert_pr(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
    sql = (
        f"INSERT INTO pr ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}"
    )
    conn.execute(sql, row)


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


def upsert_complexity(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "head_sha")
    sql = (
        f"INSERT INTO complexity ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(head_sha) DO UPDATE SET {updates}"
    )
    conn.execute(sql, row)


def get_complexity(conn: sqlite3.Connection, head_sha: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM complexity WHERE head_sha = ?", (head_sha,)).fetchone()


def upsert_diff(conn: sqlite3.Connection, head_sha: str, patch_text: str | None,
                truncated: bool, fetched_at: str) -> None:
    conn.execute(
        "INSERT INTO pr_diff (head_sha, patch_text, truncated, fetched_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(head_sha) DO UPDATE SET "
        "patch_text = excluded.patch_text, truncated = excluded.truncated, "
        "fetched_at = excluded.fetched_at",
        (head_sha, patch_text, int(truncated), fetched_at),
    )


def get_diff(conn: sqlite3.Connection, head_sha: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pr_diff WHERE head_sha = ?", (head_sha,)).fetchone()


def upsert_ai_review(conn: sqlite3.Connection, head_sha: str, sibling_head_sha: str,
                     summary: str, concerns_json: str, verdict: str,
                     computed_at: str) -> None:
    conn.execute(
        "INSERT INTO ai_review (head_sha, sibling_head_sha, summary, concerns, "
        "verdict, computed_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(head_sha) DO UPDATE SET "
        "sibling_head_sha = excluded.sibling_head_sha, "
        "summary = excluded.summary, concerns = excluded.concerns, "
        "verdict = excluded.verdict, computed_at = excluded.computed_at",
        (head_sha, sibling_head_sha, summary, concerns_json, verdict, computed_at),
    )


def get_ai_review(conn: sqlite3.Connection, head_sha: str,
                  sibling_head_sha: str = "") -> sqlite3.Row | None:
    """Return cached review only when the sibling context matches.
    Paired PRs reviewed before pairing don't hit cache - they re-review with context.
    """
    return conn.execute(
        "SELECT * FROM ai_review WHERE head_sha = ? AND sibling_head_sha = ?",
        (head_sha, sibling_head_sha),
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
