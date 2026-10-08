"""Tab definitions: per view, its rows, the row a PR ref names, and a compact and a full row."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pr_dash import query

if TYPE_CHECKING:
    from collections.abc import Callable

    from pr_dash.config import Config


class NoMatch(ValueError):
    """No row of the view holds the PR a ref names."""


class Elsewhere(NoMatch):
    """The PR a ref names is not in the view asked, but in `tab` as `row`."""

    def __init__(self, tab: Tab, row: dict, message: str):
        super().__init__(message)
        self.tab, self.row = tab, row


@dataclass(frozen=True)
class Tab:
    """One view: its `load` takes include_dismissed, its `resolve` raises NoMatch or ambiguity."""

    label: str
    reader: str
    load: Callable[..., list[dict]]
    resolve: Callable[[list[dict], str | int], dict]
    summarize: Callable[[dict], dict]
    detail: Callable[[Config, dict], dict]

    def find(self, cfg: Config, ref: str | int) -> dict:
        """The row `ref` names among every row, dismissed ones included."""
        return self.resolve(self.load(cfg, include_dismissed=True), ref)


def _members(row: dict) -> list[dict]:
    return row.get("members") or [row]


def hits(rows: list[dict], ref: str | int, prs=_members) -> list[tuple[dict, dict]]:
    """(row, PR) for each PR `ref` names, `prs` giving a row's PRs with repo, repo_short, number."""
    repo_full, repo_short, number = query._parse_ref(ref)
    return [
        (row, pr)
        for row in rows
        for pr in prs(row)
        if pr.get("number") == number
        and repo_full in (None, pr.get("repo"))
        and repo_short in (None, pr.get("repo_short"))
    ]


def _distinct(found: list[tuple[dict, dict]]) -> list[dict]:
    return list({id(row): row for row, _ in found}.values())


def _single(ref: str | int, found: list[tuple[dict, dict]]) -> dict:
    if len(_distinct(found)) > 1:
        prs = ", ".join(f"{pr['repo']}#{pr['number']}" for _, pr in found)
        raise ValueError(f"{ref!r} is ambiguous - candidates: {prs}")
    return found[0][0]


def _queue_label(item: dict) -> str:
    parts = [f"{m.get('repo_short')}#{m.get('number')}" for m in _members(item)]
    return f"{item['id']} [{' + '.join(parts)}]" if len(parts) > 1 else item["id"]


def _resolve_queue(items: list[dict], ref: str | int) -> dict:
    """Match every member, so an enterprise number resolves to its odoo+enterprise pair."""
    found = _distinct(hits(items, ref))
    if len(found) == 1:
        return found[0]
    if not found:
        raise NoMatch(f"No PR matching {ref!r} among {len(items)} Review queue PRs, see list_prs.")
    number = query._parse_ref(ref)[2]
    raise ValueError(
        f"Ambiguous PR reference {ref!r} matches: "
        f"{', '.join(_queue_label(it) for it in found)}. "
        f"Qualify with a repo, e.g. 'odoo#{number}' or 'enterprise#{number}'."
    )


def _resolve_tracked(items: list[dict], ref: str | int) -> dict:
    found = hits(items, ref)
    if not found:
        raise NoMatch(f"No tracked PR matches {ref!r} among {len(items)}, see list_tracked.")
    return _single(ref, found)


def mine_prs(branch_set: dict) -> list[dict]:
    return [
        {"repo": pr["repo"], "repo_short": pr["repo"].split("/")[-1], "number": pr["num"]}
        for m in branch_set["members"]
        for pr in [m, *m["fw"]]
    ]


def _resolve_mine(sets: list[dict], ref: str | int) -> dict:
    """Match every member of a Branch set and each of their Forward-ports."""
    found = hits(sets, ref, mine_prs)
    if not found:
        raise NoMatch(f"No Authored PR matches {ref!r} among {len(sets)} Branch sets, see list_mine.")
    return _single(ref, found)


QUEUE = Tab(
    label="Review queue",
    reader="get_pr",
    load=lambda cfg, include_dismissed=False: query.load_items(cfg),
    resolve=_resolve_queue,
    summarize=query.summarize,
    detail=lambda _cfg, item: query.detail(item),
)
TRACKED = Tab(
    label="Tracked tab",
    reader="get_tracked",
    load=query.load_tracked,
    resolve=_resolve_tracked,
    summarize=query.summarize_tracked,
    detail=lambda _cfg, t: query.tracked_detail(t),
)
MINE = Tab(
    label="Mine tab",
    reader="get_mine",
    load=query.load_mine,
    resolve=_resolve_mine,
    summarize=query.summarize_mine,
    detail=query.mine_detail,
)

# The order get_comments and queue_only walk the views in.
TABS = (QUEUE, MINE, TRACKED)


def first(cfg: Config, ref: str | int, *tabs: Tab) -> tuple[Tab, dict]:
    """The first of `tabs` holding the PR `ref` names with its row, else the first one's NoMatch."""
    misses = []
    for tab in tabs:
        try:
            return tab, tab.find(cfg, ref)
        except NoMatch as e:
            misses.append(e)
    raise misses[0]


def queue_only(cfg: Config, ref: str | int, tool: str) -> dict:
    """The Review queue row `ref` names, else an Elsewhere naming the first other view holding it."""
    try:
        return QUEUE.find(cfg, ref)
    except NoMatch as miss:
        for tab in TABS[1:]:
            try:
                row = tab.find(cfg, ref)
            except NoMatch:
                continue
            raise Elsewhere(tab, row, (
                f"{ref!r} is in the {tab.label}, {tool} covers the {QUEUE.label} only. "
                f"{tab.reader} reads it."
            )) from miss
        raise


def get_comments(cfg: Config, ref: str | int) -> dict:
    """The Discussion of the cached PR `ref` names, in the Review queue, Mine or Tracked."""
    tab, row = first(cfg, ref, *TABS)
    if tab is QUEUE:
        return {"id": row["id"], "discussion": row["discussion"]}
    return tab.detail(cfg, row)
