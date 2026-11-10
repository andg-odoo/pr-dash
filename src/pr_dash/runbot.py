"""Read runbot build trees and their failures from runbot's API, under a request cap in the DB."""

from __future__ import annotations

import http.client
import json
import logging
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from pr_dash import db

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("pr_dash.runbot")

BASE_URL = "https://runbot.odoo.com"
API_TIMEOUT_SECONDS = 60
GET_TIMEOUT_SECONDS = 30
REQUESTS_PER_HOUR = 30
REQUESTS_PER_DAY = 150
BUNDLE_CANDIDATES = 20
# Bundle, batches and tree: a walk starts only if they fit.
BUNDLE_WALK_REQUESTS = 3
# The API answers HTTP 400 past this many ids in one call.
MAX_RECORDS = 50
TREE_DEPTH = 20
# A build killed past this many seconds hit runbot's step time limit (seen at ~1812s).
KILL_TIMEOUT_SECONDS = 1790
TRACEBACK_LINES = 15
ERROR_LINES = 3

_CHECK_URL_RE = re.compile(r"https://runbot\.odoo\.com/runbot/batch/(\d+)/build/(\d+)")
# The first line of a test's failure row, e.g. `FAIL: TestUi.test_x`
_TEST_ROW_RE = re.compile(r"(?:FAIL|ERROR): (?:Subtest )?(\w+\.\w+)(?=[\s(]|$)")
_EXCEPTION_RE = re.compile(r"[\w.]*(?:Error|Exception|Warning|Failure)\b")
_RUFF_PATH_RE = re.compile(r"^/data/build/(?:merge_base_)?")
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_MD_LINE_LINK_RE = re.compile(r"\[(.+?):(\d+)\]\((\S+?)\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(\S+?\)")
_FAILED = ("ko", "killed")


class RunbotError(Exception):
    pass


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
        super().__init__(
            f"{count} runbot bundles match {name!r}: {candidates}" if count
            else f"no runbot bundle is named {name!r}, the runbot API needs the exact name")
        self.candidates, self.count = candidates, count


class Http(Protocol):
    """The two kinds of request runbot gets: API reads and worker-host file reads."""

    def get_json(self, url: str) -> list[dict]: ...

    def get_text(self, url: str) -> str: ...


class UrllibHttp:
    """`Http` over urllib."""

    def get_json(self, url):
        text = _open(url, API_TIMEOUT_SECONDS)
        try:
            return json.loads(text)
        except ValueError as e:
            raise HttpError(f"{url} answered non-JSON: {text[:80]!r}") from e

    def get_text(self, url):
        try:
            return _open(url, GET_TIMEOUT_SECONDS)
        except HttpError as e:
            if e.status == 404:
                raise LogGone(f"{url} is gone", status=404) from e
            raise


def _open(url: str, timeout: int) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
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


