"""Read runbot build trees and the failures in their logs, under a request cap shared via the DB."""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pr_dash import db

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("pr_dash.runbot")

BASE_URL = "https://runbot.odoo.com"
RPC_TIMEOUT_SECONDS = 60
GET_TIMEOUT_SECONDS = 30
REQUESTS_PER_HOUR = 30
REQUESTS_PER_DAY = 150
BUNDLE_CANDIDATES = 20
# Bundle, partial name, batches, slots, tree and previous trees: a walk starts only if they fit.
BUNDLE_WALK_REQUESTS = 6
# Failing builds of one tree whose logs a fetch reads, so a wide red tree fits in the caps.
FAILING_LOGS_PER_TREE = 5
# A build killed past this many seconds hit runbot's step time limit (seen at ~1812s).
KILL_TIMEOUT_SECONDS = 1790
TRACEBACK_LINES = 15

_CHECK_URL_RE = re.compile(r"https://runbot\.odoo\.com/runbot/batch/(\d+)/build/(\d+)")
_LINE_START_RE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ ")
# The test's own logger line, e.g. `odoo.addons.web_studio.tests.test_ui: FAIL: TestUi.test_x`
_TEST_FAIL_RE = re.compile(
    r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ \w+ \S+ (\S+): (?:FAIL|ERROR): "
    r"(?:Subtest )?(\w+\.\w+)(?=[\s(]|$)",
)
_FINDINGS_RE = re.compile(r"Findings: (\d+)")
_EXCEPTION_RE = re.compile(r"[\w.]*(?:Error|Exception|Warning|Failure)\b")
_RUFF_PATH_RE = re.compile(r"^/data/build/(?:merge_base_)?")
_FAILED = ("ko", "killed")
# Fields of a build tree row, build_time joins them when runbot can compute it.
_TREE_FIELDS = ["trigger_id", "description", "host", "dest", "log_list", "local_state",
                "local_result", "parent_id"]
_TREE_LIMIT = 1000
_FIREFOX_QUERY = (
    "SELECT value, lastAccessed FROM moz_cookies WHERE host LIKE '%runbot.odoo.com' "
    "AND name = 'session_id' ORDER BY lastAccessed DESC LIMIT 1"
)


class RunbotError(Exception):
    pass


class SessionExpired(RunbotError):
    """No session cookie was found, or runbot rejected it."""


class CapReached(RunbotError):
    """The hourly or daily request cap is spent, nothing was sent."""


class HttpError(RunbotError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class LogGone(HttpError):
    """A worker-host file answered 404, as logs of old builds are garbage-collected."""


class NoBundle(RunbotError):
    """A bundle name matched none or several bundles, the first `candidates` of `count`."""

    def __init__(self, name: str, candidates: list[str], count: int):
        super().__init__(f"{count} runbot bundles match {name!r}: {candidates}"
                         if count else f"no runbot bundle matches {name!r}")
        self.candidates, self.count = candidates, count


class RpcError(RunbotError):
    """A JSON-RPC error other than a rejected session."""

    def __init__(self, message: str, *, name: str):
        super().__init__(message)
        self.name = name


class Http(Protocol):
    """The two kinds of request runbot gets: JSON-RPC posts and static file reads."""

    def post_json(self, url: str, body: dict, headers: dict[str, str]) -> dict: ...

    def get_text(self, url: str) -> str: ...


class UrllibHttp:
    """`Http` over urllib."""

    def post_json(self, url, body, headers):
        req = urllib.request.Request(
            url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers},
        )
        text = _open(req, url, RPC_TIMEOUT_SECONDS)
        try:
            return json.loads(text)
        except ValueError as e:
            raise HttpError(f"{url} answered non-JSON: {text[:80]!r}") from e

    def get_text(self, url):
        try:
            return _open(url, url, GET_TIMEOUT_SECONDS)
        except HttpError as e:
            if e.status == 404:
                raise LogGone(f"{url} is gone", status=404) from e
            raise


def _open(req: urllib.request.Request | str, url: str, timeout: int) -> str:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HttpError(f"HTTP {e.code} from {url}", status=e.code) from e
    except (OSError, http.client.HTTPException) as e:
        raise HttpError(f"fetch {url} failed: {e}") from e


@dataclass
class Trigger:
    """One runbot check's build tree, reduced to what the Runbot tab shows."""

    name: str
    build_id: int
    url: str
    verdict: str  # red, green or running, over the build and all its descendants
    children: dict[str, int]  # done, ok, ko, killed, testing
    failures: list[dict]
    previous: str | None = None  # red or green the run before, on red ones only
    # Failing builds already done when read, whose failures a later read of the tree reuses.
    settled: list[int] = field(default_factory=list)


