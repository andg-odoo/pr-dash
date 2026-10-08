"""Snapshots of runbot builds and bundles, shared by the refresh and get_runbot."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pr_dash import db, derive, query, runbot, tab

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("pr_dash")


# Failed fetches of a tree wait 1, 2 then 4 Mine ticks, and the fourth one is final.
RETRY_AFTER = timedelta(minutes=15)
RETRY_LIMIT = 4
# A snapshot no Mine check names any more is dropped once it is this old.
KEEP_UNREFERENCED = timedelta(days=3)


def refresh(conn, http: runbot.Http, find_session: Callable[[], str],
            clock: Callable[[], datetime], warnings: list[str]) -> int:
    """Snapshot the stale red builds of every open Authored PR, returning the requests made."""
    rows = db.list_mine(conn, include_dismissed=True)
    db.prune_runbot_snapshots(
        conn, {f"build:{ids[1]}" for r in rows for c in r["checks"]
               if (ids := runbot.parse_check_url(c["url"] or ""))},
        (clock() - KEEP_UNREFERENCED).isoformat(timespec="seconds"))
    snapshots = db.get_runbot_snapshots(conn)
    session = _session(conn, find_session)
    stale = _stale_builds(rows, db.list_mine_mergebot(conn), snapshots, clock())
    if not stale:
        return 0
    client = _runbot_client(conn, http, session, clock)
    _snapshot_builds(conn, client, stale, snapshots, warnings,
                     clock().isoformat(timespec="seconds"))
    return client.requests if client else 0


def _stale_builds(rows: list[dict], pages: dict[str, dict], snapshots: dict[str, dict],
                  now: datetime) -> dict[str, int]:
    """Subject to build id of the failing runbot checks of open rows left to fetch, retries due."""
    def due(snap: dict) -> bool:
        wait = RETRY_AFTER * 2 ** (snap["attempts"] - 1) if snap["attempts"] else timedelta()
        return snap["retry"] and derive.parse_iso(snap["fetched_at"]) + wait <= now

    stale = {}
    for row in rows:
        states = derive.check_states(row, pages.get(row["id"]))
        for c in row["checks"]:
            ids = runbot.parse_check_url(c["url"] or "")
            subject = ids and f"build:{ids[1]}"
            if (ids and row["state"] == "OPEN" and states.get(c["name"]) == "failure"
                    and (subject not in snapshots or due(snapshots[subject]))):
                stale[subject] = ids[1]
    return stale


def _session(conn, find_session: Callable[[], str]) -> str:
    """The current runbot cookie, "" when none, ending the expired state once it changed."""
    try:
        session = find_session()
    except runbot.SessionExpired:
        session = ""
    rejected = db.get_meta(conn, "runbot_session_expired")
    if rejected is not None and rejected != _digest(session):
        db.delete_meta(conn, "runbot_session_expired")
    return session


def _digest(session: str) -> str:
    # Only the digest of a rejected cookie is kept, a fetch waits for a different one.
    return hashlib.sha256(session.encode()).hexdigest() if session else ""


def _runbot_client(conn, http: runbot.Http, session: str,
                   clock: Callable[[], datetime]) -> runbot.Runbot | None:
    """A client on `session`, None when there is none or it is the one runbot rejected."""
    if not session or db.get_meta(conn, "runbot_session_expired") == _digest(session):
        db.set_meta(conn, "runbot_session_expired", _digest(session))
        return None
    return runbot.Runbot(conn, http, session, clock)


def _snapshot_builds(conn, client: runbot.Runbot | None, stale: dict[str, int],
                     snapshots: dict[str, dict], warnings: list[str], now: str) -> None:
    """Fetch and store each stale build, recording why the ones left over were not read."""
    def kept(subject: str) -> list[dict]:
        return snapshots[subject]["triggers"] if subject in snapshots else []

    def settled(subject: str) -> dict[int, list[dict]]:
        [old] = kept(subject) or [{"settled": [], "failures": []}]
        return {b: [f for f in old["failures"] if f["build_id"] == b] for b in old["settled"]}

    # Per subject: (triggers, error, retry, attempts), the ones left out end with `stop`.
    outcomes: dict[str, tuple[list[dict], str | None, bool, int]] = {}
    stop = "expired"
    for subject, build_id in stale.items() if client else ():
        try:
            triggers = client.triggers([build_id], settled(subject))
        except runbot.SessionExpired:
            db.set_meta(conn, "runbot_session_expired", _digest(client.session_id))
            break
        except runbot.CapReached:
            stop = "deferred"
            break
        except Exception as e:
            # Never aborts the refresh, a network failure is retried a few times, the rest is final.
            log.exception("runbot fetch of %s failed", subject)
            warnings.append(f"Runbot fetch of {subject} failed: {e}")
            attempts = snapshots.get(subject, {}).get("attempts", 0) + 1
            transient = isinstance(e, runbot.HttpError) and attempts < RETRY_LIMIT
            outcomes[subject] = kept(subject) if transient else [], str(e), transient, attempts
            continue
        db.delete_meta(conn, "runbot_session_expired")
        if not triggers:
            outcomes[subject] = [], "build not found", False, 0
            continue
        [trigger] = triggers
        unfinished = trigger.verdict == "running" or trigger.children["testing"] > 0
        outcomes[subject] = [asdict(trigger)], None, unfinished, 0
    with db.transaction(conn):
        for subject in stale:
            triggers, error, retry, attempts = outcomes.get(subject, (kept(subject), stop, True, 0))
            db.upsert_runbot_snapshot(conn, subject, "", now, triggers, error, retry=retry,
                                      attempts=attempts)


def get_runbot(cfg, ref: str, *, http: runbot.Http | None = None,
               find_session: Callable[[], str] = runbot.find_session_id,
               clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> dict:
    """The runbot state of the Branch set holding Authored PR `ref`, else of bundle `ref`."""
    try:
        query._parse_ref(ref)
    except ValueError:
        branch_set = None
    else:
        # A PR ref names an Authored PR or misses with the view holding it, never a bundle.
        branch_set = tab.only(cfg, ref, "get_runbot", tab.MINE)
    conn = db.connect(cfg.db_path)
    try:
        http = http or runbot.UrllibHttp()
        if branch_set is not None:
            return _branch_set_runbot(conn, branch_set, http, find_session, clock)
        return _bundle_runbot(conn, cfg, ref, http, find_session, clock)
    finally:
        conn.close()


def _branch_set_runbot(conn, branch_set: dict, http: runbot.Http,
                       find_session: Callable[[], str], clock: Callable[[], datetime]) -> dict:
    """Each runbot batch of the set's open PRs, its red builds once each, stale ones refetched."""
    ids = {f"{pr['repo']}#{pr['number']}" for pr in tab.mine_prs(branch_set)}
    rows = [r for r in db.list_mine(conn, include_dismissed=True) if r["id"] in ids]
    pages, snapshots = db.list_mine_mergebot(conn), db.get_runbot_snapshots(conn)
    requests = 0
    session = _session(conn, find_session)
    if stale := _stale_builds(rows, pages, snapshots, clock()):
        client = _runbot_client(conn, http, session, clock)
        _snapshot_builds(conn, client, stale, snapshots, [],
                         clock().isoformat(timespec="seconds"))
        requests = client.requests if client else 0
        snapshots = db.get_runbot_snapshots(conn)
    return {"ref": branch_set["key"], "requests": requests,
            "session_expired": db.get_meta(conn, "runbot_session_expired") is not None,
            "batches": derive.runbot_batches(rows, pages, snapshots)}


