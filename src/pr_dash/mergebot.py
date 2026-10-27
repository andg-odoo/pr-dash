"""Fetch a PR's Mergebot page and parse it into readiness state."""

from __future__ import annotations

import http.client
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

PAGE_URL = "https://mergebot.odoo.com/{repo}/pull/{number}"
HOME_URL = "https://mergebot.odoo.com/runbot_merge"
TIMEOUT_SECONDS = 10
# The dashboard lists every branch of every project, about a megabyte
HOME_TIMEOUT_SECONDS = 30

# runbot_merge/models/pull_requests.py writes this description on an overridden status
_OVERRIDE_RE = re.compile(r"Overridden by @([\w-]+)")
_LINKED_RE = re.compile(r"([\w.-]+/[\w.-]+)#(\d+)")
_TABLE_HREF_RE = re.compile(r"/([\w.-]+/[\w.-]+)/pull/(\d+)")
_GITHUB_PR_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_RUNBOT_BATCH_RE = re.compile(r"https://runbot\.odoo\.com/runbot/batch/\d+")
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "wbr",
    },
)
_FINAL_STATES = {
    "alert-success": "merged",
    "alert-light": "closed",
    "alert-danger": "error",
}
_CHECK_STATUSES = {"ok": "ok", "fail": "fail", "lazy": "lazy", "": "pending"}
_TODO_FLAGS = {"ok": True, "fail": False}
_BLOCKER_CLASSES = {"text-danger", "text-warning"}


@dataclass
class Check:
    name: str
    status: str | None  # ok, fail, lazy or pending, None where the page shows no status
    description: str
    overridden: bool
    overridden_by: str | None


@dataclass
class LinkedPR:
    repo: str
    number: int
    ready: bool | None
    blockers: list[str] = field(default_factory=list)


@dataclass
class MergebotState:
    """What the Mergebot page says about one PR, or state "unknown" with the reason."""

    state: str  # blocked, ready, staged, merged, closed, error, unmanaged or unknown
    checks: list[Check] = field(default_factory=list)
    r_plus: bool | None = None
    merge_method: bool | None = None
    linked: list[LinkedPR] = field(default_factory=list)
    reason: str | None = None


@dataclass
class QueueSpot:
    """Where an r+'d PR sits on the Mergebot dashboard: in the running staging, a split, or awaiting."""

    branch: str
    staging_url: str | None = None  # the runbot batch of the running staging holding the PR
    staged: str | None = None  # the staging's "Staged at ... for Ns" title
    in_split: bool = False
    position: int | None = None  # rank in the Awaiting list, which is not the staging order
    awaiting: int = 0
    split_prs: int = 0
    running: bool = False  # whether the branch has a staging in progress


class _Unparseable(Exception):
    pass


class _Node:
    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict, parent: _Node | None):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[_Node | str] = []

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    @property
    def elements(self) -> list[_Node]:
        return [c for c in self.children if isinstance(c, _Node)]

    def own_text(self) -> str:
        return " ".join("".join(c for c in self.children if isinstance(c, str)).split())

    def text(self) -> str:
        return " ".join("".join(self._strings()).split())

    def _strings(self) -> Iterator[str]:
        for c in self.children:
            if isinstance(c, str):
                yield c
            else:
                yield from c._strings()

    def iter(self, tag: str) -> Iterator[_Node]:
        for c in self.elements:
            if c.tag == tag:
                yield c
            yield from c.iter(tag)

    def child(self, tag: str) -> _Node:
        found = next((c for c in self.elements if c.tag == tag), None)
        if found is None:
            raise _Unparseable(f"no <{tag}> under <{self.tag}>")
        return found


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self._cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self._cur)
        self._cur.children.append(node)
        if tag not in _VOID_TAGS:
            self._cur = node

    def handle_endtag(self, tag):
        node = self._cur
        while node.parent is not None and node.tag != tag:
            node = node.parent
        if node.parent is not None:
            self._cur = node.parent

    def handle_data(self, data):
        self._cur.children.append(data)


