import sqlite3
from pathlib import Path

from pr_dash import db


def _conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "t.db")


def _insert(conn, pr_id, *, reviewed, archived=None):
    repo, number = pr_id.split("#")
    conn.execute(
        "INSERT INTO pr (id, repo, number, title, url, author, target_branch, "
        "head_branch, head_sha, created_at, updated_at, review_requested_at, "
        "previously_reviewed, additions, deletions, changed_files, archived_at, "
        "fetched_at) VALUES (?, ?, ?, '', '', 'auth', 'b', 'b', 'sha', 't', 't', "
        "'t', ?, 0, 0, 0, ?, 't')",
        (pr_id, repo, int(number), reviewed, archived),
    )


def _ids(conn):
    return {r["id"] for r in conn.execute("SELECT id FROM pr").fetchall()}


def test_sweep_archives_reviewed_deletes_unreviewed(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=1)   # fell out, reviewed -> archive
    _insert(conn, "odoo/odoo#2", reviewed=0)   # fell out, never reviewed -> delete
    _insert(conn, "odoo/odoo#3", reviewed=1)   # still active -> untouched

    archived, deleted = db.sweep(conn, {"odoo/odoo#3"}, "2026-05-26T00:00:00+00:00")

    assert (archived, deleted) == (1, 1)
    assert _ids(conn) == {"odoo/odoo#1", "odoo/odoo#3"}
    row = conn.execute("SELECT archived_at FROM pr WHERE id = 'odoo/odoo#1'").fetchone()
    assert row["archived_at"] == "2026-05-26T00:00:00+00:00"


def test_sweep_no_delete_when_reconciliation_failed(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=1)
    _insert(conn, "odoo/odoo#2", reviewed=0)

    archived, deleted = db.sweep(conn, set(), "now", delete=False)

    assert (archived, deleted) == (1, 0)
    assert _ids(conn) == {"odoo/odoo#1", "odoo/odoo#2"}  # unreviewed survives


def test_sweep_never_re_archives(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=1, archived="2026-01-01T00:00:00+00:00")

    archived, deleted = db.sweep(conn, set(), "2026-05-26T00:00:00+00:00")

    assert archived == 0  # already archived, timestamp preserved
    row = conn.execute("SELECT archived_at FROM pr WHERE id = 'odoo/odoo#1'").fetchone()
    assert row["archived_at"] == "2026-01-01T00:00:00+00:00"


def test_delete_candidates_excludes_kept_and_archived(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=0)                                  # candidate
    _insert(conn, "odoo/odoo#2", reviewed=0)                                  # kept -> excluded
    _insert(conn, "odoo/odoo#3", reviewed=1)                                  # reviewed -> excluded
    _insert(conn, "odoo/odoo#4", reviewed=0, archived="2026-01-01T00:00:00")  # archived -> excluded

    cands = db.delete_candidates(conn, {"odoo/odoo#2"})

    assert {r["id"] for r in cands} == {"odoo/odoo#1"}


def test_review_snapshot_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=1)
    assert db.get_review_snapshot(conn, "odoo/odoo#1") is None

    db.upsert_review_snapshot(conn, "odoo/odoo#1", "sha_a", '{"x.py": "h1"}', "t1")
    row = db.get_review_snapshot(conn, "odoo/odoo#1")
    assert row["reviewed_sha"] == "sha_a" and row["signatures"] == '{"x.py": "h1"}'

    # upsert overwrites in place (new review on a new head)
    db.upsert_review_snapshot(conn, "odoo/odoo#1", "sha_b", '{"x.py": "h2"}', "t2")
    row = db.get_review_snapshot(conn, "odoo/odoo#1")
    assert row["reviewed_sha"] == "sha_b" and row["signatures"] == '{"x.py": "h2"}'


def _states(conn, pr_id):
    return {(r["kind"], r["name"]): r["state"]
            for r in conn.execute(
                "SELECT kind, name, state FROM pr_reviewer WHERE pr_id = ?", (pr_id,))}


def test_set_my_review_state_inserts_then_updates(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=1, archived="t")

    db.set_my_review_state(conn, "odoo/odoo#1", "andg-odoo", "APPROVED")
    assert _states(conn, "odoo/odoo#1") == {("user", "andg-odoo"): "APPROVED"}

    # idempotent upsert: a later decision overwrites, no duplicate row
    db.set_my_review_state(conn, "odoo/odoo#1", "andg-odoo", "CHANGES_REQUESTED")
    assert _states(conn, "odoo/odoo#1") == {("user", "andg-odoo"): "CHANGES_REQUESTED"}


def test_set_my_review_state_leaves_other_reviewers(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=1, archived="t")
    db.replace_reviewers(conn, "odoo/odoo#1", [
        {"kind": "user", "name": "someone", "state": "COMMENTED"},
        {"kind": "team", "name": "rd-accounting", "state": "PENDING"},
    ])

    db.set_my_review_state(conn, "odoo/odoo#1", "andg-odoo", "APPROVED")

    assert _states(conn, "odoo/odoo#1") == {
        ("user", "someone"): "COMMENTED",
        ("team", "rd-accounting"): "PENDING",
        ("user", "andg-odoo"): "APPROVED",
    }


def test_mark_reviewed_then_sweep_archives(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=0)  # approved-but-stale: cache says unreviewed

    db.mark_reviewed(conn, {"odoo/odoo#1"})
    archived, deleted = db.sweep(conn, set(), "2026-05-26T00:00:00+00:00")

    assert (archived, deleted) == (1, 0)
    assert _ids(conn) == {"odoo/odoo#1"}  # rescued from deletion


def test_state_defaults_open_and_migrates(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=1)
    row = conn.execute("SELECT state FROM pr WHERE id = 'odoo/odoo#1'").fetchone()
    assert row["state"] == "OPEN"

    db.set_pr_state(conn, "odoo/odoo#1", "MERGED")
    row = conn.execute("SELECT state FROM pr WHERE id = 'odoo/odoo#1'").fetchone()
    assert row["state"] == "MERGED"


def test_migration_adds_state_to_v10_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    old_schema = db.SCHEMA_SQL.replace(
        "  state                TEXT NOT NULL DEFAULT 'OPEN',\n", "")
    conn.executescript(old_schema)
    conn.execute("PRAGMA user_version = 10")
    conn.close()

    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
    assert "state" in cols
