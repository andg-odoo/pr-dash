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


def _comment(kind, comment_id, *, thread_id=None, author="a", created_at="t",
             body="b", path=None, state=None, url=None):
    return {"kind": kind, "thread_id": thread_id, "comment_id": comment_id,
            "author": author, "created_at": created_at, "body": body,
            "path": path, "state": state, "url": url}


def test_replace_comments_roundtrip_and_replace(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=0)
    db.replace_comments(conn, "odoo/odoo#1", [
        _comment("thread", "11", thread_id="T1", path="sale/x.py"),
        _comment("review", "R1", state="APPROVED", body="lgtm"),
        _comment("issue", "99", body="ping"),
    ])
    rows = db.comments_for(conn, "odoo/odoo#1")
    assert {r["kind"] for r in rows} == {"thread", "review", "issue"}
    assert db.list_comments(conn)["odoo/odoo#1"] == rows

    # Replace wipes the old rows (no accumulation).
    db.replace_comments(conn, "odoo/odoo#1", [_comment("issue", "42", body="only")])
    rows = db.comments_for(conn, "odoo/odoo#1")
    assert [r["comment_id"] for r in rows] == ["42"]


def test_replace_comments_scoped_to_pr(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=0)
    _insert(conn, "odoo/odoo#2", reviewed=0)
    db.replace_comments(conn, "odoo/odoo#1", [_comment("issue", "1")])
    db.replace_comments(conn, "odoo/odoo#2", [_comment("issue", "2")])
    # Replacing #1 must not touch #2.
    db.replace_comments(conn, "odoo/odoo#1", [])
    assert db.comments_for(conn, "odoo/odoo#1") == []
    assert [r["comment_id"] for r in db.comments_for(conn, "odoo/odoo#2")] == ["2"]


def test_my_pending_review_column_defaults_zero(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=0)
    row = db.get_cached_pr(conn, "odoo/odoo#1")
    assert row["my_pending_review"] == 0


def test_set_ping_roundtrip_and_clear(tmp_path):
    conn = _conn(tmp_path)
    _insert(conn, "odoo/odoo#1", reviewed=1)
    db.set_ping(conn, "odoo/odoo#1", "2026-07-02T00:00:00Z", "alice", "done, ready")
    row = db.get_cached_pr(conn, "odoo/odoo#1")
    assert row["ping_at"] == "2026-07-02T00:00:00Z"
    assert row["ping_author"] == "alice"
    assert row["ping_snippet"] == "done, ready"

    db.set_ping(conn, "odoo/odoo#1", None, None, None)
    row = db.get_cached_pr(conn, "odoo/odoo#1")
    assert row["ping_at"] is None and row["ping_author"] is None


def test_migration_adds_ping_columns_to_v12_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    old_schema = db.SCHEMA_SQL
    for line in ("  ping_at              TEXT,\n",
                 "  ping_author          TEXT,\n",
                 "  ping_snippet         TEXT,\n"):
        old_schema = old_schema.replace(line, "")
    conn.executescript(old_schema)
    conn.execute("PRAGMA user_version = 12")
    conn.close()

    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)").fetchall()}
    assert {"ping_at", "ping_author", "ping_snippet"} <= cols


def test_migration_adds_mine_tables_to_v23_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(db.SCHEMA_SQL.replace(db.MINE_SCHEMA_SQL + db.MINE_ACK_SCHEMA_SQL, ""))
    conn.execute("INSERT INTO tracked (id, repo, number, url, added_at) "
                 "VALUES ('odoo/odoo#1', 'odoo/odoo', 1, 'u', 't')")
    conn.execute("PRAGMA user_version = 23")
    conn.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.add_mine(conn, "odoo/odoo#2", "odoo/odoo", 2, "u", "t") is True
    db.upsert_tab_seen(conn, "mine", {"pr_id": "odoo/odoo#2", "state": "OPEN", "seen_at": "t"})
    db.upsert_mine_mergebot(conn, "odoo/odoo#2", {
        "state": "blocked", "r_plus": False, "merge_method": True, "checks": [],
        "linked": [], "reason": None}, "t")
    assert [r["id"] for r in db.list_mine(conn)] == ["odoo/odoo#2"]
    assert db.list_mine_mergebot(conn)["odoo/odoo#2"]["r_plus"] is False
    assert [r["id"] for r in db.list_tracked(conn)] == ["odoo/odoo#1"]


def test_migration_adds_acknowledge_and_fyi_state_to_v24_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    v24 = (db.SCHEMA_SQL.replace(db.MINE_ACK_SCHEMA_SQL, "")
           .replace("  head_committed_at TEXT,\n", "")
           .replace("  fetched_at     TEXT,\n  r_plus         INTEGER,\n", ""))
    conn.executescript(v24)
    conn.execute("INSERT INTO mine (id, repo, number, url, added_at) "
                 "VALUES ('odoo/odoo#2', 'odoo/odoo', 2, 'u', 't')")
    conn.execute("INSERT INTO mine_seen (pr_id, state, seen_at) VALUES ('odoo/odoo#2', 'OPEN', 't')")
    conn.execute("PRAGMA user_version = 24")
    conn.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    db.update_tab_state(conn, "mine", "odoo/odoo#2", {"head_committed_at": "2026-10-01"})
    db.set_mine_ack(conn, "b", "f1", "t")
    [row] = db.list_mine(conn)
    seen = db.list_tab_seen(conn, "mine")["odoo/odoo#2"]
    assert (row["head_committed_at"], seen["fetched_at"], seen["r_plus"],
            db.list_mine_acks(conn)) == ("2026-10-01", None, None, {"b": "f1"})


def test_migration_links_forward_ports_that_follow_their_source_dismissal(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(db.SCHEMA_SQL.replace(db.MINE_FW_SCHEMA_SQL, ""))
    conn.execute("INSERT INTO mine (id, repo, number, url, added_at) "
                 "VALUES ('odoo/odoo#2', 'odoo/odoo', 2, 'u', 't')")
    conn.execute("PRAGMA user_version = 25")
    conn.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    db.add_mine(conn, "odoo/odoo#3", "odoo/odoo", 3, "u", "t")
    db.link_mine_forward_port(conn, "odoo/odoo#3", "odoo/odoo#2")
    assert [(r["id"], r["source_id"]) for r in db.list_mine(conn)] == [
        ("odoo/odoo#2", None), ("odoo/odoo#3", "odoo/odoo#2")]
    db.set_dismissed(conn, "mine", "odoo/odoo#2", "t")
    assert db.list_mine(conn) == []
    assert len(db.list_mine(conn, include_dismissed=True)) == 2


def test_migration_stores_comments_once_and_refetches_a_v27_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(db.SCHEMA_SQL.replace(db.COMMENT_SCHEMA_SQL, "") + "".join(
        f"CREATE TABLE {tab}_comment (pr_id TEXT REFERENCES {tab}(id), comment_id TEXT);"
        f"INSERT INTO {tab} (id, repo, number, url, added_at, fetched_at) "
        f"VALUES ('odoo/odoo#1', 'odoo/odoo', 1, 'u', 't', 't');"
        f"INSERT INTO {tab}_comment VALUES ('odoo/odoo#1', 'c1');"
        for tab in ("tracked", "mine")))
    conn.execute("PRAGMA user_version = 27")
    conn.close()

    conn = db.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "comment" in tables and {"tracked_comment", "mine_comment"}.isdisjoint(tables)
    assert [r["fetched_at"] for r in [*db.list_tracked(conn), *db.list_mine(conn)]] == [None] * 2