@dataclass
class BundleBatch:
    """The latest visible batch of a bundle asked by name, with one Trigger per slot."""

    bundle: str
    batch_id: int
    key: str  # the batch id and its slot builds, equal until a push or a rebuild
    triggers: list[Trigger]


def build_url(build_id: int) -> str:
    """Link to a build page, for people only: scripts get a Cloudflare challenge there."""
    return f"{BASE_URL}/runbot/build/{build_id}"


def parse_check_url(url: str) -> tuple[int, int] | None:
    """Return (batch id, build id) of a GitHub check's runbot target URL."""
    m = _CHECK_URL_RE.fullmatch(url)
    return (int(m.group(1)), int(m.group(2))) if m else None


def find_session_id() -> str:
    """Return the runbot session cookie from the env, the runbot-status file or Firefox."""
    if value := os.environ.get("RUNBOT_SESSION_ID"):
        return value
    path = Path.home() / ".config/runbot-status/session_id"
    if path.exists() and (value := path.read_text().strip()):
        return value
    found = [
        cookie for cookies_db in Path.home().glob(".mozilla/firefox/*/cookies.sqlite")
        if (cookie := _firefox_cookie(cookies_db))
    ]
    if not found:
        msg = "no runbot session cookie found: log in to runbot.odoo.com in Firefox"
        raise SessionExpired(msg)
    return max(found)[1]


def _firefox_cookie(cookies_db: Path) -> tuple[int, str] | None:
    """Return (lastAccessed, value) of the newest runbot cookie in one profile's cookie db."""
    # Firefox holds the db open and keeps recent writes in the -wal, so read a copy of all three.
    with tempfile.TemporaryDirectory() as tmp:
        try:
            for suffix in ("", "-wal", "-shm"):
                src = cookies_db.with_name(cookies_db.name + suffix)
                if not suffix or src.exists():
                    shutil.copyfile(src, Path(tmp) / src.name)
            conn = sqlite3.connect(Path(tmp) / cookies_db.name)
            try:
                row = conn.execute(_FIREFOX_QUERY).fetchone()
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as e:
            log.warning("skipping Firefox cookies %s: %s", cookies_db, e)
            return None
    return (row[1], row[0]) if row else None