def page_url(repo: str, number: int) -> str:
    return PAGE_URL.format(repo=repo, number=number)


def fetch(repo: str, number: int) -> MergebotState:
    """Fetch and parse one PR's page, "unmanaged" on a 404 and "unknown" instead of raising."""
    url = page_url(repo, number)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as resp:
            html = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return MergebotState("unmanaged", reason=f"{url} not found")
        return MergebotState("unknown", reason=f"fetch {url} failed: {e}")
    except (OSError, http.client.HTTPException) as e:
        return MergebotState("unknown", reason=f"fetch {url} failed: {e}")
    return parse(html)


def fetch_queue() -> dict[str, QueueSpot] | None:
    """Read the Mergebot dashboard into the spot of each queued `owner/repo#n`, None when unreadable."""
    try:
        with urllib.request.urlopen(HOME_URL, timeout=HOME_TIMEOUT_SECONDS) as resp:
            return parse_queue(resp.read().decode("utf-8", "replace"))
    except (OSError, http.client.HTTPException):
        return None


def parse_queue(html: str) -> dict[str, QueueSpot]:
    """Map each PR of a running staging, a split or an Awaiting list to its QueueSpot."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    out = {}
    for section in builder.root.iter("section"):
        h2 = next((c for c in section.elements if c.tag == "h2"), None)
        a = h2 and next(h2.iter("a"), None)
        # Project sections carry an <h1> and a ministate <h2> of staging anchors.
        if a is None or not (a.attrs.get("href") or "").startswith("/runbot_merge/"):
            continue
        out.update(_branch_queue(section, a.text()))
    return out


def _branch_queue(section: _Node, branch: str) -> dict[str, QueueSpot]:
    out = {}
    running = False
    for ul in section.elements:
        if ul.tag != "ul" or "stagings" not in ul.classes:
            continue
        for li in ul.elements:
            if "staging" not in li.classes or "bg-info" not in li.classes:
                continue
            running = True
            batch = next((m.group(0) for x in li.iter("a")
                          if (m := _RUNBOT_BATCH_RE.match(x.attrs.get("href") or ""))), None)
            button = next(li.iter("button"), None)
            for pr in _github_prs(li.child("ul")):
                out[pr] = QueueSpot(branch, staging_url=batch,
                                    staged=button and button.attrs.get("title"), running=True)
    lists = {}
    for div in section.elements:
        h5 = div.tag == "div" and next((c for c in div.elements if c.tag == "h5"), None)
        if h5:
            lists[h5.own_text()] = _github_prs(div)
    splits = lists.get("Splits", [])
    for pr in splits:
        out[pr] = QueueSpot(branch, in_split=True, split_prs=len(splits), running=running)
    awaiting = lists.get("Awaiting", [])
    for i, pr in enumerate(awaiting, 1):
        out[pr] = QueueSpot(branch, position=i, awaiting=len(awaiting), split_prs=len(splits),
                            running=running)
    return out


def _github_prs(node: _Node) -> list[str]:
    return [f"{m.group(1)}#{m.group(2)}" for a in node.iter("a")
            if (m := _GITHUB_PR_RE.fullmatch(a.attrs.get("href") or ""))]


def parse(html: str) -> MergebotState:
    """Parse the HTML of a Mergebot PR page, state "unknown" when it is not one."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    try:
        return _parse_page(builder.root)
    except _Unparseable as e:
        return MergebotState("unknown", reason=f"unparseable page: {e}")