class Runbot:
    """Read-only runbot client: one API read per build forest, plus ruff outputs of red checks."""

    def __init__(self, conn: sqlite3.Connection, http: Http,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.conn, self.http, self.clock = conn, http, clock
        self.requests = 0

    def _claim(self) -> None:
        if not db.claim_runbot_request(self.conn, self.clock(), REQUESTS_PER_HOUR,
                                       REQUESTS_PER_DAY):
            msg = f"runbot cap of {REQUESTS_PER_HOUR}/hour or {REQUESTS_PER_DAY}/day reached"
            raise CapReached(msg)
        self.requests += 1

    def requests_left(self) -> int:
        return db.runbot_requests_left(self.conn, self.clock(), REQUESTS_PER_HOUR, REQUESTS_PER_DAY)

    def _api(self, path: str) -> list[dict]:
        self._claim()
        return self.http.get_json(f"{BASE_URL}/runbot/api/{path}")

    def _get(self, url: str) -> str:
        self._claim()
        return self.http.get_text(url)

    def triggers(self, build_ids: list[int], known: dict[int, list[dict]] | None = None,
                 ) -> list[Trigger]:
        """Read the trees of `build_ids` with their failure rows in one API call.

        :param known: failures of failing builds a previous read settled, reused without a request
        """
        ids = list(dict.fromkeys(build_ids))
        if len(ids) > MAX_RECORDS:
            raise RunbotError(f"{len(ids)} builds asked, runbot's API reads {MAX_RECORDS} at most")
        if not ids:
            return []
        roots = self._api(f"build/{','.join(map(str, ids))}?depth={TREE_DEPTH}&logs=errors")
        if missing := sorted(set(ids) - {root["id"] for root in roots}):
            log.warning("runbot builds not found: %s", missing)
        return [self._trigger(root, known or {}) for root in roots]

    def bundle(self, name: str, known: str | None = None) -> BundleBatch | None:
        """Read the latest batch of the bundle named `name`, None while its key is `known`."""
        if self.requests_left() < BUNDLE_WALK_REQUESTS:
            msg = f"a bundle walk needs {BUNDLE_WALK_REQUESTS} requests, the cap leaves fewer"
            raise CapReached(msg)
        # Project 1 is R&D, base bundle names like `master` also exist in other projects.
        rows = self._api(f"bundle/{urllib.parse.quote(name, safe='')}?project_id=1")
        if len(rows) != 1:
            raise NoBundle(name, [r["name"] for r in rows[:BUNDLE_CANDIDATES]], len(rows))
        bundle = rows[0]
        # Nightly and weekly batches sit in other categories, the pushes are in Default.
        batches = [b["id"] for b in bundle["batches"]
                   if b["category"] == "Default" and b["state"] != "skipped"][:2]
        if not batches:
            raise RunbotError(f"runbot bundle {bundle['name']!r} has no visible batch")
        slots = {b["id"]: [s for s in b["slots"] if s["build"]]
                 for b in self._api(f"batch/{','.join(map(str, batches))}?depth=0&logs=none")}
        latest = [s["build"]["id"] for s in slots[batches[0]]]
        previous = {s["trigger"]: s["build"] for b in batches[1:] for s in slots.get(b, [])}
        key = json.dumps({"bundle": bundle["name"], "batch_id": batches[0],
                          "builds": sorted(latest)})
        if key == known:
            return None
        triggers = self.triggers(latest)
        for t in triggers:
            if t.verdict != "red" or not (before := previous.get(t.name)):
                continue
            if before["global_result"] in _FAILED:
                t.previous = "red"
            elif before["global_state"] == "done" and before["global_result"] in ("ok", "warn"):
                t.previous = "green"
        return BundleBatch(bundle["name"], batches[0], key, triggers)

    def batch(self, batch_id: int) -> list[Trigger]:
        """Read the slot builds of batch `batch_id` and their trees, reading no failure."""
        batches = self._api(f"batch/{batch_id}?depth={TREE_DEPTH}&logs=none")
        # A slot's build leaves out its trigger when it is the slot's.
        return [self._trigger({"trigger": s["trigger"], **s["build"]}, {}, read=False)
                for b in batches for s in b["slots"] if s["build"]]

    def _trigger(self, root: dict, known: dict[int, list[dict]], *, read: bool = True,
                 ) -> Trigger:
        tree = _tree(root)
        kids = tree[1:]
        done = [b for b in kids if b["local_state"] == "done"]
        failures, settled = (self._tree_failures(tree, known) if read else
                             ([_where(b) for b in tree if b["local_result"] in _FAILED], []))
        verdict = _verdict(tree)
        # runbot's own aggregate wins when the tree read missed the failing build.
        if root["global_result"] in _FAILED and verdict != "red":
            verdict = "red"
            failures = failures or [{"log": "unparsed", **_where(root)}]
        return Trigger(
            name=root["trigger"],
            build_id=root["id"],
            url=build_url(root["id"]),
            verdict=verdict,
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
        failures, settled = [], []
        for build in tree:
            if build["local_result"] not in _FAILED:
                continue
            done = build["local_state"] == "done"
            if done and build["id"] in known:
                failures += known[build["id"]]
                settled.append(build["id"])
                continue
            found = self._failures(build)
            failures += found
            if done and not any(f.get("log") == "fetch failed" for f in found):
                settled.append(build["id"])
        return failures, settled

    def _failures(self, build: dict) -> list[dict]:
        where = _where(build)
        files = {f["name"]: f["url"] for f in build["log_files"]}
        steps = list(files)
        if build["local_result"] == "killed":
            timeout = build["build_time"] >= KILL_TIMEOUT_SECONDS
            return [{"killed": "timeout" if timeout else None, "step": steps[-1] if steps else None,
                     "build_time": build["build_time"], **where}]
        checks = [s for s in steps if s.startswith("check_") or s == "stable_policy_check"]
        if not checks:
            found = _test_failures(build["logs"]) or _error_lines(build["logs"])
            return [{**f, **where} for f in found] or [{"log": "unparsed", **where}]
        found: list[dict] = []
        if "check_style_ruff" in checks:
            static = files["check_style_ruff"].rpartition("/")[0]
            try:
                output = self._get(f"{static}/check_style_ruff-ruff-output.json")
                base = self._get(f"{static}/merge_base_check_style_ruff-ruff-output.json")
            except LogGone:
                return [{"log": "gone", **where}]
            except HttpError as e:
                return [{"log": "fetch failed", "status": e.status, **where}]
            found += _ruff_findings(output, base)
        # Only the findings on changed lines are logged, one row each.
        others = [s for s in checks if s != "check_style_ruff"]
        if others:
            found += [_check_finding(", ".join(others), row) for row in build["logs"]]
        if not found:
            found = [{"check": step, "count": None} for step in others]
        return [{**f, **where} for f in found] or [{"log": "unparsed", **where}]


def _tree(root: dict) -> list[dict]:
    """Return `root` then the descendants counting in its result, each with the keys it inherits."""
    tree, todo = [], [root]
    while todo:
        build = todo.pop()
        tree.append(build)
        # A child leaves out the keys equal to its parent's, a linked build has its own orphan flag.
        todo += [{**build, **child} for child in reversed(build["children"])
                 if not child["link_orphan_result" if child["relation"] != "child"
                              else "orphan_result"]]
    return tree


def _where(build: dict) -> dict:
    # A child's description ("Test at install") tells it apart, shown without its markdown `**`.
    name = (build["description"] or build["trigger"]).replace("**", "")
    return {"build_id": build["id"], "build_name": name, "url": build_url(build["id"])}


def _verdict(tree: list[dict]) -> str:
    if any(b["local_result"] in _FAILED for b in tree):
        return "red"
    if all(b["local_state"] == "done" and b["local_result"] in ("ok", "warn") for b in tree):
        return "green"
    return "running"


def _error_lines(logs: list[dict]) -> list[dict]:
    """Return the first distinct ERROR or CRITICAL rows of a build that failed outside a test."""
    errors = dict.fromkeys(first for row in logs if row["level"] in ("ERROR", "CRITICAL")
                           if (first := row["message"].partition("\n")[0].strip()))
    return [{"error": e} for e in list(errors)[:ERROR_LINES]]


def _test_failures(logs: list[dict]) -> list[dict]:
    """Return each failing test once with its summary, trimmed traceback and known error."""
    tours = {}
    for row in logs:
        if row["canonical_tag"] and row["message"].startswith("FAILED: ["):
            tours.setdefault(row["canonical_tag"], row["message"].partition("\n")[0])
    out = {}
    for row in logs:
        tag = row["canonical_tag"]
        if not tag or not (m := _TEST_ROW_RE.match(row["message"])):
            continue
        module = tag.split("/")[1]
        test = f"{module}: {m.group(1)}"
        # A retried test or a second subtest logs a second row, its first traceback is kept.
        if test in out:
            continue
        traceback, exception = _trim_traceback(module, row["message"].splitlines()[1:])
        out[test] = {"test": test, "summary": tours.get(tag) or exception,
                     "traceback": traceback, "tag": tag, "known_error": row["known_error"]}
    return list(out.values())


def _trim_traceback(module: str, block: list[str]) -> tuple[str, str | None]:
    """Return the test's own frames with the exception tail, and the last exception line."""
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
    exceptions = [line for line in lines if _EXCEPTION_RE.match(line)]
    return "\n".join(kept), next(reversed(exceptions), None)


def _check_finding(step: str, row: dict) -> dict:
    """Return one check row as a finding, its path, line and link from a markdown line link."""
    text = _MD_ESCAPE_RE.sub(r"\1", row["message"]) if row["type"] == "markdown" else row["message"]
    if m := _MD_LINE_LINK_RE.search(text):
        path, line, link = m.group(1), int(m.group(2)), m.group(3)
        text = text[:m.start()] + text[m.end():]
    else:
        path = line = link = None
    text = _MD_LINK_RE.sub(r"\1", text)
    return {"check": step, "path": path, "line": line, "message": " ".join(text.split()),
            "source_url": link}


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