class Runbot:
    """Read-only runbot client: one search_read per build forest, then one GET per failing log."""

    def __init__(self, conn: sqlite3.Connection, http: Http, session_id: str,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.conn, self.http, self.session_id, self.clock = conn, http, session_id, clock
        self.requests = 0

    def _claim(self) -> None:
        if not db.claim_runbot_request(self.conn, self.clock(), REQUESTS_PER_HOUR,
                                       REQUESTS_PER_DAY):
            msg = f"runbot cap of {REQUESTS_PER_HOUR}/hour or {REQUESTS_PER_DAY}/day reached"
            raise CapReached(msg)
        self.requests += 1

    def requests_left(self) -> int:
        return db.runbot_requests_left(self.conn, self.clock(), REQUESTS_PER_HOUR, REQUESTS_PER_DAY)

    def search_read(self, model: str, domain: list, fields: list[str], *, limit: int,
                    order: str = "id", newest: bool = False) -> list[dict]:
        """Run one search_read, raising when it fills `limit` unless only the `newest` are asked."""
        self._claim()
        body = {"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": "search_read", "args": [],
            "kwargs": {"domain": domain, "fields": fields, "limit": limit, "order": order},
        }}
        data = self.http.post_json(f"{BASE_URL}/web/dataset/call_kw", body,
                                   {"Cookie": f"session_id={self.session_id}"})
        if error := data.get("error"):
            name = (error.get("data") or {}).get("name") or ""
            message = (error.get("data") or {}).get("message") or error.get("message")
            if "SessionExpired" in name or "AccessDenied" in name:
                msg = f"runbot rejected the session ({name}): log in to runbot.odoo.com in Firefox"
                raise SessionExpired(msg)
            raise RpcError(f"{model} search_read failed: {name}: {message}", name=name)
        rows = data["result"]
        if len(rows) >= limit and not newest:
            raise RunbotError(f"{model} search_read hit its limit of {limit}, results truncated")
        return rows

    def _get(self, url: str) -> str:
        self._claim()
        return self.http.get_text(url)

    def triggers(self, build_ids: list[int], known: dict[int, list[dict]] | None = None,
                 ) -> list[Trigger]:
        """Read the trees of `build_ids` in one search_read and the logs of new failing builds.

        :param known: failures of failing builds a previous read settled, reused without a request
        """
        return [self._trigger(root, tree, known or {}) for root, tree in self._trees(build_ids)]

    def bundle(self, name: str, known: str | None = None) -> BundleBatch | None:
        """Read the latest batch of the one bundle `name` matches, None while its key is `known`."""
        if self.requests_left() < BUNDLE_WALK_REQUESTS:
            msg = f"a bundle walk needs {BUNDLE_WALK_REQUESTS} requests, the cap leaves fewer"
            raise CapReached(msg)
        fields = ["name"]
        rows = (self.search_read("runbot.bundle", [("name", "=", name)], fields, limit=10)
                or self.search_read("runbot.bundle", [("name", "ilike", name)], fields,
                                    limit=1000, order="name"))
        if len(rows) != 1:
            raise NoBundle(name, [r["name"] for r in rows[:BUNDLE_CANDIDATES]], len(rows))
        batches = [
            b["id"] for b in self.search_read(
                "runbot.batch", [("bundle_id", "=", rows[0]["id"]), ("hidden", "=", False)],
                ["category_id"], limit=10, order="id desc", newest=True)
            # Nightly and weekly batches sit in other categories, the pushes are in Default.
            if b["category_id"] and b["category_id"][1] == "Default"
        ][:2]
        if not batches:
            raise RunbotError(f"runbot bundle {rows[0]['name']!r} has no visible batch")
        slots = [s for s in self.search_read(
            "runbot.batch.slot", [("batch_id", "in", batches), ("active", "=", True)],
            ["batch_id", "trigger_id", "build_id"], limit=1000) if s["build_id"]]
        roots = {s["trigger_id"][1]: s["build_id"][0]
                 for s in slots if s["batch_id"][0] != batches[0]}
        latest = [s["build_id"][0] for s in slots if s["batch_id"][0] == batches[0]]
        key = json.dumps({"bundle": rows[0]["name"], "batch_id": batches[0],
                          "builds": sorted(latest)})
        if key == known:
            return None
        triggers = [self._trigger(root, tree, {}) for root, tree in self._trees(latest)]
        red = [t for t in triggers if t.verdict == "red" and t.name in roots]
        if red:
            verdicts = {root["id"]: _verdict(tree)
                        for root, tree in self._trees(sorted({roots[t.name] for t in red}))}
            for t in red:
                if (verdict := verdicts.get(roots[t.name])) in ("red", "green"):
                    t.previous = verdict
        return BundleBatch(rows[0]["name"], batches[0], key, triggers)

    def _trees(self, build_ids: list[int]) -> list[tuple[dict, list[dict]]]:
        """Read `build_ids` and all their descendants in one search_read, as (root, tree) pairs."""
        domain = [("id", "child_of", build_ids)]
        try:
            rows = self.search_read("runbot.build", domain, [*_TREE_FIELDS, "build_time"],
                                    limit=_TREE_LIMIT)
        except RpcError as e:
            # runbot crashes comparing a datetime to False in build_time on some builds.
            if "TypeError" not in e.name:
                raise
            log.info("retrying without build_time: %s", e)
            rows = self.search_read("runbot.build", domain, _TREE_FIELDS, limit=_TREE_LIMIT)
        by_id = {r["id"]: r for r in rows}
        trees: dict[int, list[dict]] = {i: [] for i in build_ids if i in by_id}
        if missing := [i for i in build_ids if i not in by_id]:
            log.warning("runbot builds not found: %s", missing)
        for row in rows:
            node = row
            while node["id"] not in trees:
                node = by_id[node["parent_id"][0]]
            trees[node["id"]].append(row)
        return [(by_id[root_id], tree) for root_id, tree in trees.items()]

    def _trigger(self, root: dict, tree: list[dict], known: dict[int, list[dict]]) -> Trigger:
        kids = [b for b in tree if b is not root]
        done = [b for b in kids if b["local_state"] == "done"]
        failures, settled = self._tree_failures(tree, known)
        return Trigger(
            name=root["trigger_id"][1],
            build_id=root["id"],
            url=build_url(root["id"]),
            verdict=_verdict(tree),
            children={
                "done": len(done),
                "ok": sum(b["local_result"] in ("ok", "warn") for b in done),
                "ko": sum(b["local_result"] == "ko" for b in kids),
                "killed": sum(b["local_result"] == "killed" for b in kids),
                "testing": len(kids) - len(done),
            },
            failures=failures,
            settled=settled,
        )

    def _tree_failures(self, tree: list[dict], known: dict[int, list[dict]],
                       ) -> tuple[list[dict], list[int]]:
        """Return the tree's failures and the failing builds settled, done and fully read."""
        failures, settled, logged = [], [], 0
        for build in tree:
            if build["local_result"] not in _FAILED:
                continue
            done = build["local_state"] == "done"
            if done and build["id"] in known:
                failures += known[build["id"]]
                settled.append(build["id"])
                continue
            if build["local_result"] == "ko" and build["log_list"]:
                logged += 1
                if logged > FAILING_LOGS_PER_TREE:
                    failures.append({"log": "not fetched", **_where(build)})
                    continue
            found = self._failures(build)
            failures += found
            if done and not any(f.get("log") == "fetch failed" for f in found):
                settled.append(build["id"])
        return failures, settled

    def _failures(self, build: dict) -> list[dict]:
        where = _where(build)
        steps = build["log_list"].split(",") if build["log_list"] else []
        if build["local_result"] == "killed":
            build_time = build.get("build_time")
            timeout = (build_time or 0) >= KILL_TIMEOUT_SECONDS
            return [{"killed": "timeout" if timeout else None, "step": steps[-1] if steps else None,
                     "build_time": build_time, **where}]
        checks = [s for s in steps if s.startswith("check_") or s == "stable_policy_check"]
        static = f"http://{build['host']}/runbot/static/build/{build['dest']}/logs"
        found: list[dict] = []
        try:
            if steps and not checks:
                found = _test_failures(self._get(f"{static}/{steps[-1]}.txt"))
            for step in checks:
                if step == "check_style_ruff":
                    output = self._get(f"{static}/{step}-ruff-output.json")
                    base = self._get(f"{static}/merge_base_{step}-ruff-output.json")
                    found += _ruff_findings(output, base)
                elif (m := _FINDINGS_RE.search(self._get(f"{static}/{step}.txt"))) is None:
                    found.append({"check": step, "count": None})
                elif m.group(1) != "0":
                    found.append({"check": step, "count": int(m.group(1))})
        except LogGone:
            return [{"log": "gone", **where}]
        except HttpError as e:
            return [{"log": "fetch failed", "status": e.status, **where}]
        return [{**f, **where} for f in found] or [{"log": "unparsed", **where}]