# Follows the live mergebot.odoo.com PR page, see odoo/runbot runbot_merge/views/templates.xml
def _parse_page(root: _Node) -> MergebotState:
    h1 = next(root.iter("h1"), None)
    if h1 is None:
        msg = "no <h1>"
        raise _Unparseable(msg)
    siblings = h1.parent.elements
    after = siblings[siblings.index(h1) + 1 :]
    info = next((n for n in after if n.tag == "div" and "alert" in n.classes), None)
    if info is None:
        msg = "no state alert after the title"
        raise _Unparseable(msg)
    table = next((n for n in after if n.tag == "table"), None)
    blockers = _table_blockers(table) if table is not None else {}
    kind = next((c for c in info.classes if c.startswith("alert-")), None)
    if kind in _FINAL_STATES:
        state = _FINAL_STATES[kind]
        return MergebotState(
            state,
            checks=_statuses(info),
            linked=_linked(info, blockers),
        )
    if kind == "alert-primary":
        return MergebotState("staged", linked=_linked(info, blockers))
    if kind == "alert-info":
        return _parse_open(info, blockers)
    raise _Unparseable(f"unexpected state alert {kind!r}")


def _parse_open(info: _Node, blockers: dict) -> MergebotState:
    headline = info.child("p").text()
    if headline == "Blocked":
        state = "blocked"
    elif headline.startswith("Ready"):
        state = "ready"
    else:
        raise _Unparseable(f"unexpected headline {headline!r}")
    todo = {li.own_text(): li for li in info.child("ul").elements if li.tag == "li"}
    missing = {"Merge method", "Review", "CI"} - todo.keys()
    if missing:
        raise _Unparseable(f"missing todo items {sorted(missing)}")
    linked = []
    if "Linked pull requests" in todo:
        for li in todo["Linked pull requests"].child("ul").elements:
            linked.append(_linked_pr(li.child("a"), _class(li, _TODO_FLAGS), blockers))
    return MergebotState(
        state,
        checks=[
            _check(li, _class(li, _CHECK_STATUSES))
            for li in todo["CI"].child("ul").elements
        ],
        r_plus=_class(todo["Review"], _TODO_FLAGS),
        merge_method=_class(todo["Merge method"], _TODO_FLAGS),
        linked=linked,
    )


def _class(node: _Node, allowed: dict):
    key = " ".join(node.classes)
    if key not in allowed:
        raise _Unparseable(
            f"unexpected class {key!r} on <{node.tag}> {node.text()[:40]!r}",
        )
    return allowed[key]


def _check(li: _Node, status: str | None) -> Check:
    name = li.child("a").text()
    description = li.own_text().removeprefix(":").strip()
    override = _OVERRIDE_RE.match(description)
    return Check(
        name=name,
        status=status,
        description=description,
        overridden=override is not None,
        overridden_by=override and override.group(1),
    )


def _statuses(info: _Node) -> list[Check]:
    ul = next((n for n in info.elements if n.tag == "ul"), None)
    return [_check(li, None) for li in ul.elements] if ul is not None else []


def _linked(info: _Node, blockers: dict) -> list[LinkedPR]:
    for div in info.elements:
        if div.tag == "div" and div.own_text() == "Linked pull requests":
            return [
                _linked_pr(li.child("a"), None, blockers)
                for li in div.child("ul").elements
            ]
    return []


def _linked_pr(a: _Node, ready: bool | None, blockers: dict) -> LinkedPR:
    m = _LINKED_RE.fullmatch(a.text())
    if m is None:
        raise _Unparseable(f"unexpected linked PR {a.text()!r}")
    repo, number = m.group(1), int(m.group(2))
    return LinkedPR(repo, number, ready, blockers.get((repo, number), []))


def _table_blockers(table: _Node) -> dict[tuple[str, int], list[str]]:
    """Map each PR in the batch table to its blocking labels (missing r+, a failing check...)."""
    out = {}
    for span in table.iter("span"):
        a = next((c for c in span.elements if c.tag == "a"), None)
        m = a and _TABLE_HREF_RE.fullmatch(a.attrs.get("href") or "")
        if m:
            sups = [
                s
                for s in span.elements
                if s.tag == "sup" and _BLOCKER_CLASSES & {*s.classes}
            ]
            out[m.group(1), int(m.group(2))] = [s.text() for s in sups if s.text()]
    return out