def _bundle_runbot(conn, cfg, name: str, http: runbot.Http, find_session: Callable[[], str],
                   clock: Callable[[], datetime]) -> dict:
    """The latest batch of bundle `name`, from its snapshot while that is finished and recent."""
    subject = f"bundle:{name}"
    snap = db.get_runbot_snapshots(conn).get(subject)
    final = snap is not None and not snap["retry"]
    recent = clock() - timedelta(minutes=cfg.thresholds.mine_staleness_minutes)
    if final and derive.parse_iso(snap["fetched_at"]) > recent:
        return _bundle_payload(conn, name, snap, 0)
    client = _runbot_client(conn, http, _session(conn, find_session), clock)
    if client is None:
        return _bundle_payload(conn, name, snap, 0, "expired")
    try:
        walk = client.bundle(name, known=snap["key"] if final else None)
    except runbot.NoBundle as e:
        return {"ref": name, "requests": client.requests, "error": str(e),
                "candidates": e.candidates, "count": e.count}
    except runbot.SessionExpired:
        db.set_meta(conn, "runbot_session_expired", _digest(client.session_id))
        return _bundle_payload(conn, name, snap, client.requests, "expired")
    except runbot.CapReached:
        return _bundle_payload(conn, name, snap, client.requests, "deferred")
    except Exception as e:
        log.exception("runbot walk of %s failed", name)
        return _bundle_payload(conn, name, snap, client.requests, str(e))
    db.delete_meta(conn, "runbot_session_expired")
    if walk is None:
        key, triggers, retry = snap["key"], snap["triggers"], False
    else:
        key, triggers = walk.key, [asdict(t) for t in walk.triggers]
        retry = any(t.verdict == "running" or t.children["testing"] for t in walk.triggers)
    db.upsert_runbot_snapshot(conn, subject, key, clock().isoformat(timespec="seconds"),
                              triggers, None, retry=retry)
    return _bundle_payload(conn, name, db.get_runbot_snapshots(conn)[subject], client.requests)


def _bundle_payload(conn, name: str, snap: dict | None, requests: int,
                    error: str | None = None) -> dict:
    """A bundle snapshot in the Branch set shape, `error` saying why it was not refreshed."""
    batches = []
    if snap is not None:
        key = json.loads(snap["key"])
        triggers = [{**t, "error": None, "fetched_at": snap["fetched_at"], "prs": []}
                    for t in snap["triggers"]]
        batches = [{"batch_id": key["batch_id"], "bundle": key["bundle"], "prs": [],
                    "triggers": triggers}]
    return {"ref": name, "requests": requests, "error": error,
            "session_expired": db.get_meta(conn, "runbot_session_expired") is not None,
            "batches": batches}