def _where(build: dict) -> dict:
    # A child's description ("Test at install") tells it apart, its trigger is the root's.
    return {"build_id": build["id"], "build_name": build["description"] or build["trigger_id"][1],
            "url": build_url(build["id"])}


def _verdict(tree: list[dict]) -> str:
    if any(b["local_result"] in _FAILED for b in tree):
        return "red"
    if all(b["local_state"] == "done" and b["local_result"] in ("ok", "warn") for b in tree):
        return "green"
    return "running"


def _test_failures(text: str) -> list[dict]:
    """Return each failing test once with its summary and trimmed traceback, from a whole log."""
    failures: dict[tuple[str, str], list[str]] = {}
    current = None
    for line in text.splitlines():
        if m := _TEST_FAIL_RE.match(line):
            logger, test = m.groups()
            module = logger.split(".")[2] if logger.startswith("odoo.addons.") else logger
            key = (module, test)
            # A retried test or a second subtest repeats the marker, its first traceback is kept.
            current = None if key in failures else failures.setdefault(key, [])
        elif _LINE_START_RE.match(line):
            current = None
        elif current is not None:
            current.append(line)
    out = []
    for (module, test), block in failures.items():
        lines = [line for line in block if line.strip()]
        frames = []
        for i, line in enumerate(lines):
            if line.startswith("  File ") and f"/{module}/tests/" in line:
                frames.append(line)
                if i + 1 < len(lines) and lines[i + 1].startswith("    "):
                    frames.append(lines[i + 1])
        end = len(lines)
        while end and not lines[end - 1].startswith(" "):
            end -= 1
        kept = (frames + lines[end:])[:TRACEBACK_LINES] if frames else lines[-TRACEBACK_LINES:]
        tour = next((line[line.index("FAILED: ["):] for line in lines if "FAILED: [" in line), None)
        exceptions = [line for line in lines if _EXCEPTION_RE.match(line)]
        summary = tour or next(reversed(exceptions), None)
        out.append({"test": f"{module}: {test}", "summary": summary, "traceback": "\n".join(kept)})
    return out


def _ruff_findings(output: str, merge_base: str) -> list[dict]:
    """Return the ruff findings absent from the merge base, matched on rule, path and message."""
    def key(finding):
        return finding["code"], _RUFF_PATH_RE.sub("", finding["filename"]), finding["message"]

    base = Counter(map(key, json.loads(merge_base)))
    out = []
    for f in json.loads(output):
        k = key(f)
        if base[k]:
            base[k] -= 1
        # E902 "No such file" is a file the PR deletes, so it is not a finding.
        elif f["code"] != "E902":
            out.append({"rule": k[0], "path": k[1], "line": int(f["location"]["row"]),
                        "message": k[2]})
    return out
